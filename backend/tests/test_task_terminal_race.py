"""Focused real-SQLite races for task terminal-state arbitration."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.task_service as task_service_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.engines.base import GenerationResult, TaskStatus  # noqa: E402
from app.filestore import asset_exists, reset_store  # noqa: E402
from app.models.models import Asset, Base, Task, Template  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402
from app.thumbs import (  # noqa: E402
    task_lifecycle_key,
    thumb_key,
    thumbnail_lifecycle,
)
from provider_test_utils import lease_acquirer  # noqa: E402


class FakeProvider:
    def __init__(self, result: GenerationResult, output: bytes = b"video-bytes"):
        self.result = result
        self.output = output
        self.cancel_calls: list[str] = []

    async def get_status(self, _prompt_id: str) -> GenerationResult:
        return self.result

    async def download_output(
        self,
        _filename: str,
        _subfolder: str = "",
        _output_type: str = "output",
    ) -> bytes:
        return self.output

    async def cancel(self, prompt_id: str) -> bool:
        self.cancel_calls.append(prompt_id)
        return True


class GatedResultProvider(FakeProvider):
    def __init__(self, result: GenerationResult):
        super().__init__(result)
        self.status_started = asyncio.Event()
        self.release_status = asyncio.Event()

    async def get_status(self, _prompt_id: str) -> GenerationResult:
        self.status_started.set()
        await self.release_status.wait()
        return self.result


class BlockingDoneCommitSession(AsyncSession):
    commit_started: asyncio.Event
    release_commit: asyncio.Event

    async def commit(self) -> None:
        if any(
            isinstance(value, Task) and value.status == "done"
            for value in self.identity_map.values()
        ):
            type(self).commit_started.set()
            await type(self).release_commit.wait()
        await super().commit()


class RecordingCancelCommitSession(AsyncSession):
    commit_started: asyncio.Event

    async def commit(self) -> None:
        type(self).commit_started.set()
        await super().commit()


class GatedSubmitProvider(FakeProvider):
    """submit / get_status 都卡在 release 事件上：测试精确控制 executor 何时放行。"""

    def __init__(self, result: GenerationResult):
        super().__init__(result)
        self.submit_calls: list = []
        self.release_submit = asyncio.Event()

    async def submit(self, workflow, params):
        self.submit_calls.append(workflow)
        await self.release_submit.wait()
        return f"p-{len(self.submit_calls)}"

    async def get_status(self, _prompt_id: str) -> GenerationResult:
        await self.release_submit.wait()
        return self.result


class FlakyStatusProvider(FakeProvider):
    """get_status 先失败 fail_times 次再返回结果；cancel 记账。"""

    def __init__(self, result: GenerationResult, fail_times: int):
        super().__init__(result)
        self.fail_times = fail_times
        self.status_calls = 0
        self.cancel_calls: list[str] = []

    async def get_status(self, prompt_id: str) -> GenerationResult:
        self.status_calls += 1
        if self.status_calls <= self.fail_times:
            raise ConnectionError("engine unreachable (transient)")
        return self.result

    async def cancel(self, prompt_id: str) -> bool:
        self.cancel_calls.append(prompt_id)
        return True


async def _try_acquire_lifecycle_lock(key: str, timeout: float = 0.1) -> bool:
    """能立刻拿到 lifecycle 锁 = 此刻没人持有它。"""
    async def hold() -> None:
        async with thumbnail_lifecycle([key]):
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(hold(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


class EntryCancelGateSession(AsyncSession):
    """在 executor 第一次 commit（pending→running 过渡）处逼并发 cancel 落地。

    - 旧实现（过渡不持锁）：探针能立刻拿到锁 → 等 cancel 完整提交后再放行
      本次 commit —— cancel 的 commit 恰好落在 executor 读/写之间；
    - 新实现（过渡持锁）：探针拿不到锁 → 直接放行，cancel 会在后面串行完成。
    """

    cancel_task: asyncio.Task | None = None
    gate: asyncio.Event | None = None
    lock_key: str = ""
    release_submit: asyncio.Event | None = None

    async def commit(self) -> None:
        cls = type(self)
        ct = cls.cancel_task
        if ct is not None and not ct.done():
            cls.cancel_task = None
            if await _try_acquire_lifecycle_lock(cls.lock_key):
                cls.gate.set()
                await asyncio.wait_for(ct, timeout=2)
                if cls.release_submit is not None:
                    cls.release_submit.set()
            else:
                cls.gate.set()
        await super().commit()


@asynccontextmanager
async def isolated_database(prefix: str):
    original_storage = settings.STORAGE_DIR
    with tempfile.TemporaryDirectory(prefix=prefix) as raw_dir:
        root = Path(raw_dir)
        settings.STORAGE_DIR = root / "storage"
        reset_store()
        engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'test.db'}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            yield engine, sessions
        finally:
            task_service_module._cancel_requested.clear()
            await engine.dispose()
            settings.STORAGE_DIR = original_storage
            reset_store()


async def seed_running_task(sessions, suffix: str) -> tuple[str, str]:
    template_id = f"tpl-{suffix}"
    task_id = f"task-{suffix}"
    async with sessions() as db:
        db.add(Template(
            id=template_id,
            name=f"Template {suffix}",
            engine="fake",
            category="video",
            output_config={},
        ))
        db.add(Task(
            id=task_id,
            template_id=template_id,
            status="running",
            provider_task_id=f"provider-{suffix}",
            params={"prompt": suffix},
            result={},
        ))
        await db.commit()
    return template_id, task_id


async def load_state(sessions, task_id: str) -> tuple[Task, int]:
    async with sessions() as db:
        task = await db.get(Task, task_id)
        count = await db.scalar(
            select(func.count()).select_from(Asset).where(Asset.task_id == task_id)
        )
        assert task is not None
        return task, int(count or 0)


async def test_cancel_wins_while_done_output_save_is_blocked() -> None:
    async with isolated_database("terminal-save-") as (_engine, sessions):
        template_id, task_id = await seed_running_task(sessions, "save")
        provider = FakeProvider(GenerationResult(
            status=TaskStatus.DONE,
            outputs=[{"filename": "clip.mp4", "type": "output"}],
        ))
        save_started = threading.Event()
        release_save = threading.Event()
        real_save = task_service_module.save_asset_file

        def blocking_save(key: str, data: bytes, content_type: str | None = None) -> None:
            if key == f"{task_id}/clip.mp4":
                save_started.set()
                if not release_save.wait(timeout=5):
                    raise RuntimeError("save gate timed out")
            real_save(key, data, content_type)

        with (
            patch.object(task_service_module, "save_asset_file", blocking_save),
            patch.object(task_service_module, "make_video_thumb", lambda *_args: b"thumb"),
            patch.object(
                task_service_module,
                "acquire_provider",
                lease_acquirer(lambda _name: provider),
            ),
        ):
            async with sessions() as finish_db:
                task = await finish_db.get(Task, task_id)
                template = await finish_db.get(Template, template_id)
                assert task is not None and template is not None
                finish = asyncio.create_task(
                    TaskService._finish_task(
                        finish_db,
                        task,
                        template,
                        provider,
                        task.provider_task_id,
                    )
                )
                assert await asyncio.to_thread(save_started.wait, 2), "save did not block"
                try:
                    async with sessions() as cancel_db:
                        cancelled = await asyncio.wait_for(
                            TaskService.cancel_task(cancel_db, task_id),
                            timeout=2,
                        )
                        assert cancelled.status == "cancelled"
                finally:
                    release_save.set()
                await asyncio.wait_for(finish, timeout=3)

        task, asset_count = await load_state(sessions, task_id)
        assert task.status == "cancelled", task.status
        assert asset_count == 0, asset_count
        assert not asset_exists(f"{task_id}/clip.mp4")
        assert not asset_exists(thumb_key(f"{task_id}/clip.mp4"))


async def test_cancel_wins_over_provider_error_completion() -> None:
    async with isolated_database("terminal-error-") as (_engine, sessions):
        template_id, task_id = await seed_running_task(sessions, "error")
        provider = GatedResultProvider(GenerationResult(
            status=TaskStatus.ERROR,
            error="provider failed",
        ))
        with patch.object(
            task_service_module,
            "acquire_provider",
            lease_acquirer(lambda _name: provider),
        ):
            async with sessions() as finish_db:
                task = await finish_db.get(Task, task_id)
                template = await finish_db.get(Template, template_id)
                assert task is not None and template is not None
                finish = asyncio.create_task(
                    TaskService._finish_task(
                        finish_db,
                        task,
                        template,
                        provider,
                        task.provider_task_id,
                    )
                )
                await asyncio.wait_for(provider.status_started.wait(), timeout=2)
                async with sessions() as cancel_db:
                    cancelled = await TaskService.cancel_task(cancel_db, task_id)
                    assert cancelled.status == "cancelled"
                provider.release_status.set()
                await asyncio.wait_for(finish, timeout=2)

        task, asset_count = await load_state(sessions, task_id)
        assert task.status == "cancelled", task.status
        assert task.error_msg == "", task.error_msg
        assert asset_count == 0


async def test_finish_holds_terminal_lock_then_cancel_conflicts() -> None:
    async with isolated_database("terminal-finish-") as (engine, sessions):
        template_id, task_id = await seed_running_task(sessions, "finish")
        provider = FakeProvider(GenerationResult(status=TaskStatus.DONE, outputs=[]))
        BlockingDoneCommitSession.commit_started = asyncio.Event()
        BlockingDoneCommitSession.release_commit = asyncio.Event()
        RecordingCancelCommitSession.commit_started = asyncio.Event()
        finish_sessions = async_sessionmaker(
            engine,
            class_=BlockingDoneCommitSession,
            expire_on_commit=False,
        )
        cancel_sessions = async_sessionmaker(
            engine,
            class_=RecordingCancelCommitSession,
            expire_on_commit=False,
        )

        with patch.object(
            task_service_module,
            "acquire_provider",
            lease_acquirer(lambda _name: provider),
        ):
            async with finish_sessions() as finish_db:
                task = await finish_db.get(Task, task_id)
                template = await finish_db.get(Template, template_id)
                assert task is not None and template is not None
                finish = asyncio.create_task(
                    TaskService._finish_task(
                        finish_db,
                        task,
                        template,
                        provider,
                        task.provider_task_id,
                    )
                )
                await asyncio.wait_for(
                    BlockingDoneCommitSession.commit_started.wait(),
                    timeout=2,
                )

                async with cancel_sessions() as cancel_db:
                    cancel = asyncio.create_task(TaskService.cancel_task(cancel_db, task_id))
                    await asyncio.sleep(0.05)
                    reached_commit_before_finish = (
                        RecordingCancelCommitSession.commit_started.is_set()
                    )
                    BlockingDoneCommitSession.release_commit.set()
                    await asyncio.wait_for(finish, timeout=2)
                    try:
                        await asyncio.wait_for(cancel, timeout=2)
                    except ValueError as exc:
                        conflict = "终态" in str(exc)
                    else:
                        conflict = False

        task, asset_count = await load_state(sessions, task_id)
        assert not reached_commit_before_finish, "cancel committed while finish owned terminal transition"
        assert conflict, "cancel did not return a terminal conflict"
        assert task.status == "done", task.status
        assert asset_count == 0


async def missing_template_cancel_race(*, resume: bool) -> None:
    suffix = "resume-missing" if resume else "execute-missing"
    async with isolated_database(f"terminal-{suffix}-") as (_engine, sessions):
        task_id = f"task-{suffix}"
        async with sessions() as db:
            db.add(Task(
                id=task_id,
                template_id=f"absent-{suffix}",
                status="running" if resume else "pending",
                provider_task_id="provider-missing" if resume else "",
                params={},
                result={},
            ))
            await db.commit()

        provider = FakeProvider(GenerationResult(status=TaskStatus.RUNNING))

        async def cancel_call():
            async with sessions() as db:
                return await TaskService.cancel_task(db, task_id)

        with (
            patch.object(task_service_module, "async_session", sessions),
            patch.object(
                task_service_module,
                "acquire_provider",
                lease_acquirer(lambda _name: provider),
            ),
        ):
            async with thumbnail_lifecycle([task_lifecycle_key(task_id)]):
                cancellation = asyncio.create_task(cancel_call())
                await asyncio.sleep(0.03)
                completion = asyncio.create_task(
                    TaskService.resume_task(task_id)
                    if resume
                    else TaskService.execute_task(task_id)
                )
                await asyncio.sleep(0.05)
                completion_bypassed_lock = completion.done()
            cancel_result, completion_result = await asyncio.gather(
                cancellation,
                completion,
                return_exceptions=True,
            )

        assert not completion_bypassed_lock, "terminal setup failure bypassed task ownership"
        assert isinstance(cancel_result, Task), cancel_result
        assert cancel_result.status == "cancelled", cancel_result.status
        assert completion_result is None, completion_result
        task, asset_count = await load_state(sessions, task_id)
        assert task.status == "cancelled", task.status
        assert task.error_msg == "", task.error_msg
        assert asset_count == 0


async def test_execute_missing_template_serializes_with_cancel() -> None:
    await missing_template_cancel_race(resume=False)


async def test_resume_missing_template_serializes_with_cancel() -> None:
    await missing_template_cancel_race(resume=True)


async def entry_cancel_race(*, resume: bool) -> None:
    """cancel 的 commit 落在 executor「读任务 → 提交 running」之间时，
    最终状态必须是 cancelled —— 旧实现会把已提交的 cancelled 覆盖回 running，
    任务从此卡死在 running（引擎侧还白跑）。"""
    suffix = "resume-entry" if resume else "execute-entry"
    async with isolated_database(f"terminal-{suffix}-") as (engine, sessions):
        _template_id, task_id = await seed_running_task(sessions, suffix)
        async with sessions() as db:
            entry_task = await db.get(Task, task_id)
            # execute：还没提交引擎的 pending；
            # resume：崩溃遗留的 pending + 已提交 provider_task_id
            # （running→running 是 no-op 不会 flush，构不成覆盖竞态，必须 pending）
            entry_task.status = "pending"
            if not resume:
                entry_task.provider_task_id = ""
            await db.commit()

        provider = GatedSubmitProvider(GenerationResult(status=TaskStatus.DONE, outputs=[]))
        gate = asyncio.Event()
        cancel_settled = asyncio.Event()

        async def do_cancel() -> None:
            await gate.wait()
            try:
                async with sessions() as cancel_db:
                    await TaskService.cancel_task(cancel_db, task_id)
            except ValueError:
                pass  # 终态冲突在串行化后是预期内（任务已终态）
            provider.release_submit.set()
            cancel_settled.set()

        cancel_job = asyncio.create_task(do_cancel())
        EntryCancelGateSession.cancel_task = cancel_job
        EntryCancelGateSession.gate = gate
        EntryCancelGateSession.lock_key = task_lifecycle_key(task_id)
        EntryCancelGateSession.release_submit = provider.release_submit
        gate_sessions = async_sessionmaker(
            engine, class_=EntryCancelGateSession, expire_on_commit=False
        )
        try:
            with (
                patch.object(task_service_module, "async_session", gate_sessions),
                patch.object(
                    task_service_module,
                    "acquire_provider",
                    lease_acquirer(lambda _name: provider),
                ),
            ):
                await asyncio.wait_for(
                    TaskService.resume_task(task_id)
                    if resume
                    else TaskService.execute_task(task_id),
                    timeout=3,
                )
            await asyncio.wait_for(cancel_settled.wait(), timeout=2)
        finally:
            EntryCancelGateSession.cancel_task = None
            EntryCancelGateSession.gate = None
            EntryCancelGateSession.release_submit = None
            if not cancel_job.done():
                cancel_job.cancel()

        task, asset_count = await load_state(sessions, task_id)
        assert task.status == "cancelled", f"executor 覆盖了并发取消: {task.status}"
        assert asset_count == 0, asset_count


async def test_execute_entry_cannot_overwrite_committed_cancel() -> None:
    await entry_cancel_race(resume=False)


async def test_resume_entry_cannot_overwrite_committed_cancel() -> None:
    await entry_cancel_race(resume=True)


async def test_transient_status_failure_does_not_fail_task() -> None:
    """get_status 瞬时失败（网络抖动）不得判任务失败：引擎侧还在正常跑。"""
    async with isolated_database("poll-flaky-") as (_engine, sessions):
        template_id, task_id = await seed_running_task(sessions, "flaky")
        provider = FlakyStatusProvider(
            GenerationResult(status=TaskStatus.DONE, outputs=[]),
            fail_times=2,
        )
        with (
            patch.object(
                task_service_module,
                "acquire_provider",
                lease_acquirer(lambda _name: provider),
            ),
            patch.object(settings, "TASK_POLL_INTERVAL", 0.05),
        ):
            async with sessions() as finish_db:
                task = await finish_db.get(Task, task_id)
                template = await finish_db.get(Template, template_id)
                assert task is not None and template is not None
                await TaskService._finish_task(
                    finish_db, task, template, provider, task.provider_task_id
                )

        task, _asset_count = await load_state(sessions, task_id)
        assert task.status == "done", (task.status, task.error_msg)
        assert provider.status_calls == 3, provider.status_calls


async def test_persistent_status_failure_cancels_engine_then_fails_task() -> None:
    """get_status 持续失败超过宽限窗口：必须先 cancel 引擎侧 prompt（不留孤儿），
    再把任务判 error。"""
    async with isolated_database("poll-dead-") as (_engine, sessions):
        template_id, task_id = await seed_running_task(sessions, "dead")
        provider = FlakyStatusProvider(
            GenerationResult(status=TaskStatus.DONE, outputs=[]),
            fail_times=10**9,
        )
        with (
            patch.object(
                task_service_module,
                "acquire_provider",
                lease_acquirer(lambda _name: provider),
            ),
            patch.object(task_service_module, "_POLL_FAIL_GRACE_SECONDS", 0.2),
            patch.object(settings, "TASK_POLL_INTERVAL", 0.05),
        ):
            async with sessions() as finish_db:
                task = await finish_db.get(Task, task_id)
                template = await finish_db.get(Template, template_id)
                assert task is not None and template is not None
                await TaskService._finish_task(
                    finish_db, task, template, provider, task.provider_task_id
                )

        task, _asset_count = await load_state(sessions, task_id)
        assert task.status == "error", (task.status, task.error_msg)
        assert provider.cancel_calls == [task.provider_task_id], provider.cancel_calls


async def main() -> int:
    tests = [
        test_cancel_wins_while_done_output_save_is_blocked,
        test_cancel_wins_over_provider_error_completion,
        test_finish_holds_terminal_lock_then_cancel_conflicts,
        test_execute_missing_template_serializes_with_cancel,
        test_resume_missing_template_serializes_with_cancel,
        test_execute_entry_cannot_overwrite_committed_cancel,
        test_resume_entry_cannot_overwrite_committed_cancel,
        test_transient_status_failure_does_not_fail_task,
        test_persistent_status_failure_cancels_engine_then_fails_task,
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
