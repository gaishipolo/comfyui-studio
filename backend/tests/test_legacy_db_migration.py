"""T1 回归：存量库升级路径（补列 + JSON 转义改写）必须可用且幂等。

场景：老库缺 users.token_version 等新列，JSON 列里是 \\uXXXX 转义形式。
跑一次 init_db 之后：列补齐、转义解码成真实字符；再跑一次不能改坏数据。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.models import Asset, Task, Template, User  # noqa: E402


LEGACY_SCHEMA = """
CREATE TABLE users (
    id VARCHAR(32) PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(128) NOT NULL,
    display_name VARCHAR(64) DEFAULT '',
    role VARCHAR(16) DEFAULT 'member',
    is_active BOOLEAN DEFAULT 1,
    created_at DATETIME
);
CREATE TABLE templates (
    id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    description TEXT DEFAULT '',
    category VARCHAR(64) DEFAULT 'image',
    icon VARCHAR(256) DEFAULT '',
    input_schema TEXT DEFAULT '{}',
    engine VARCHAR(32) DEFAULT 'comfyui',
    workflow_file VARCHAR(256) DEFAULT '',
    param_mapping TEXT DEFAULT '{}',
    output_config TEXT DEFAULT '{}',
    is_active BOOLEAN DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE TABLE tasks (
    id VARCHAR(32) PRIMARY KEY,
    template_id VARCHAR(32) NOT NULL,
    status VARCHAR(16) DEFAULT 'pending',
    progress FLOAT DEFAULT 0.0,
    params TEXT DEFAULT '{}',
    provider_task_id VARCHAR(64) DEFAULT '',
    result TEXT DEFAULT '{}',
    error_msg TEXT DEFAULT '',
    created_at DATETIME,
    started_at DATETIME,
    finished_at DATETIME
);
CREATE TABLE assets (
    id VARCHAR(32) PRIMARY KEY,
    task_id VARCHAR(32) NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    file_name VARCHAR(256) NOT NULL,
    file_size INTEGER DEFAULT 0,
    mime_type VARCHAR(64) DEFAULT 'image/png',
    tags TEXT DEFAULT '[]',
    meta TEXT DEFAULT '{}',
    created_at DATETIME
);
"""

LEGACY_ROWS = [
    (
        "INSERT INTO users (id, username, password_hash, display_name, role) "
        "VALUES ('u1', 'legacy', 'hash', '旧用户', 'member')"
    ),
    (
        "INSERT INTO templates (id, name, input_schema, param_mapping, output_config) "
        "VALUES ('t1', '旧模板', "
        "'{\"fields\":[{\"name\":\"prompt\",\"label\":\"\\u63d0\\u793a\\u8bcd\"}]}', "
        "'{\"prompt\":\"1.inputs.value\"}', "
        "'{\"nodes\":[\"9\"]}')"
    ),
    (
        "INSERT INTO tasks (id, template_id, params, result) "
        "VALUES ('k1', 't1', '{\"prompt\":\"\\u732b\"}', '{\"note\":\"\\u4f60\\u597d\"}')"
    ),
    (
        "INSERT INTO assets (id, task_id, file_path, file_name, tags, meta) "
        "VALUES ('a1', 'k1', 'k1/x.png', 'x.png', '[\"\\u56fe\\u7247\"]', '{\"k\":\"\\u503c\"}')"
    ),
]


def seed_legacy_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(LEGACY_SCHEMA)
        for statement in LEGACY_ROWS:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def columns_of(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def raw_value(path: Path, table: str, column: str, rowid: int = 1) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute(
            f'SELECT "{column}" FROM "{table}" WHERE rowid = ?', (rowid,)
        ).fetchone()[0]
    finally:
        con.close()


def run_migration(path: Path) -> None:
    """在指定库上执行与 app.database.init_db 相同的补列/改写逻辑。"""
    from app import database as db_module

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:

            def _ensure(sync_conn):
                for table, column, ddl in db_module._COLUMN_MIGRATIONS:
                    cols = {
                        row[1]
                        for row in sync_conn.exec_driver_sql(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    if cols and column not in cols:
                        sync_conn.exec_driver_sql(ddl)
                db_module._unescape_json_columns(sync_conn)
                db_module._ensure_asset_filename_index(sync_conn)

            _ensure(conn)
    finally:
        engine.dispose()


def test_legacy_db_gets_columns_and_unescaped_json() -> None:
    with tempfile.TemporaryDirectory(prefix="legacy-db-") as raw_dir:
        path = Path(raw_dir) / "legacy.db"
        seed_legacy_db(path)

        run_migration(path)

        assert "token_version" in columns_of(path, "users")
        assert "is_shared" in columns_of(path, "assets")
        assert "capability" in columns_of(path, "templates")
        assert "created_by" in columns_of(path, "tasks")

        schema = raw_value(path, "templates", "input_schema")
        assert "\\u" not in schema, schema
        assert "提示词" in schema, schema
        assert "标签" not in raw_value(path, "assets", "tags")
        assert "图片" in raw_value(path, "assets", "tags")
        assert "猫" in raw_value(path, "tasks", "params")


def test_migration_is_idempotent() -> None:
    with tempfile.TemporaryDirectory(prefix="legacy-db-idem-") as raw_dir:
        path = Path(raw_dir) / "legacy.db"
        seed_legacy_db(path)

        run_migration(path)
        first = {
            (table, column): raw_value(path, table, column)
            for table, column in (
                ("templates", "input_schema"),
                ("assets", "tags"),
                ("tasks", "params"),
            )
        }
        run_migration(path)
        second = {
            (table, column): raw_value(path, table, column)
            for table, column in (
                ("templates", "input_schema"),
                ("assets", "tags"),
                ("tasks", "params"),
            )
        }
        assert first == second, (first, second)


def test_model_metadata_still_matches_migrated_columns() -> None:
    # 迁移补的列必须与模型声明一致，避免「库里有列、模型没有」导致 ORM 报错
    assert "token_version" in User.__table__.columns
    assert "is_shared" in Asset.__table__.columns
    assert "capability" in Template.__table__.columns
    assert "created_by" in Task.__table__.columns
    assert set(Base.metadata.tables) >= {"users", "templates", "tasks", "assets"}


def main() -> int:
    tests = [
        test_legacy_db_gets_columns_and_unescaped_json,
        test_migration_is_idempotent,
        test_model_metadata_still_matches_migrated_columns,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
