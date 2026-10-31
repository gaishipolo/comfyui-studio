"""Async API mapping for missing versus operational FileStore failures."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import filestore  # noqa: E402
from app.api import tasks as tasks_api  # noqa: E402
from app.filestore import MinioFileStore  # noqa: E402
from app.models.models import Asset  # noqa: E402


FileStoreUnavailableError = getattr(
    filestore,
    "FileStoreUnavailableError",
    RuntimeError,
)
USER = SimpleNamespace(id="owner", role="member")


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class AssetDB:
    def __init__(self, asset):
        self.asset = asset

    async def execute(self, _query):
        return ScalarResult(self.asset)


class BlockingStatClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def stat_object(self, _bucket: str, _key: str):
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test stat release timed out")
        error = RuntimeError("invalid credentials")
        error.code = "AccessDenied"
        raise error


def make_asset() -> SimpleNamespace:
    return SimpleNamespace(
        id="asset-id",
        task_id="task-id",
        file_path="task-id/file.png",
        file_name="file.png",
        file_size=4,
        mime_type="image/png",
        is_shared=False,
    )


async def owner_map(_db, task_ids: set[str]) -> dict[str, str]:
    return {task_id: "owner" for task_id in task_ids}


async def test_slow_minio_stat_stays_off_loop_and_maps_auth_failure_to_503() -> None:
    client = BlockingStatClient()
    store = MinioFileStore(client=client)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        assert await asyncio.to_thread(client.started.wait, 1), "stat did not start"
        for _ in range(4):
            ticks += 1
            await asyncio.sleep(0.01)
        client.release.set()

    async def operation() -> HTTPException:
        try:
            await tasks_api.download_asset(
                "task-id",
                "file.png",
                user=USER,
                db=AssetDB(make_asset()),
            )
        except HTTPException as exc:
            return exc
        raise AssertionError("operational FileStore failure did not fail")

    with (
        patch.object(tasks_api, "open_asset", store.open),
        patch.object(tasks_api, "task_owner_map", owner_map),
    ):
        error, _ = await asyncio.wait_for(
            asyncio.gather(operation(), ticker()),
            timeout=3,
        )

    assert ticks == 4, ticks
    assert error.status_code == 503, (error.status_code, error.detail)
    assert error.detail == "文件存储服务暂时不可用", error.detail


async def test_thumbnail_operational_failure_is_503_not_404() -> None:
    asset = make_asset()

    def unavailable(_path: str):
        raise FileStoreUnavailableError("backend unavailable")

    with (
        patch.object(tasks_api, "open_asset", unavailable),
        patch.object(tasks_api, "task_owner_map", owner_map),
    ):
        try:
            await tasks_api.download_asset_thumb(
                "task-id",
                "file.png",
                SimpleNamespace(headers={}),
                user=USER,
                db=AssetDB(asset),
            )
        except HTTPException as exc:
            error = exc
        else:
            raise AssertionError("thumbnail FileStore failure did not fail")

    assert error.status_code == 503, (error.status_code, error.detail)
    assert error.detail == "文件存储服务暂时不可用", error.detail


async def main() -> int:
    tests = [
        test_slow_minio_stat_stays_off_loop_and_maps_auth_failure_to_503,
        test_thumbnail_operational_failure_is_503_not_404,
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
