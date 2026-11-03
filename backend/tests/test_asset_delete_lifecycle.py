"""Real-SQLite concurrency checks for single-Asset deletion ownership."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import assets as assets_api  # noqa: E402
from app.api import tasks as tasks_api  # noqa: E402
from app.config import settings  # noqa: E402
from app.filestore import asset_exists, reset_store, save_asset_file  # noqa: E402
from app.models.models import Asset, Base, Task, Template, User  # noqa: E402
from app.services import task_service  # noqa: E402
from app.thumbs import (  # noqa: E402
    cleanup_asset_files,
    task_lifecycle_key,
    thumb_key,
    thumbnail_lifecycle,
)


ADMIN = SimpleNamespace(id="admin", role="admin")


@asynccontextmanager
async def fixture(prefix: str):
    original_storage = settings.STORAGE_DIR
    with tempfile.TemporaryDirectory(prefix=prefix) as raw_dir:
        root = Path(raw_dir)
        settings.STORAGE_DIR = root / "storage"
        reset_store()
        engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'test.db'}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        task_id = f"task-{prefix.strip('-')}"
        asset_id = f"asset-{prefix.strip('-')}"
        template_id = f"tpl-{prefix.strip('-')}"
        async with sessions() as db:
            db.add(Template(
                id=template_id,
                name=f"Template {prefix}",
                engine="fake",
                category="image",
            ))
            db.add(Task(
                id=task_id,
                template_id=template_id,
                created_by="owner",
                status="cancelled",
                params={},
                result={},
            ))
            db.add(User(
                id="admin",
                username=f"admin-{prefix}",
                password_hash="unused",
                role="admin",
                is_active=True,
            ))
            db.add(Asset(
                id=asset_id,
                task_id=task_id,
                file_path=f"{task_id}/image.png",
                file_name="image.png",
                file_size=6,
                mime_type="image/png",
                tags=[],
                meta={},
            ))
            await db.commit()
        save_asset_file(f"{task_id}/image.png", b"source", "image/png")
        save_asset_file(thumb_key(f"{task_id}/image.png"), b"thumb", "image/jpeg")
        try:
            yield sessions, task_id, asset_id
        finally:
            task_service._cancel_requested.clear()
            tasks_api._running_tasks.discard(task_id)
            await engine.dispose()
            settings.STORAGE_DIR = original_storage
            reset_store()


async def call_asset_delete(sessions, asset_id: str):
    async with sessions() as db:
        return await assets_api.delete_asset(asset_id, user=ADMIN, db=db)


async def call_retry(sessions, task_id: str):
    async with sessions() as db:
        return await tasks_api.retry_task(task_id, user=ADMIN, db=db)


async def call_task_delete(sessions, task_id: str):
    async with sessions() as db:
        return await tasks_api.delete_task(task_id, user=ADMIN, db=db)


async def db_counts(sessions, task_id: str) -> tuple[int, int, str | None]:
    async with sessions() as db:
        assets = await db.scalar(
            select(func.count()).select_from(Asset).where(Asset.task_id == task_id)
        )
        task = await db.get(Task, task_id)
        return int(assets or 0), int(task is not None), task.status if task else None


async def fake_spawn(_task_id: str, resume: bool = False) -> bool:
    return True


async def test_asset_delete_then_retry_serializes_without_stale_rows() -> None:
    async with fixture("asset-first-") as (sessions, task_id, asset_id):
        with patch.object(tasks_api, "spawn_task", fake_spawn):
            async with thumbnail_lifecycle([task_lifecycle_key(task_id)]):
                delete = asyncio.create_task(call_asset_delete(sessions, asset_id))
                await asyncio.sleep(0.03)
                retry = asyncio.create_task(call_retry(sessions, task_id))
                await asyncio.sleep(0.03)
                delete_waited_for_task = not delete.done()
                retry_waited_for_task = not retry.done()
            deleted, retried = await asyncio.gather(delete, retry)

        assert delete_waited_for_task, "asset delete bypassed task lifecycle ownership"
        assert retry_waited_for_task
        assert deleted["id"] == asset_id
        assert retried.status == "pending"
        assert await db_counts(sessions, task_id) == (0, 1, "pending")
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_retry_then_asset_delete_reloads_current_asset() -> None:
    async with fixture("retry-first-") as (sessions, task_id, asset_id):
        with patch.object(tasks_api, "spawn_task", fake_spawn):
            async with thumbnail_lifecycle([task_lifecycle_key(task_id)]):
                retry = asyncio.create_task(call_retry(sessions, task_id))
                await asyncio.sleep(0.03)
                delete = asyncio.create_task(call_asset_delete(sessions, asset_id))
                await asyncio.sleep(0.03)
            retried, delete_result = await asyncio.gather(
                retry,
                delete,
                return_exceptions=True,
            )

        assert isinstance(retried, Task) and retried.status == "pending", retried
        assert isinstance(delete_result, HTTPException), delete_result
        assert delete_result.status_code == 404, delete_result
        assert await db_counts(sessions, task_id) == (0, 1, "pending")
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_task_delete_then_asset_delete_returns_404_not_500() -> None:
    async with fixture("task-first-") as (sessions, task_id, asset_id):
        async with thumbnail_lifecycle([task_lifecycle_key(task_id)]):
            task_delete = asyncio.create_task(call_task_delete(sessions, task_id))
            await asyncio.sleep(0.03)
            asset_delete = asyncio.create_task(call_asset_delete(sessions, asset_id))
            await asyncio.sleep(0.03)
        task_result, asset_result = await asyncio.gather(
            task_delete,
            asset_delete,
            return_exceptions=True,
        )

        assert task_result["id"] == task_id, task_result
        assert isinstance(asset_result, HTTPException), asset_result
        assert asset_result.status_code == 404, asset_result
        assert await db_counts(sessions, task_id) == (0, 0, None)
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_cancelled_asset_delete_keeps_task_ownership_until_commit() -> None:
    async with fixture("cancel-parent-") as (sessions, task_id, asset_id):
        started = threading.Event()
        release = threading.Event()
        real_cleanup = cleanup_asset_files

        def blocking_cleanup(file_path: str, file_name: str):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("cleanup gate timed out")
            return real_cleanup(file_path, file_name)

        acquired = asyncio.Event()

        async def contender() -> None:
            async with thumbnail_lifecycle([task_lifecycle_key(task_id)]):
                acquired.set()

        with patch.object(assets_api, "cleanup_asset_files", blocking_cleanup):
            parent = asyncio.create_task(call_asset_delete(sessions, asset_id))
            assert await asyncio.to_thread(started.wait, 2), "cleanup did not start"
            parent.cancel()
            contender_task = asyncio.create_task(contender())
            await asyncio.sleep(0.05)
            retained_ownership = not acquired.is_set() and not parent.done()
            release.set()
            try:
                await asyncio.wait_for(parent, timeout=2)
            except asyncio.CancelledError:
                cancelled = True
            else:
                cancelled = False
            await asyncio.wait_for(contender_task, timeout=2)

        assert retained_ownership, "cancelled delete released task ownership before mutation"
        assert cancelled, "caller cancellation did not propagate after child completion"
        assert await db_counts(sessions, task_id) == (0, 1, "cancelled")
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_session_bound_user_survives_hint_transaction_reset() -> None:
    async with fixture("orm-user-") as (sessions, task_id, asset_id):
        async with sessions() as db:
            user = await db.get(User, "admin")
            assert user is not None
            result = await assets_api.delete_asset(asset_id, user=user, db=db)

        assert result["id"] == asset_id
        assert await db_counts(sessions, task_id) == (0, 1, "cancelled")
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def main() -> int:
    tests = [
        test_asset_delete_then_retry_serializes_without_stale_rows,
        test_retry_then_asset_delete_reloads_current_asset,
        test_task_delete_then_asset_delete_returns_404_not_500,
        test_cancelled_asset_delete_keeps_task_ownership_until_commit,
        test_session_bound_user_survives_hint_transaction_reset,
    ]
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
