"""素材回收站回归：软删除 / 恢复 / 彻底删除 / 超期清理 / 权限边界。

核心不变量：
- 软删除只打时间戳，磁盘文件不动（误删可恢复）
- 回收站里的素材从列表、标签、文件下载、批量下载里全部消失（不能"删了还能下载"）
- 彻底删除必须先经过回收站（防止误点直接毁文件）
- 超期清理只清超过保留期的，且分批不炸
"""
import asyncio
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import main as app_main  # noqa: E402
from app.asset_retention import purge_expired  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import Asset, User  # noqa: E402
from app.timeutils import utcnow  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
engine = create_async_engine(f"sqlite+aiosqlite:///{_tmp.name}")
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

ADMIN = User(id="u-admin", username="admin", password_hash="x", role="admin", is_active=True)
MEMBER = User(id="u-member", username="bob", password_hash="x", role="member", is_active=True)
OTHER = User(id="u-other", username="eve", password_hash="x", role="member", is_active=True)


def make_asset(asset_id: str, owner: str, *, size: int = 1000, trashed_days_ago: int | None = None) -> Asset:
    deleted_at = utcnow() - timedelta(days=trashed_days_ago) if trashed_days_ago else None
    return Asset(
        id=asset_id,
        owner_id=owner,
        task_id=None,
        file_path=f"assets/{asset_id}/file.png",
        file_name=f"{asset_id}.png",
        file_size=size,
        mime_type="image/png",
        tags=["demo"],
        meta={},
        is_shared=False,
        deleted_at=deleted_at,
    )


async def _override_db():
    async with Session() as session:
        yield session


async def _reset(assets: list[Asset]) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as db:
        for user in (ADMIN, MEMBER, OTHER):
            db.add(user)
        for asset in assets:
            db.add(asset)
        await db.commit()


def _client(user: User) -> httpx.AsyncClient:
    app_main.app.dependency_overrides[get_db] = _override_db
    app_main.app.dependency_overrides[get_current_user] = lambda: user
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_main.app), base_url="http://test"
    )


def _clear_overrides() -> None:
    app_main.app.dependency_overrides.clear()


async def test_soft_delete_moves_to_trash() -> None:
    await _reset([make_asset("a1", MEMBER.id)])
    async with _client(MEMBER) as client:
        r = await client.delete("/api/assets/a1")
        assert r.status_code == 200, r.text
        assert "回收站" in r.json()["message"]

        listed = await client.get("/api/assets")
        assert listed.json() == [], "软删除后不该出现在正常列表"

        trash = await client.get("/api/assets/trash")
        assert [a["id"] for a in trash.json()] == ["a1"]
        assert trash.json()[0]["deleted_at"] is not None
    _clear_overrides()


async def test_restore_returns_asset() -> None:
    await _reset([make_asset("a1", MEMBER.id, trashed_days_ago=1)])
    async with _client(MEMBER) as client:
        r = await client.post("/api/assets/a1/restore")
        assert r.status_code == 200, r.text
        assert r.json()["deleted_at"] is None

        assert [a["id"] for a in (await client.get("/api/assets")).json()] == ["a1"]
        assert (await client.get("/api/assets/trash")).json() == []
    _clear_overrides()


async def test_trashed_asset_files_are_404() -> None:
    # 删了还能从旧链接下载 = 删除只是视觉上消失，这是必须堵住的
    await _reset([make_asset("a1", MEMBER.id, trashed_days_ago=1)])
    async with _client(MEMBER) as client:
        assert (await client.get("/api/assets/a1")).status_code == 404
        assert (await client.get("/api/assets/a1/file")).status_code == 404
        assert (await client.get("/api/assets/a1/thumb")).status_code == 404
    _clear_overrides()


async def test_purge_requires_trash_first() -> None:
    await _reset([make_asset("a1", MEMBER.id)])
    async with _client(MEMBER) as client:
        r = await client.delete("/api/assets/a1/purge")
        assert r.status_code == 409, "正常素材不能跳过回收站直接彻底删除"
    _clear_overrides()


async def test_purge_removes_record() -> None:
    await _reset([make_asset("a1", MEMBER.id, trashed_days_ago=1)])
    async with _client(MEMBER) as client:
        r = await client.delete("/api/assets/a1/purge")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "a1"
        assert (await client.get("/api/assets/trash")).json() == []
    async with Session() as db:
        assert (await db.execute(select(Asset).where(Asset.id == "a1"))).scalar_one_or_none() is None
    _clear_overrides()


async def test_member_cannot_delete_or_restore_others() -> None:
    await _reset([make_asset("a1", OTHER.id)])
    async with _client(MEMBER) as client:
        assert (await client.delete("/api/assets/a1")).status_code == 403
    await _reset([make_asset("a1", OTHER.id, trashed_days_ago=1)])
    async with _client(MEMBER) as client:
        assert (await client.post("/api/assets/a1/restore")).status_code == 403
        assert (await client.delete("/api/assets/a1/purge")).status_code == 403
    _clear_overrides()


async def test_member_sees_only_own_trash() -> None:
    await _reset([
        make_asset("mine", MEMBER.id, trashed_days_ago=1),
        make_asset("theirs", OTHER.id, trashed_days_ago=1),
    ])
    async with _client(MEMBER) as client:
        assert [a["id"] for a in (await client.get("/api/assets/trash")).json()] == ["mine"]
    async with _client(ADMIN) as client:
        assert len((await client.get("/api/assets/trash")).json()) == 2
    _clear_overrides()


async def test_tags_ignore_trashed() -> None:
    # 列表里看不到的素材，它的标签不该出现在筛选器上
    await _reset([
        make_asset("live", MEMBER.id),
        make_asset("gone", MEMBER.id, trashed_days_ago=1),
    ])
    async with _client(MEMBER) as client:
        tags = (await client.get("/api/assets/tags")).json()
        assert tags == [{"name": "demo", "count": 1}], tags
    _clear_overrides()


async def test_purge_expired_only_removes_old() -> None:
    await _reset([
        make_asset("old", MEMBER.id, trashed_days_ago=40),
        make_asset("fresh", MEMBER.id, trashed_days_ago=5),
        make_asset("live", MEMBER.id),
    ])
    async with Session() as db:
        result = await purge_expired(db, days=30)
    assert result["purged"] == 1, result
    async with Session() as db:
        left = {a.id for a in (await db.execute(select(Asset))).scalars().all()}
    assert left == {"fresh", "live"}, left


async def test_purge_expired_disabled_with_zero_days() -> None:
    await _reset([make_asset("old", MEMBER.id, trashed_days_ago=999)])
    async with Session() as db:
        result = await purge_expired(db, days=0)
    assert result["disabled"] is True
    async with Session() as db:
        assert (await db.execute(select(Asset))).scalars().all(), "days=0 不该删任何东西"


async def test_empty_trash_clears_all() -> None:
    # 清空回收站走和保留期清理同一套分批逻辑：删完要回 remaining=0，
    # 正常素材不能被顺手带走
    await _reset([
        make_asset("a", MEMBER.id, trashed_days_ago=1),
        make_asset("b", MEMBER.id, trashed_days_ago=2),
        make_asset("live", MEMBER.id),
    ])
    async with _client(MEMBER) as client:
        r = await client.delete("/api/assets/trash")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] == 2, body
        assert body["remaining"] == 0, body
        assert (await client.get("/api/assets/trash")).json() == []
        assert [a["id"] for a in (await client.get("/api/assets")).json()] == ["live"]
    _clear_overrides()


async def test_member_empty_trash_only_clears_own() -> None:
    await _reset([
        make_asset("mine", MEMBER.id, trashed_days_ago=1),
        make_asset("theirs", OTHER.id, trashed_days_ago=1),
    ])
    async with _client(MEMBER) as client:
        r = await client.delete("/api/assets/trash")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 1
    async with Session() as db:
        left = {a.id for a in (await db.execute(select(Asset))).scalars().all()}
    assert left == {"theirs"}, left
    _clear_overrides()


async def test_trashed_asset_cannot_be_modified() -> None:
    # 回收站里的东西对写操作一律 404：加标签、共享、当参考输入都不行
    await _reset([make_asset("a1", MEMBER.id, trashed_days_ago=1)])
    async with _client(MEMBER) as client:
        assert (await client.post("/api/assets/a1/tags?tag=x")).status_code == 404
        assert (await client.delete("/api/assets/a1/tags/demo")).status_code == 404
        assert (
            await client.post("/api/assets/a1/share", json={"shared": True})
        ).status_code == 404
        assert (
            await client.post(
                "/api/upload/from-asset", json={"asset_id": "a1", "engine": "comfyui"}
            )
        ).status_code == 404
    _clear_overrides()


def main() -> int:
    tests = [
        test_soft_delete_moves_to_trash,
        test_restore_returns_asset,
        test_trashed_asset_files_are_404,
        test_purge_requires_trash_first,
        test_purge_removes_record,
        test_member_cannot_delete_or_restore_others,
        test_member_sees_only_own_trash,
        test_tags_ignore_trashed,
        test_purge_expired_only_removes_old,
        test_purge_expired_disabled_with_zero_days,
        test_empty_trash_clears_all,
        test_member_empty_trash_only_clears_own,
        test_trashed_asset_cannot_be_modified,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            asyncio.run(test())
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
        finally:
            _clear_overrides()
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
