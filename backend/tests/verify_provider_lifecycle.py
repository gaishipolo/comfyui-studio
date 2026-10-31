# -*- coding: utf-8 -*-
"""Focused in-process checks for Provider retirement and shutdown."""

import asyncio
import gc
import sys
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

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


class _SyncRetirementProvider(_ProviderStub):
    name = "lifecycle_sync_retirement"
    close_calls = 0

    async def close(self) -> None:
        type(self).close_calls += 1


class _RaisingProvider(_ProviderStub):
    name = "lifecycle_close_raises"

    async def close(self) -> None:
        raise RuntimeError("close exploded")


class _LeaseProvider(_ProviderStub):
    name = "lifecycle_lease"
    instances: list["_LeaseProvider"] = []

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.close_calls = 0
        self.closed = asyncio.Event()
        self.use_started = asyncio.Event()
        self.release_use = asyncio.Event()
        type(self).instances.append(self)

    async def blocked_use(self) -> bool:
        self.use_started.set()
        await self.release_use.wait()
        return self.close_calls == 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class _LeaseReplacementProvider(_LeaseProvider):
    name = "lifecycle_lease_replacement"
    instances: list["_LeaseReplacementProvider"] = []


class _AsyncSessionStub:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ProviderLifecycleSyncTests(unittest.TestCase):
    def test_sync_retirement_is_retained_until_later_shutdown(self):
        engine_name = _SyncRetirementProvider.name
        _SyncRetirementProvider.close_calls = 0
        factory.register_provider(engine_name, _SyncRetirementProvider)
        provider = factory.get_provider(engine_name)
        provider_ref = weakref.ref(provider)

        try:
            factory.set_engine_config(engine_name, {"version": 2})
            del provider
            gc.collect()
            retained = provider_ref() is not None

            asyncio.run(factory.close_all_providers())

            self.assertTrue(retained)
            self.assertEqual(1, _SyncRetirementProvider.close_calls)
            self.assertFalse(factory._pending_retirements)
        finally:
            factory.unregister_provider(engine_name)
            asyncio.run(factory.close_all_providers())


class ProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def acquire(self, engine_name: str):
        acquire_provider = getattr(factory, "acquire_provider", None)
        self.assertTrue(callable(acquire_provider), "factory.acquire_provider is missing")
        return acquire_provider(engine_name)

    async def _wait_closed(self, provider: _LeaseProvider) -> None:
        await asyncio.wait_for(provider.closed.wait(), timeout=0.5)
        await asyncio.sleep(0)

    async def test_replacement_waits_for_active_lease_then_closes_once(self):
        engine_name = "lifecycle_leased_replacement"
        _LeaseProvider.instances.clear()
        _LeaseReplacementProvider.instances.clear()
        factory.register_provider(engine_name, _LeaseProvider)

        async def active_use():
            async with self.acquire(engine_name) as provider:
                usable = await provider.blocked_use()
                self.assertTrue(usable)

        use = asyncio.create_task(active_use())
        old = None
        try:
            while not _LeaseProvider.instances and not use.done():
                await asyncio.sleep(0)
            if use.done():
                await use
            old = _LeaseProvider.instances[-1]
            await asyncio.wait_for(old.use_started.wait(), timeout=0.5)

            factory.register_provider(engine_name, _LeaseReplacementProvider)
            await asyncio.sleep(0)
            new = factory.get_provider(engine_name)

            self.assertIsNot(new, old)
            self.assertIsInstance(new, _LeaseReplacementProvider)
            self.assertEqual(0, old.close_calls)
            old.release_use.set()
            await asyncio.wait_for(use, timeout=0.5)
            await self._wait_closed(old)
            self.assertEqual(1, old.close_calls)

            await factory.close_all_providers()
            self.assertEqual(1, old.close_calls)
        finally:
            if old is not None:
                old.release_use.set()
            await asyncio.gather(use, return_exceptions=True)
            factory.unregister_provider(engine_name)
            await factory.close_all_providers()

    async def test_config_change_waits_for_active_lease_and_new_get_uses_config(self):
        engine_name = "lifecycle_leased_config"
        _LeaseProvider.instances.clear()
        factory.register_provider(engine_name, _LeaseProvider)
        factory.set_engine_config(engine_name, {"version": 1})
        lease = self.acquire(engine_name)
        old = _LeaseProvider.instances[-1]

        try:
            factory.set_engine_config(engine_name, {"version": 2})
            await asyncio.sleep(0)
            new = factory.get_provider(engine_name)
            self.assertIsNot(new, old)
            self.assertEqual(2, new.config["version"])
            self.assertEqual(0, old.close_calls)

            async with lease as leased:
                self.assertIs(leased, old)
                self.assertEqual(0, leased.close_calls)
            await self._wait_closed(old)
            self.assertEqual(1, old.close_calls)
        finally:
            factory.unregister_provider(engine_name)
            await factory.close_all_providers()

    async def test_disable_waits_for_active_lease_and_rejects_new_get(self):
        engine_name = "lifecycle_leased_disable"
        _LeaseProvider.instances.clear()
        factory.register_provider(engine_name, _LeaseProvider)
        lease = self.acquire(engine_name)
        old = _LeaseProvider.instances[-1]

        try:
            factory.unregister_provider(engine_name)
            await asyncio.sleep(0)
            with self.assertRaises(ValueError):
                factory.get_provider(engine_name)
            self.assertEqual(0, old.close_calls)

            async with lease as leased:
                self.assertIs(leased, old)
                self.assertEqual(0, leased.close_calls)
            await self._wait_closed(old)
            self.assertEqual(1, old.close_calls)
        finally:
            factory.unregister_provider(engine_name)
            await factory.close_all_providers()

    async def test_shutdown_waits_for_active_lease_without_duplicate_close(self):
        engine_name = "lifecycle_leased_shutdown"
        _LeaseProvider.instances.clear()
        factory.register_provider(engine_name, _LeaseProvider)
        lease = self.acquire(engine_name)
        old = _LeaseProvider.instances[-1]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def active_use():
            async with lease as provider:
                entered.set()
                await release.wait()
                self.assertEqual(0, provider.close_calls)

        use = asyncio.create_task(active_use())
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            shutdown = asyncio.create_task(factory.close_all_providers())
            await asyncio.sleep(0.02)
            self.assertFalse(shutdown.done())
            self.assertEqual(0, old.close_calls)

            release.set()
            await asyncio.wait_for(use, timeout=0.5)
            await asyncio.wait_for(shutdown, timeout=0.5)
            self.assertEqual(1, old.close_calls)
        finally:
            release.set()
            await asyncio.gather(use, return_exceptions=True)
            if shutdown is not None:
                await asyncio.gather(shutdown, return_exceptions=True)
            factory.unregister_provider(engine_name)
            await factory.close_all_providers()

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

    async def test_lifespan_closes_providers_when_runtime_fails(self):
        from app import main as app_main
        from app.api import plugins
        from app.services.task_service import TaskService

        engine_name = "lifecycle_runtime_failure"
        factory.register_provider(engine_name, _RecordingProvider)
        provider = factory.get_provider(engine_name)

        class RuntimeFailure(RuntimeError):
            pass

        async def no_startup_work() -> None:
            return None

        async def no_plugins(_db) -> list[dict]:
            return []

        async def no_stale_tasks() -> dict:
            return {"resumed": [], "failed": []}

        try:
            with (
                patch.object(app_main, "init_db", no_startup_work),
                patch.object(app_main, "create_default_admin", no_startup_work),
                patch.object(app_main, "async_session", lambda: _AsyncSessionStub()),
                patch.object(plugins, "restore_plugins", no_plugins),
                patch.object(TaskService, "reap_stale_tasks", staticmethod(no_stale_tasks)),
            ):
                with self.assertRaises(RuntimeFailure):
                    async with app_main.lifespan(app_main.app):
                        raise RuntimeFailure("expected runtime failure")
            self.assertTrue(provider.closed.is_set())
        finally:
            if not provider.closed.is_set():
                await factory.close_all_providers()
            factory.unregister_provider(engine_name)

    async def test_close_failure_keeps_registry_change_and_logs_engine_name(self):
        engine_name = _RaisingProvider.name
        factory.register_provider(engine_name, _RaisingProvider)
        factory.get_provider(engine_name)

        try:
            with self.assertLogs(factory.__name__, level="ERROR") as captured:
                factory.unregister_provider(engine_name)
                await factory.close_all_providers()

            self.assertNotIn(engine_name, factory.list_engines())
            self.assertNotIn(engine_name, factory._providers)
            log_output = "\n".join(captured.output)
            self.assertIn(engine_name, log_output)
            self.assertIn("close exploded", log_output)
        finally:
            factory.unregister_provider(engine_name)
            await factory.close_all_providers()

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
