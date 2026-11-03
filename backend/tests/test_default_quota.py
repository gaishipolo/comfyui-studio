"""新用户的默认存储配额。

此前 `storage_quota_bytes` 默认 0（不限），等于没有防线：50 人 × 每天几十张
2MB 的图，一个月几十 GB。磁盘写满时是**整站故障**（上传和生成全部失败），
不是某一个人受影响。

不变量：
1. `default_quota_bytes()` 按 GB 换算成字节（不是 GiB 的近似，是 1024^3）
2. 配置为 0 / 负数 → 返回 0（不限），不能被当成「上限为零」把人锁死
3. 通过 `/api/auth/register` 建出来的用户，真的带上这个默认值
4. 存量用户不受影响（没有迁移代码会去改已有记录）
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import User  # noqa: E402
from app.quota import default_quota_bytes  # noqa: E402

GIB = 1024**3
_results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))


class _GB:
    """临时改 DEFAULT_STORAGE_QUOTA_GB，退出时还原。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def __enter__(self) -> None:
        self.original = settings.DEFAULT_STORAGE_QUOTA_GB
        settings.DEFAULT_STORAGE_QUOTA_GB = self.value

    def __exit__(self, *exc: object) -> None:
        settings.DEFAULT_STORAGE_QUOTA_GB = self.original


def test_conversion() -> None:
    with _GB(20):
        check("20 GB 换算成 20 * 1024^3 字节", default_quota_bytes() == 20 * GIB,
              f"got={default_quota_bytes()}")
    with _GB(0.5):
        check("小数 GB 也按字节精确换算", default_quota_bytes() == GIB // 2,
              f"got={default_quota_bytes()}")


def test_zero_and_negative_mean_unlimited() -> None:
    with _GB(0):
        check("配置 0 → 不限（返回 0，不是锁死）", default_quota_bytes() == 0,
              f"got={default_quota_bytes()}")
    with _GB(-5):
        check("负数配置 → 按不限处理", default_quota_bytes() == 0,
              f"got={default_quota_bytes()}")


async def test_register_applies_default() -> None:
    from app.main import app as fastapi_app

    with tempfile.TemporaryDirectory(prefix="quota-default-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def override_get_db():
            async with sessions() as session:
                yield session

        fastapi_app.dependency_overrides[get_db] = override_get_db
        try:
            async with sessions() as db:
                db.add(
                    User(
                        id="u-admin",
                        username="admin",
                        password_hash=hash_password("admin123"),
                        display_name="管理员",
                        role="admin",
                        storage_quota_bytes=0,
                    )
                )
                await db.commit()

            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "admin123"},
                )
                check("管理员能登录", login.status_code == 200, f"{login.status_code} {login.text[:120]}")
                token = login.json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                with _GB(20):
                    created = await client.post(
                        "/api/auth/register",
                        json={"username": "newbie", "password": "pass1234"},
                        headers=headers,
                    )
                check("新建用户成功", created.status_code == 200,
                      f"{created.status_code} {created.text[:160]}")
                body = created.json()
                check(
                    "新用户带上 20 GB 默认配额",
                    body.get("storage_quota_bytes") == 20 * GIB,
                    f"storage_quota_bytes={body.get('storage_quota_bytes')}",
                )

                # 再建一个：配置关掉后应该是不限（0）
                with _GB(0):
                    created_off = await client.post(
                        "/api/auth/register",
                        json={"username": "unlimited", "password": "pass1234"},
                        headers=headers,
                    )
                check(
                    "配置为 0 时新用户不限（0）",
                    created_off.status_code == 200
                    and created_off.json().get("storage_quota_bytes") == 0,
                    f"{created_off.status_code} {created_off.text[:160]}",
                )

                # admin 自己建出来时也不该被默认值影响行为（豁免在 quota 层）
                listed = await client.get("/api/auth/users", headers=headers)
                if listed.status_code == 200:
                    users = {u["username"]: u for u in listed.json()}
                    check(
                        "存量 admin 没被默认值改写",
                        users.get("admin", {}).get("storage_quota_bytes") == 0,
                        f"admin={users.get('admin', {}).get('storage_quota_bytes')}",
                    )
        finally:
            fastapi_app.dependency_overrides.pop(get_db, None)
            # 必须显式释放连接池，否则 Windows 上临时目录里的 app.db 仍被占用，
            # TemporaryDirectory 清理时抛 PermissionError（WinError 32）
            await engine.dispose()


async def main() -> int:
    test_conversion()
    test_zero_and_negative_mean_unlimited()
    await test_register_applies_default()

    failed = [r for r in _results if not r[1]]
    print(f"\nRESULT: {len(_results) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
