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
]

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
    """Preserve legacy duplicate rows by renaming their display filename."""
    cols = [
        row[1]
        for row in sync_conn.exec_driver_sql("PRAGMA table_info(assets)").fetchall()
    ]
    if not {"id", "task_id", "file_name"}.issubset(cols):
        return

    used_by_task: dict[str, set[str]] = {}
    rows = sync_conn.exec_driver_sql(
        "SELECT id, task_id, file_name FROM assets ORDER BY rowid"
    ).fetchall()
    for asset_id, task_id, file_name in rows:
        used = used_by_task.setdefault(task_id, set())
        unique = unique_asset_name(file_name or "asset.bin", used)
        if unique != file_name:
            # file_path stays unchanged: both legacy records may point at the same
            # stored object, while their route/display names become unambiguous.
            sync_conn.exec_driver_sql(
                "UPDATE assets SET file_name = ? WHERE id = ?",
                (unique, asset_id),
            )
        used.add(unique)
    sync_conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_assets_task_file_name "
        "ON assets(task_id, file_name)"
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
            _ensure_asset_filename_index(sync_conn)

        await conn.run_sync(_ensure_columns)
