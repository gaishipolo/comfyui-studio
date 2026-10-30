"""数据库连接"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=settings.SQL_ECHO)
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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _ensure_columns(sync_conn):
            for table, column, ddl in _COLUMN_MIGRATIONS:
                cols = [r[1] for r in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]
                if cols and column not in cols:
                    sync_conn.exec_driver_sql(ddl)
            _unescape_json_columns(sync_conn)

        await conn.run_sync(_ensure_columns)
