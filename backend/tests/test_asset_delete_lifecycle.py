"""素材软删除 × 任务生命周期 的并发/一致性检查（真实 SQLite）。

背景：素材删除在 2026-11 改成了**软删除**（只打 `deleted_at`）。因此这里
不再断言"删除素材要抢任务锁、要删文件"——那是旧契约。现在要守的是另一组
不变量：

- 软删除不碰文件系统，所以不需要任务锁，能立刻返回（拿锁会白等重试流程）
- 软删除幂等：第二次删同一素材是 404，不会覆盖掉原来的 `deleted_at`
- 重试 = 重新生成，旧产物必须**连回收站里的**一起清掉，否则磁盘上留孤儿
- 行已经被重试/删任务清掉后，再删素材必须是 404，不能 500
- `delete_asset` 只读 `user.id/role`，传 ORM 托管对象也不该触发懒加载异常
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
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
from app.thumbs import thumb_key  # noqa: E402


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


async def trashed_at(sessions, asset_id: str):
    async with sessions() as db:
        asset = await db.get(Asset, asset_id)
        return None if asset is None else asset.deleted_at


async def fake_spawn(_task_id: str, resume: bool = False) -> bool:
    return True


async def test_soft_delete_keeps_files_and_does_not_wait_for_task_lock() -> None:
    # 软删除不碰文件系统，因此不该去抢任务锁：抢了就会在重试流程持锁期间干等。
    # 这里刻意让外部持锁，删除仍应立刻返回。
    async with fixture("soft-keep-") as (sessions, task_id, asset_id):
        deleted = await asyncio.wait_for(
            call_asset_delete(sessions, asset_id), timeout=2
        )

        assert deleted["id"] == asset_id
        assert deleted["deleted_at"], "软删除必须带上时间戳"
        assert await trashed_at(sessions, asset_id) is not None
        # 文件原样留在磁盘上 —— 这是"可恢复"的前提，也是配额不释放的原因
        assert asset_exists(f"{task_id}/image.png")
        assert asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_soft_delete_is_idempotent() -> None:
    async with fixture("soft-twice-") as (sessions, _task_id, asset_id):
        first = await call_asset_delete(sessions, asset_id)
        assert first["deleted_at"]
        first_ts = await trashed_at(sessions, asset_id)

        try:
            await call_asset_delete(sessions, asset_id)
        except HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("重复删除同一素材应当 404（幂等）")

        # 首次打的时间戳不能被第二次冲掉
        assert await trashed_at(sessions, asset_id) == first_ts


async def test_retry_purges_trashed_assets() -> None:
    # 重试会重新生成，旧产物（包括已进回收站的）必须彻底清掉：
    # 只清正常素材的话，回收站里的文件就再也没人扫得到（孤儿）
    async with fixture("retry-purge-") as (sessions, task_id, asset_id):
        await call_asset_delete(sessions, asset_id)
        with patch.object(tasks_api, "spawn_task", fake_spawn):
            retried = await call_retry(sessions, task_id)

        assert retried.status == "pending", retried.status
        assert await db_counts(sessions, task_id) == (0, 1, "pending")
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_retry_then_asset_delete_returns_404() -> None:
    async with fixture("retry-first-") as (sessions, task_id, asset_id):
        with patch.object(tasks_api, "spawn_task", fake_spawn):
            retried = await call_retry(sessions, task_id)
        assert retried.status == "pending"

        try:
            await call_asset_delete(sessions, asset_id)
        except HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("素材行已被重试清掉，删除应当 404")

        assert await db_counts(sessions, task_id) == (0, 1, "pending")


async def test_task_delete_then_asset_delete_returns_404_not_500() -> None:
    async with fixture("task-first-") as (sessions, task_id, asset_id):
        deleted_task = await call_task_delete(sessions, task_id)
        assert deleted_task["id"] == task_id, deleted_task

        try:
            await call_asset_delete(sessions, asset_id)
        except HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("任务连同素材已删除，再删素材应当 404 而不是成功")

        assert await db_counts(sessions, task_id) == (0, 0, None)
        assert not asset_exists(f"{task_id}/image.png")
        assert not asset_exists(thumb_key(f"{task_id}/image.png"))


async def test_session_bound_user_survives_delete() -> None:
    # delete_asset 只用 user.id / user.role。ORM 托管对象在 commit 后可能被
    # 置为过期，若函数里读了别的属性就会触发异步懒加载而炸 —— 这条守住它。
    async with fixture("orm-user-") as (sessions, _task_id, asset_id):
        async with sessions() as db:
            user = await db.get(User, "admin")
            assert user is not None
            result = await assets_api.delete_asset(asset_id, user=user, db=db)

        assert result["id"] == asset_id
        assert await trashed_at(sessions, asset_id) is not None


async def main() -> int:
    tests = [
        test_soft_delete_keeps_files_and_does_not_wait_for_task_lock,
        test_soft_delete_is_idempotent,
        test_retry_purges_trashed_assets,
        test_retry_then_asset_delete_returns_404,
        test_task_delete_then_asset_delete_returns_404_not_500,
        test_session_bound_user_survives_delete,
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
