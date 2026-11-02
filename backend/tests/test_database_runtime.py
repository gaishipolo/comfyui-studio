"""I17 回归：SQLite pool / WAL / busy timeout 必须显式匹配任务并发。"""
import asyncio
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import _build_engine  # noqa: E402


async def test_sqlite_engine_pool_and_pragmas() -> None:
    with tempfile.TemporaryDirectory(prefix="db-runtime-") as raw_dir:
        path = Path(raw_dir) / "runtime.db"
        engine = _build_engine(
            f"sqlite+aiosqlite:///{path.as_posix()}",
            echo=False,
            max_concurrent_tasks=12,
        )
        try:
            assert engine.pool.size() == 17, engine.pool.size()
            async with engine.connect() as connection:
                journal = await connection.scalar(text("PRAGMA journal_mode"))
                busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
            assert str(journal).lower() == "wal", journal
            assert int(busy_timeout) == 30_000, busy_timeout
        finally:
            await engine.dispose()


async def test_sqlite_pool_has_minimum_headroom() -> None:
    with tempfile.TemporaryDirectory(prefix="db-runtime-min-") as raw_dir:
        path = Path(raw_dir) / "runtime.db"
        engine = _build_engine(
            f"sqlite+aiosqlite:///{path.as_posix()}",
            echo=False,
            max_concurrent_tasks=2,
        )
        try:
            assert engine.pool.size() == 10, engine.pool.size()
        finally:
            await engine.dispose()


async def main() -> int:
    tests = [test_sqlite_engine_pool_and_pragmas, test_sqlite_pool_has_minimum_headroom]
    failures: list[str] = []
    for test in tests:
        try:
            await test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
