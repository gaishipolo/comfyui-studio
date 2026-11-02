"""T4 回归：JWT 吊销走完整 HTTP 依赖链（改密 → 旧 token 立即 401）。

原先只测了 token_version_matches 这个纯函数：真实链路（HTTPBearer →
get_current_user → DB 查 token_version）没走过，依赖装配错了也发现不了。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import User  # noqa: E402


async def _build_client():
    """建一个只挂 /api/auth 路由的进程内应用，DB 指向临时 SQLite。"""
    from app.api.auth_api import router

    from fastapi import FastAPI

    with tempfile.TemporaryDirectory(prefix="auth-http-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(root / 'auth.db').as_posix()}"
        )
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        app = FastAPI()
        app.include_router(router)  # router 自带 /api/auth 前缀

        async def override_get_db():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        try:
            yield client, sessions
        finally:
            await client.aclose()
            await engine.dispose()


async def scenario() -> None:
    async for client, sessions in _build_client():
        async with sessions() as db:
            db.add(User(
                id="u-1",
                username="alice",
                password_hash=hash_password("oldpass123"),
                display_name="Alice",
                role="member",
            ))
            db.add(User(
                id="u-admin",
                username="root",
                password_hash=hash_password("adminpass123"),
                display_name="Root",
                role="admin",
            ))
            await db.commit()

        # 登录拿 token
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "oldpass123"}
        )
        assert response.status_code == 200, response.text
        old_token = response.json()["access_token"]

        headers = {"Authorization": f"Bearer {old_token}"}
        response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200, response.text

        # 改密：旧 token 必须立刻失效
        response = await client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"old_password": "oldpass123", "new_password": "newpass456"},
        )
        assert response.status_code == 200, response.text

        response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401, (
            f"改密后旧 token 仍可用: {response.status_code} {response.text}"
        )

        # 用新密码换新 token，新 token 可用
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "newpass456"}
        )
        assert response.status_code == 200, response.text
        new_token = response.json()["access_token"]
        response = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {new_token}"}
        )
        assert response.status_code == 200, response.text

        # 管理员重置密码同样吊销对方 token
        response = await client.post(
            "/api/auth/login", json={"username": "root", "password": "adminpass123"}
        )
        assert response.status_code == 200, response.text
        admin_headers = {
            "Authorization": f"Bearer {response.json()['access_token']}"
        }
        response = await client.post(
            "/api/auth/users/u-1/reset-password",
            headers=admin_headers,
            json={"new_password": "resetpass789"},
        )
        assert response.status_code == 200, response.text
        response = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {new_token}"}
        )
        assert response.status_code == 401, (
            f"管理员重置后旧 token 仍可用: {response.status_code} {response.text}"
        )

        # 停用账号 → 401
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "resetpass789"}
        )
        assert response.status_code == 200, response.text
        member_token = response.json()["access_token"]
        response = await client.patch(
            "/api/auth/users/u-1",
            headers=admin_headers,
            json={"is_active": False},
        )
        assert response.status_code == 200, response.text
        response = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {member_token}"}
        )
        assert response.status_code == 401, (
            f"停用后 token 仍可用: {response.status_code} {response.text}"
        )


def main() -> int:
    try:
        asyncio.run(scenario())
    except BaseException as exc:
        print(f"[FAIL] test_token_revocation_over_http: {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 1 failed")
        return 1
    print("[PASS] test_token_revocation_over_http")
    print("\nRESULT: 1 passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
