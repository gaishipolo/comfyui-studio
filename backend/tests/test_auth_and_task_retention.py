"""Focused checks for auth token revocation and background task retention.

Covers review findings:
- M1: changing/resetting a password must invalidate previously issued tokens.
- M6: fire-and-forget background coroutines must be strongly referenced.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth  # noqa: E402
from app.services import task_service  # noqa: E402


def test_token_carries_version_and_matches_user() -> None:
    user = SimpleNamespace(id="u1", username="u1", role="member", token_version=0)
    token = auth.create_token(user)
    payload = auth.decode_token(token)
    assert auth.token_version_matches(payload, user), payload

    # password was changed elsewhere -> version bumped -> old token must not match
    user.token_version = 1
    assert not auth.token_version_matches(payload, user)


def test_legacy_token_without_version_is_rejected_after_bump() -> None:
    user = SimpleNamespace(id="u1", username="u1", role="member", token_version=0)
    payload = {"sub": "u1", "role": "member"}  # 老 token 没有 ver 字段
    # version 0 时容忍缺省（等价 0），bump 后拒绝
    assert auth.token_version_matches(payload, user)
    user.token_version = 1
    assert not auth.token_version_matches(payload, user)


def test_spawn_task_retains_strong_reference_until_done() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(task_id: str, resume: bool = False) -> None:
            started.set()
            await release.wait()

        orig = task_service.TaskService.execute_task
        task_service.TaskService.execute_task = staticmethod(fake_run)
        try:
            task_service._running_tasks.discard("retain-check")
            task_service._cancel_requested.add("retain-check")
            assert await task_service.spawn_task("retain-check", resume=False) is True
            await asyncio.wait_for(started.wait(), timeout=1)
            # 存活期间必须被强引用持有，否则 asyncio 只做弱引用会被 GC
            assert any(
                t for t in task_service._background_tasks.values()
            ), task_service._background_tasks
            release.set()
            await asyncio.sleep(0.05)
            assert not task_service._background_tasks, task_service._background_tasks
            assert "retain-check" not in task_service._running_tasks
            assert "retain-check" not in task_service._cancel_requested
        finally:
            task_service.TaskService.execute_task = orig
            task_service._running_tasks.discard("retain-check")
            task_service._cancel_requested.discard("retain-check")

    asyncio.run(scenario())


def main() -> int:
    tests = [
        test_token_carries_version_and_matches_user,
        test_legacy_token_without_version_is_rejected_after_bump,
        test_spawn_task_retains_strong_reference_until_done,
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
