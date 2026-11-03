"""数据库连接"""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.asset_names import unique_asset_name


_SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _build_engine(
    database_url: str,
    *,
    echo: bool,
    max_concurrent_tasks: int,
):
    """Build the shared engine with headroom for long-lived task sessions."""
    pool_size = max(10, int(max_concurrent_tasks) + 5)
    options: dict = {"echo": echo, "pool_size": pool_size}
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        options["connect_args"] = {"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}

    built = create_async_engine(database_url, **options)
    if is_sqlite:
        @event.listens_for(built.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(
                    f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_SECONDS * 1000}"
                )
            finally:
                cursor.close()
    return built


engine = _build_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    max_concurrent_tasks=settings.MAX_CONCURRENT_TASKS,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


# 轻量列迁移：老库补新增列（create_all 不会改已有表）
_COLUMN_MIGRATIONS = [
    ("tasks", "created_by", "ALTER TABLE tasks ADD COLUMN created_by VARCHAR(32) DEFAULT ''"),
    # 用户隔离（2026-10-29）：素材默认仅作者可见，is_shared=True 才给团队看
    ("assets", "is_shared", "ALTER TABLE assets ADD COLUMN is_shared BOOLEAN DEFAULT 0"),
    # 多引擎插件（2026-10-29）：模板绑定能力 kind
    ("templates", "capability", "ALTER TABLE templates ADD COLUMN capability VARCHAR(64) DEFAULT ''"),
    # 令牌吊销（2026-11-01）：改密/重置递增版本，让旧 JWT 失效
    ("users", "token_version", "ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0"),
    # 素材独立化（2026-11-02）：归属从 task.created_by 搬到素材自己身上，
    # 上传素材没有任务也能确定归属。加完列由 _backfill_asset_owner 回填。
    ("assets", "owner_id", "ALTER TABLE assets ADD COLUMN owner_id VARCHAR(32) DEFAULT ''"),
]

# SQLite 不支持 ALTER COLUMN，改 task_id 可空只能重建表。列顺序与 models.Asset 对齐。
_ASSETS_REBUILD_DDL = """
CREATE TABLE assets_rebuild (
    id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL DEFAULT '',
    task_id VARCHAR(32),
    file_path VARCHAR(512) NOT NULL,
    file_name VARCHAR(256) NOT NULL,
    file_size INTEGER NOT NULL,
    mime_type VARCHAR(64) NOT NULL,
    tags TEXT NOT NULL,
    meta TEXT NOT NULL,
    is_shared BOOLEAN DEFAULT 0,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(task_id) REFERENCES tasks (id)
)
"""


def _rebuild_assets_optional_task(sync_conn) -> None:
    """把 assets.task_id 从 NOT NULL 改成可空（SQLite 唯一的办法是重建表）。

    幂等：task_id 已经可空就立刻返回，不会每次启动都重建一遍。
    上传素材的 task_id 是 NULL，因此旧唯一索引 (task_id, file_name) 必须去掉 ——
    SQLite 认为 NULL 互不相等，留着它约束不到任何一行，反而制造假的安全感。
    """
    info = sync_conn.exec_driver_sql("PRAGMA table_info(assets)").fetchall()
    if not info:
        return
    # PRAGMA table_info 列：cid, name, type, notnull, dflt_value, pk
    notnull_by_name = {row[1]: row[3] for row in info}
    if not notnull_by_name.get("task_id"):
        return  # 已是新结构

    target = [
        "id", "owner_id", "task_id", "file_path", "file_name",
        "file_size", "mime_type", "tags", "meta", "is_shared", "created_at",
    ]
    cols = [c for c in target if c in notnull_by_name]
    # 老库可能有 NULL（历史 DDL 与 ORM 不完全一致），逐列给兜底值，别让整表迁移失败
    fallbacks = {
        "owner_id": "COALESCE(owner_id, '')",
        "file_size": "COALESCE(file_size, 0)",
        "mime_type": "COALESCE(mime_type, 'application/octet-stream')",
        "tags": "COALESCE(tags, '[]')",
        "meta": "COALESCE(meta, '{}')",
        "is_shared": "COALESCE(is_shared, 0)",
    }
    select_list = ", ".join(fallbacks.get(c, c) for c in cols)

    sync_conn.exec_driver_sql("DROP TABLE IF EXISTS assets_rebuild")
    sync_conn.exec_driver_sql(_ASSETS_REBUILD_DDL)
    sync_conn.exec_driver_sql(
        f"INSERT INTO assets_rebuild ({', '.join(cols)}) "
        f"SELECT {select_list} FROM assets"
    )
    sync_conn.exec_driver_sql("DROP TABLE assets")
    sync_conn.exec_driver_sql("ALTER TABLE assets_rebuild RENAME TO assets")


def _backfill_asset_owner(sync_conn) -> None:
    """把老数据的归属从 task.created_by 搬到 assets.owner_id（幂等）。

    只填 owner_id 为空的行：上传素材 owner_id 一开始就写对了，不会被覆盖。
    无主任务（created_by 为空）的素材同样留空 —— 语义仍是「仅管理员可见」。
    """
    cols = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(assets)").fetchall()}
    if "owner_id" not in cols:
        return
    sync_conn.exec_driver_sql(
        "UPDATE assets SET owner_id = COALESCE("
        "(SELECT created_by FROM tasks WHERE tasks.id = assets.task_id), '') "
        "WHERE owner_id IS NULL OR owner_id = ''"
    )

# JSON 列改写：老库用 ensure_ascii=True 存过 \uXXXX 转义，会让中文 LIKE 搜索失效。
# 这里统一解码后按 ensure_ascii=False 重写，与 JSONStore 的写入格式对齐。
_JSON_TEXT_COLUMNS = [
    ("templates", "input_schema"),
    ("templates", "param_mapping"),
    ("templates", "output_config"),
    ("tasks", "params"),
    ("tasks", "result"),
    ("assets", "tags"),
    ("assets", "meta"),
]


def _unescape_json_columns(sync_conn):
    r"""把 JSON 列里 \uXXXX 转义形式改写为真实字符（幂等，重复执行无副作用）"""
    import json

    for table, column in _JSON_TEXT_COLUMNS:
        cols = [r[1] for r in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]
        if not cols or column not in cols:
            continue

        rows = sync_conn.exec_driver_sql(f'SELECT rowid, "{column}" FROM "{table}"').fetchall()
        for rowid, raw in rows:
            if raw is None or "\\u" not in raw:
                continue          # 无转义，跳过
            try:
                decoded = json.loads(raw)
            except (ValueError, TypeError):
                continue          # 脏数据不动，避免误伤
            fresh = json.dumps(decoded, ensure_ascii=False)
            if fresh != raw:
                sync_conn.exec_driver_sql(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    (fresh, rowid),
                )


def _ensure_asset_filename_index(sync_conn) -> None:
    """文件名去重 + 建列表索引。

    去重仍是应用层语义（同名卡片在界面上分不清），但**不再建唯一索引**：
    task_id 可空后 (task_id, file_name) 约束不到 NULL 行，留着只会误导。
    身份定位已改为 asset_id，重名不再影响路由。
    """
    cols = {
        row[1]
        for row in sync_conn.exec_driver_sql("PRAGMA table_info(assets)").fetchall()
    }
    if not {"id", "task_id", "file_name"}.issubset(cols):
        return

    has_owner = "owner_id" in cols
    used_groups: dict[object, set[str]] = {}
    rows = sync_conn.exec_driver_sql(
        "SELECT id, task_id, owner_id, file_name FROM assets ORDER BY rowid"
        if has_owner
        else "SELECT id, task_id, NULL, file_name FROM assets ORDER BY rowid"
    ).fetchall()
    for asset_id, task_id, owner_id, file_name in rows:
        # 生成素材按任务分组；上传素材（无任务）按归属人分组，
        # 否则所有用户的上传会挤在同一个命名空间里互相改名。
        group = task_id if task_id else f"owner:{owner_id or ''}"
        used = used_groups.setdefault(group, set())
        unique = unique_asset_name(file_name or "asset.bin", used)
        if unique != file_name:
            # file_path 保持不动：历史重复行可能指向同一个存储对象，
            # 改的只是显示名/路由名。
            sync_conn.exec_driver_sql(
                "UPDATE assets SET file_name = ? WHERE id = ?",
                (unique, asset_id),
            )
        used.add(unique)

    sync_conn.exec_driver_sql("DROP INDEX IF EXISTS uq_assets_task_file_name")
    sync_conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_assets_owner_created "
        "ON assets(owner_id, created_at)"
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _ensure_columns(sync_conn):
            for table, column, ddl in _COLUMN_MIGRATIONS:
                cols = [r[1] for r in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]
                if cols and column not in cols:
                    sync_conn.exec_driver_sql(ddl)
            _unescape_json_columns(sync_conn)
            # 顺序有讲究：先补 owner_id 列 → 再重建表让 task_id 可空
            # → 再回填归属（重建时 owner_id 还是空值）→ 最后去重/建索引。
            _rebuild_assets_optional_task(sync_conn)
            _backfill_asset_owner(sync_conn)
            _ensure_asset_filename_index(sync_conn)

        await conn.run_sync(_ensure_columns)
