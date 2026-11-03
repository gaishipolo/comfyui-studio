"""存储配额回归：边界 / 超限 / 管理员豁免 / 0=不限 / 回收站计入。

核心不变量：
- 配额是**准入控制**：进入前判断「已用 + 本次 <= 上限」，等于上限算通过
- 回收站里的素材照样计入。软删除只打时间戳、文件还在盘上，删了就"腾出空间"
  是假的 —— 只有彻底删除才释放
- 管理员不受准入限制（否则被自己的配额锁在管理页外面），但用量照常统计
- `quota == 0` 表示不限，必须当作"永不拦截"，不能当成"上限为零"
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import main as app_main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import Asset, User  # noqa: E402
from app.quota import ensure_quota, human_bytes, usage_summary, used_bytes  # noqa: E402
from app.timeutils import utcnow  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
engine = create_async_engine(f"sqlite+aiosqlite:///{_tmp.name}")
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

KB = 1024
MB = 1024 * KB

ADMIN = User(
    id="u-admin",
    username="admin",
    password_hash="x",
    role="admin",
    is_active=True,
    storage_quota_bytes=100 * MB,  # 管理员即便设了上限也不该被拦
)
MEMBER = User(
    id="u-member",
    username="bob",
    password_hash="x",
    role="member",
    is_active=True,
    storage_quota_bytes=1000,
)
UNLIMITED = User(
    id="u-unlimited",
    username="carol",
    password_hash="x",
    role="member",
    is_active=True,
    storage_quota_bytes=0,
)
OTHER = User(
    id="u-other",
    username="eve",
    password_hash="x",
    role="member",
    is_active=True,
    storage_quota_bytes=1000,
)


def make_asset(asset_id: str, owner: str, *, size: int, trashed: bool = False) -> Asset:
    return Asset(
        id=asset_id,
        owner_id=owner,
        task_id=None,
        file_path=f"assets/{asset_id}/file.png",
        file_name=f"{asset_id}.png",
        file_size=size,
        mime_type="image/png",
        tags=[],
        meta={},
        is_shared=False,
        deleted_at=utcnow() if trashed else None,
    )


async def _override_db():
    async with Session() as session:
        yield session


async def _reset(assets: list[Asset]) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as db:
        for user in (ADMIN, MEMBER, UNLIMITED, OTHER):
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


async def _assert_rejected(user: User, incoming: int = 0) -> str:
    """调用 ensure_quota，断言被 403 拒绝，返回 detail 文本。"""
    async with Session() as db:
        try:
            await ensure_quota(db, user, incoming)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            assert status == 403, f"期望 403，实际 {status}: {exc}"
            return str(getattr(exc, "detail", exc))
    raise AssertionError("配额已超限，但 ensure_quota 没有拒绝")


async def test_used_bytes_counts_trash() -> None:
    # 回收站里的 400 字节仍在磁盘上，必须计入 —— 否则"删了就有空间"是假的
    await _reset([
        make_asset("live", MEMBER.id, size=600),
        make_asset("gone", MEMBER.id, size=400, trashed=True),
    ])
    async with Session() as db:
        assert await used_bytes(db, MEMBER.id) == 1000
        assert await used_bytes(db, OTHER.id) == 0, "只统计本人的占用"


async def test_boundary_equal_is_allowed() -> None:
    # 「已用 + 本次 == 上限」必须通过。写成 `<` 会让用户在上限处莫名被拒
    await _reset([make_asset("a", MEMBER.id, size=1000)])
    async with Session() as db:
        await ensure_quota(db, MEMBER)          # 已用 1000 / 上限 1000
        await ensure_quota(db, MEMBER, 0)       # 显式传 0 也不该拒
    detail = await _assert_rejected(MEMBER, 1)  # 再多 1 字节就不行了
    assert "配额不足" in detail, detail


async def test_over_quota_rejected_with_numbers() -> None:
    await _reset([make_asset("a", MEMBER.id, size=900)])
    detail = await _assert_rejected(MEMBER, 200)
    # 提示里要能看出「已用 / 上限 / 本次需要」，否则用户不知道该怎么处理
    assert "900 B" in detail, detail
    assert "1000 B" in detail, detail
    assert "200 B" in detail, detail
    assert "彻底删除" in detail, "必须告诉用户怎么真正释放空间"


async def test_admin_is_exempt() -> None:
    # 管理员被自己的配额锁住会连管理页都进不去
    await _reset([make_asset("a", ADMIN.id, size=10 * MB)])
    async with Session() as db:
        await ensure_quota(db, ADMIN, 10 * MB)


async def test_zero_quota_means_unlimited() -> None:
    # 0 是「不限」，不是「上限为零」—— 写反了会让所有未配额的账号传不了文件
    await _reset([make_asset("a", UNLIMITED.id, size=10 * MB)])
    async with Session() as db:
        await ensure_quota(db, UNLIMITED, 100 * MB)


async def test_usage_summary_shape() -> None:
    await _reset([make_asset("a", MEMBER.id, size=250)])
    async with Session() as db:
        summary = await usage_summary(db, MEMBER)
    assert summary == {
        "used_bytes": 250,
        "quota_bytes": 1000,
        "unlimited": False,
        "percent": 25.0,
    }, summary

    async with Session() as db:
        unlimited = await usage_summary(db, UNLIMITED)
    assert unlimited["unlimited"] is True
    assert unlimited["percent"] is None, "不限时没有百分比，不能用 0 冒充"


async def test_quota_endpoint_reports_usage() -> None:
    await _reset([
        make_asset("live", MEMBER.id, size=600),
        make_asset("gone", MEMBER.id, size=400, trashed=True),
    ])
    async with _client(MEMBER) as client:
        r = await client.get("/api/assets/quota")
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["used_bytes"] == 1000, "回收站要计入"
    assert body["quota_bytes"] == 1000
    assert body["percent"] == 100.0
    _clear_overrides()


async def test_human_bytes() -> None:
    assert human_bytes(0) == "0 B"
    assert human_bytes(999) == "999 B"
    assert human_bytes(1024) == "1.0 KB"
    assert human_bytes(int(1.5 * MB)) == "1.5 MB"


def main() -> int:
    tests = [
        test_used_bytes_counts_trash,
        test_boundary_equal_is_allowed,
        test_over_quota_rejected_with_numbers,
        test_admin_is_exempt,
        test_zero_quota_means_unlimited,
        test_usage_summary_shape,
        test_quota_endpoint_reports_usage,
        test_human_bytes,
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
