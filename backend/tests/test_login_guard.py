"""I3 回归：登录暴力破解防护。

连续 N 次失败（按 用户名+客户端IP 计数）锁 M 秒，锁定期内一律 429；
成功后计数清零。内存计数（单进程部署，重启清零可接受）。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import login_guard  # noqa: E402
from app.login_guard import LoginRateLimiter  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_locks_after_max_failures():
    clock = FakeClock()
    guard = LoginRateLimiter(max_failures=5, lock_seconds=900, now=clock)
    key = ("admin", "1.2.3.4")
    for _ in range(4):
        guard.record_failure(key)
        assert not guard.is_locked(key)
    guard.record_failure(key)
    assert guard.is_locked(key)


def test_lock_expires_and_counter_restarts():
    clock = FakeClock()
    guard = LoginRateLimiter(max_failures=5, lock_seconds=900, now=clock)
    key = ("admin", "1.2.3.4")
    for _ in range(5):
        guard.record_failure(key)
    assert guard.is_locked(key)
    clock.advance(900)
    assert not guard.is_locked(key)
    # 锁过期后计数从零开始：再连错 5 次才会重新锁
    for _ in range(4):
        guard.record_failure(key)
    assert not guard.is_locked(key)


def test_failure_while_locked_does_not_extend_lock():
    clock = FakeClock()
    guard = LoginRateLimiter(max_failures=5, lock_seconds=900, now=clock)
    key = ("admin", "1.2.3.4")
    for _ in range(5):
        guard.record_failure(key)
    clock.advance(100)
    guard.record_failure(key)  # 锁定期内的失败不得延长锁
    clock.advance(800)
    assert not guard.is_locked(key)


def test_success_resets_counter():
    clock = FakeClock()
    guard = LoginRateLimiter(max_failures=5, lock_seconds=900, now=clock)
    key = ("admin", "1.2.3.4")
    for _ in range(4):
        guard.record_failure(key)
    guard.record_success(key)
    for _ in range(4):
        guard.record_failure(key)
    assert not guard.is_locked(key)


def test_keys_are_independent():
    clock = FakeClock()
    guard = LoginRateLimiter(max_failures=5, lock_seconds=900, now=clock)
    for _ in range(5):
        guard.record_failure(("admin", "1.2.3.4"))
    assert guard.is_locked(("admin", "1.2.3.4"))
    assert not guard.is_locked(("admin", "5.6.7.8"))
    assert not guard.is_locked(("bob", "1.2.3.4"))


# ── 进程内 HTTP 链路：真实 login 端点 + 临时库 ──────────────

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.models.models import User  # noqa: E402
from app import main as app_main  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
engine = create_async_engine(f"sqlite+aiosqlite:///{_tmp.name}")
TestSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with TestSession() as session:
        yield session


async def test_login_endpoint_locks_after_five_failures():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    db = TestSession()
    db.add(User(id="u1", username="admin",
                password_hash=hash_password("correctpw123"), role="admin"))
    await db.commit()
    await db.close()

    login_guard.limiter = LoginRateLimiter()  # 干净计数
    app_main.app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(5):
                r = await client.post("/api/auth/login",
                                      json={"username": "admin", "password": "wrong"})
                assert r.status_code == 401, r.status_code
            # 第 6 次：即使密码正确也被锁
            r = await client.post("/api/auth/login",
                                  json={"username": "admin", "password": "correctpw123"})
            assert r.status_code == 429, r.status_code

            # 换一把干净限流器（模拟锁过期/重启后）→ 正确密码放行
            login_guard.limiter = LoginRateLimiter()
            r = await client.post("/api/auth/login",
                                  json={"username": "admin", "password": "correctpw123"})
            assert r.status_code == 200, r.status_code
            assert "access_token" in r.json()
    finally:
        app_main.app.dependency_overrides.clear()
        await engine.dispose()


def main() -> int:
    import asyncio
    tests = [
        test_locks_after_max_failures,
        test_lock_expires_and_counter_restarts,
        test_failure_while_locked_does_not_extend_lock,
        test_success_resets_counter,
        test_keys_are_independent,
        lambda: asyncio.run(test_login_endpoint_locks_after_five_failures()),
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{getattr(test, '__name__', 'test')}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {getattr(test, '__name__', 'test')}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
