"""Focused checks that real Provider-use boundaries retain factory leases."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import health as health_api  # noqa: E402
from app.api import uploads as uploads_api  # noqa: E402
from app.engines import factory  # noqa: E402
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus  # noqa: E402
from app.models.models import Base, Task, Template  # noqa: E402
from app.services import task_service as task_service_module  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402


class BlockingUseProvider(GenerationProvider):
    instances: list["BlockingUseProvider"] = []

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.upload_started = asyncio.Event()
        self.release_upload = asyncio.Event()
        self.health_started = asyncio.Event()
        self.release_health = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        type(self).instances.append(self)

    async def submit(self, _ctx: dict, _params: dict) -> str:
        self.submit_started.set()
        await self.release_submit.wait()
        return "provider-task"

    async def get_status(self, _task_id: str) -> GenerationResult:
        return GenerationResult(status=TaskStatus.ERROR, error="expected terminal error")

    async def download_output(
        self,
        _filename: str,
        _subfolder: str = "",
        _output_type: str = "output",
    ) -> bytes:
        return b""

    async def upload_input(self, _data: bytes, filename: str) -> str:
        self.upload_started.set()
        await self.release_upload.wait()
        return filename

    async def health_check(self) -> bool:
        self.health_started.set()
        await self.release_health.wait()
        return True

    async def cancel(self, _provider_task_id: str) -> bool:
        self.cancel_started.set()
        await self.release_cancel.wait()
        return True

    async def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


@asynccontextmanager
async def registered(engine_name: str):
    BlockingUseProvider.instances.clear()
    factory.register_provider(engine_name, BlockingUseProvider)
    factory.set_engine_config(engine_name, {"version": 1})
    try:
        yield
    finally:
        for provider in BlockingUseProvider.instances:
            provider.release_submit.set()
            provider.release_upload.set()
            provider.release_health.set()
            provider.release_cancel.set()
        factory.unregister_provider(engine_name)
        await factory.close_all_providers()


async def replace_while_blocked(engine_name: str, old: BlockingUseProvider):
    factory.set_engine_config(engine_name, {"version": 2})
    await asyncio.sleep(0)
    new = factory.get_provider(engine_name)
    assert new is not old
    assert new.config["version"] == 2
    assert old.close_calls == 0, "active Provider closed before use released its lease"
    return new


async def test_task_execution_holds_one_lease_through_submit_poll_and_finish() -> None:
    engine_name = "lease_task_boundary"
    async with registered(engine_name):
        with tempfile.TemporaryDirectory(prefix="lease-task-") as raw_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(raw_dir) / 'task.db'}"
            )
            sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                db.add(Template(
                    id="lease-template",
                    name="Lease Template",
                    engine=engine_name,
                    category="image",
                ))
                db.add(Task(
                    id="lease-task",
                    template_id="lease-template",
                    status="pending",
                    params={},
                    result={},
                ))
                await db.commit()

            original_session = task_service_module.async_session
            task_service_module.async_session = sessions
            execution = asyncio.create_task(TaskService.execute_task("lease-task"))
            try:
                while not BlockingUseProvider.instances:
                    await asyncio.sleep(0)
                old = BlockingUseProvider.instances[0]
                await asyncio.wait_for(old.submit_started.wait(), timeout=1)
                await replace_while_blocked(engine_name, old)
                old.release_submit.set()
                await asyncio.wait_for(execution, timeout=2)
                await asyncio.wait_for(old.closed.wait(), timeout=1)
                assert old.close_calls == 1
                async with sessions() as db:
                    task = await db.get(Task, "lease-task")
                    assert task is not None and task.status == "error"
            finally:
                old = BlockingUseProvider.instances[0] if BlockingUseProvider.instances else None
                if old is not None:
                    old.release_submit.set()
                await asyncio.gather(execution, return_exceptions=True)
                task_service_module.async_session = original_session
                await engine.dispose()


async def test_upload_forwarding_holds_lease() -> None:
    engine_name = "lease_upload_boundary"
    async with registered(engine_name):
        upload = asyncio.create_task(
            uploads_api._forward_to_engine(engine_name, b"input", "input.png")
        )
        while not BlockingUseProvider.instances:
            await asyncio.sleep(0)
        old = BlockingUseProvider.instances[0]
        await asyncio.wait_for(old.upload_started.wait(), timeout=1)
        await replace_while_blocked(engine_name, old)
        old.release_upload.set()
        assert await asyncio.wait_for(upload, timeout=1) == "input.png"
        await asyncio.wait_for(old.closed.wait(), timeout=1)
        assert old.close_calls == 1


async def test_health_probe_holds_lease() -> None:
    engine_name = "lease_health_boundary"
    async with registered(engine_name):
        probe = asyncio.create_task(health_api._probe(engine_name))
        while not BlockingUseProvider.instances:
            await asyncio.sleep(0)
        old = BlockingUseProvider.instances[0]
        await asyncio.wait_for(old.health_started.wait(), timeout=1)
        await replace_while_blocked(engine_name, old)
        old.release_health.set()
        result = await asyncio.wait_for(probe, timeout=1)
        assert result == {"name": engine_name, "status": "connected"}, result
        await asyncio.wait_for(old.closed.wait(), timeout=1)
        assert old.close_calls == 1


async def test_task_cancellation_holds_lease_while_notifying_provider() -> None:
    engine_name = "lease_cancel_boundary"
    async with registered(engine_name):
        with tempfile.TemporaryDirectory(prefix="lease-cancel-") as raw_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(raw_dir) / 'cancel.db'}"
            )
            sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with sessions() as db:
                    db.add(Template(
                        id="cancel-template",
                        name="Cancel Lease Template",
                        engine=engine_name,
                        category="image",
                    ))
                    db.add(Task(
                        id="cancel-task",
                        template_id="cancel-template",
                        status="running",
                        provider_task_id="provider-cancel",
                        params={},
                        result={},
                    ))
                    await db.commit()

                async with sessions() as db:
                    cancellation = asyncio.create_task(
                        TaskService.cancel_task(db, "cancel-task")
                    )
                    old = None
                    try:
                        while not BlockingUseProvider.instances:
                            await asyncio.sleep(0)
                        old = BlockingUseProvider.instances[0]
                        await asyncio.wait_for(old.cancel_started.wait(), timeout=1)
                        await replace_while_blocked(engine_name, old)
                        old.release_cancel.set()
                        cancelled = await asyncio.wait_for(cancellation, timeout=1)
                        assert cancelled.status == "cancelled"
                        await asyncio.wait_for(old.closed.wait(), timeout=1)
                        assert old.close_calls == 1
                    finally:
                        if old is not None:
                            old.release_cancel.set()
                        await asyncio.gather(cancellation, return_exceptions=True)
            finally:
                task_service_module._cancel_requested.discard("cancel-task")
                await engine.dispose()


async def main() -> int:
    tests = [
        test_task_execution_holds_one_lease_through_submit_poll_and_finish,
        test_upload_forwarding_holds_lease,
        test_health_probe_holds_lease,
        test_task_cancellation_holds_lease_while_notifying_provider,
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
