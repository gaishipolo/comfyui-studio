# -*- coding: utf-8 -*-
"""Focused in-process checks for Provider retirement and shutdown."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import factory  # noqa: E402
from app.engines.base import GenerationProvider  # noqa: E402


class _ProviderStub(GenerationProvider):
    async def submit(self, ctx: dict, params: dict) -> str:
        raise NotImplementedError

    async def get_status(self, task_id: str):
        raise NotImplementedError

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True


class _BlockingProvider(_ProviderStub):
    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()


class _NeverClosingProvider(_ProviderStub):
    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.close_cancelled.set()


class _RecordingProvider(_ProviderStub):
    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.closed.set()


class ProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_all_times_out_a_provider_that_never_closes(self):
        engine_name = "lifecycle_never_closes"
        missing = object()
        original_timeout = getattr(factory, "_CLOSE_TIMEOUT_SECONDS", missing)
        factory._CLOSE_TIMEOUT_SECONDS = 0.02
        factory.register_provider(engine_name, _NeverClosingProvider)
        provider = factory.get_provider(engine_name)

        try:
            with self.assertLogs(factory.__name__, level="WARNING"):
                await asyncio.wait_for(factory.close_all_providers(), timeout=0.5)
            self.assertTrue(provider.close_started.is_set())
            self.assertTrue(provider.close_cancelled.is_set())
        finally:
            factory.unregister_provider(engine_name)
            if original_timeout is missing:
                del factory._CLOSE_TIMEOUT_SECONDS
            else:
                factory._CLOSE_TIMEOUT_SECONDS = original_timeout

    async def test_lifespan_closes_providers_when_startup_fails(self):
        from app import main as app_main

        engine_name = "lifecycle_startup_failure"
        factory.register_provider(engine_name, _RecordingProvider)
        provider = factory.get_provider(engine_name)
        original_init_db = app_main.init_db

        class StartupFailure(RuntimeError):
            pass

        async def fail_startup() -> None:
            raise StartupFailure("expected startup failure")

        app_main.init_db = fail_startup
        try:
            with self.assertRaises(StartupFailure):
                async with app_main.lifespan(app_main.app):
                    self.fail("lifespan yielded after startup failed")
            self.assertTrue(provider.closed.is_set())
        finally:
            app_main.init_db = original_init_db
            if not provider.closed.is_set():
                await factory.close_all_providers()
            factory.unregister_provider(engine_name)

    async def test_retirement_is_tracked_until_close_finishes_and_joined_on_shutdown(self):
        engine_name = "lifecycle_retirement"
        factory.register_provider(engine_name, _BlockingProvider)
        provider = factory.get_provider(engine_name)
        shutdown_task = None

        try:
            factory.set_engine_config(engine_name, {"version": 2})
            await asyncio.wait_for(provider.close_started.wait(), timeout=0.5)

            self.assertEqual(1, len(factory._retire_tasks))
            self.assertTrue(all(not task.done() for task in factory._retire_tasks))

            shutdown_task = asyncio.create_task(factory.close_all_providers())
            await asyncio.sleep(0)
            self.assertFalse(shutdown_task.done())

            provider.release_close.set()
            await asyncio.wait_for(shutdown_task, timeout=0.5)
            await asyncio.sleep(0)
            self.assertFalse(factory._retire_tasks)
        finally:
            provider.release_close.set()
            if shutdown_task is not None:
                await asyncio.gather(shutdown_task, return_exceptions=True)
            await asyncio.sleep(0)
            factory.unregister_provider(engine_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
