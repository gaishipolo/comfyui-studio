"""Offline verification for bounded, temporary-file batch archives.

Uses real Asset/ObjectHandle/FileResponse behavior with fake database and FileStore
boundaries. No application database or configured storage is touched.
"""
from __future__ import annotations

import asyncio
import gc
import io
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.responses import FileResponse

import app.api.assets as assets_api
import app.filestore as filestore
from app.filestore import ObjectHandle
from app.models.models import Asset


PASS = 0
FAIL = 0
_MISSING = object()
FileStoreUnavailableError = getattr(
    filestore,
    "FileStoreUnavailableError",
    RuntimeError,
)


def report(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    suffix = f"  {detail}" if detail else ""
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{suffix}")


async def run_case(name: str, test) -> None:
    try:
        await test()
    except BaseException as exc:  # Keep running so RED reports every missing behavior.
        report(name, False, f"{type(exc).__name__}: {exc}")
    else:
        report(name, True)


@contextmanager
def patched(obj: object, **changes: Any) -> Iterator[None]:
    previous = {name: getattr(obj, name, _MISSING) for name in changes}
    for name, value in changes.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, value)


@contextmanager
def temporary_archive_dir() -> Iterator[Path]:
    previous = tempfile.tempdir
    with tempfile.TemporaryDirectory() as raw_dir:
        root = Path(raw_dir)
        tempfile.tempdir = raw_dir
        try:
            yield root
        finally:
            tempfile.tempdir = previous


def make_asset(
    asset_id: str,
    file_name: str,
    *,
    task_id: str = "task-owner-0001",
    file_size: int = 0,
) -> Asset:
    return Asset(
        id=asset_id,
        task_id=task_id,
        file_path=f"{task_id}/{asset_id}.bin",
        file_name=file_name,
        file_size=file_size,
        mime_type="application/octet-stream",
        tags=[],
        meta={},
        is_shared=False,
    )


class FakeScalars:
    def __init__(self, assets: list[Asset]):
        self.assets = assets

    def all(self) -> list[Asset]:
        return self.assets


class FakeResult:
    def __init__(self, assets: list[Asset]):
        self.assets = assets

    def scalars(self) -> FakeScalars:
        return FakeScalars(self.assets)


class FakeDB:
    def __init__(self, assets: list[Asset], *, fail_on_execute: bool = False):
        self.assets = assets
        self.fail_on_execute = fail_on_execute
        self.execute_calls = 0

    async def execute(self, _query) -> FakeResult:
        self.execute_calls += 1
        if self.fail_on_execute:
            raise AssertionError("database work must not run")
        return FakeResult(self.assets)


class ReportedSizeBytes(bytes):
    """Tiny buffer with a synthetic len, avoiding a 1 GiB test allocation."""

    def __new__(cls, data: bytes, reported_size: int):
        value = super().__new__(cls, data)
        value.reported_size = reported_size
        return value

    def __len__(self) -> int:
        return self.reported_size


async def owner_map(_db, task_ids: set[str]) -> dict[str, str]:
    return {task_id: "owner" for task_id in task_ids}


ADMIN = SimpleNamespace(id="admin", role="admin")


def builder():
    value = getattr(assets_api, "_build_batch_archive", None)
    if not callable(value):
        raise AssertionError("_build_batch_archive is missing")
    return value


def cleanup(path: Path) -> None:
    value = getattr(assets_api, "_remove_temp_archive", None)
    if not callable(value):
        raise AssertionError("_remove_temp_archive is missing")
    value(path)


def handle_factory(
    payloads: dict[str, list[bytes]],
    closed: list[str],
):
    def open_asset(file_path: str) -> ObjectHandle | None:
        chunks = payloads.get(file_path)
        if chunks is None:
            return None
        return ObjectHandle(
            size=sum(len(chunk) for chunk in chunks),
            _stream=iter(chunks),
            _cleanup=lambda: closed.append(file_path),
        )

    return open_asset


async def expect_http(coro, status: int, detail: str) -> HTTPException:
    try:
        await coro
    except HTTPException as exc:
        assert exc.status_code == status, (exc.status_code, exc.detail)
        assert detail in str(exc.detail), exc.detail
        return exc
    raise AssertionError(f"expected HTTP {status}")


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


async def test_count_limit_precedes_database_and_files() -> None:
    db = FakeDB([], fail_on_execute=True)

    def forbidden_file_work(_path: str):
        raise AssertionError("file work must not run")

    with patched(
        assets_api,
        open_asset=forbidden_file_work,
        read_asset_bytes=forbidden_file_work,
    ):
        await expect_http(
            assets_api.batch_download(
                [f"asset-{index}" for index in range(51)],
                user=ADMIN,
                db=db,
            ),
            400,
            "50",
        )
    assert db.execute_calls == 0


async def test_count_limit_allows_exactly_50_ids() -> None:
    db = FakeDB([])
    await expect_http(
        assets_api.batch_download(
            [f"asset-{index}" for index in range(50)],
            user=ADMIN,
            db=db,
        ),
        404,
        "No matching assets",
    )
    assert db.execute_calls == 1


async def test_metadata_limit_precedes_archive_creation() -> None:
    asset = make_asset(
        "asset-large-metadata",
        "large.bin",
        file_size=1_073_741_825,
    )
    db = FakeDB([asset])
    build_calls = 0
    file_calls = 0

    def forbidden_builder(_assets) -> Path:
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("archive creation must not run")

    def forbidden_file_work(_path: str):
        nonlocal file_calls
        file_calls += 1
        raise AssertionError("file work must not run")

    with patched(
        assets_api,
        _build_batch_archive=forbidden_builder,
        open_asset=forbidden_file_work,
        read_asset_bytes=forbidden_file_work,
        task_owner_map=owner_map,
    ):
        await expect_http(
            assets_api.batch_download([asset.id], user=ADMIN, db=db),
            400,
            "1024MB",
        )

    assert db.execute_calls == 1
    assert build_calls == 0
    assert file_calls == 0


async def test_metadata_limit_allows_exactly_1_gib() -> None:
    asset = make_asset(
        "asset-metadata-boundary",
        "metadata-boundary.bin",
        file_size=1_073_741_824,
    )
    with temporary_archive_dir() as root:
        response, closed = await make_response(asset, b"metadata-boundary")
        path = Path(response.path)
        assert path.exists() and path.parent == root
        assert closed == [asset.file_path]
        assert response.background is not None
        await response.background()
        assert not path.exists()
        assert list(root.iterdir()) == []


async def test_actual_size_limit_removes_partial_and_closes_handle() -> None:
    asset = make_asset("asset-stream-limit", "stream.bin")
    closed: list[str] = []
    payloads = {
        asset.file_path: [
            ReportedSizeBytes(b"a", 1_073_741_824),
            ReportedSizeBytes(b"b", 1),
        ]
    }

    with temporary_archive_dir() as root, patched(
        assets_api,
        open_asset=handle_factory(payloads, closed),
        read_asset_bytes=lambda _path: (_ for _ in ()).throw(
            AssertionError("whole-file reads are forbidden")
        ),
    ):
        try:
            builder()([asset])
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("actual bytes above the limit were accepted")

        assert closed == [asset.file_path]
        assert list(root.iterdir()) == [], list(root.iterdir())


async def test_actual_size_limit_allows_exactly_1_gib() -> None:
    asset = make_asset("asset-stream-boundary", "stream-boundary.bin")
    closed: list[str] = []
    payloads = {
        asset.file_path: [ReportedSizeBytes(b"a", 1_073_741_824)],
    }

    with temporary_archive_dir() as root, patched(
        assets_api,
        open_asset=handle_factory(payloads, closed),
        read_asset_bytes=lambda _path: (_ for _ in ()).throw(
            AssertionError("whole-file reads are forbidden")
        ),
    ):
        path = builder()([asset])
        assert path.exists() and path.parent == root
        assert closed == [asset.file_path]
        cleanup(path)
        assert list(root.iterdir()) == []


async def test_archive_names_are_safe_unique_basenames() -> None:
    assets = [
        make_asset("asset-one", "../same.txt", task_id="task-duplicate"),
        make_asset("asset-two", r"..\same.txt", task_id="task-duplicate"),
        make_asset("asset-three", "nested/../../same.txt", task_id="task-duplicate"),
        make_asset("asset-four", r"C:\unsafe\other.bin", task_id="task-other"),
    ]
    contents = [b"one", b"two", b"three", b"four"]
    payloads = {
        asset.file_path: [contents[index][:1], contents[index][1:]]
        for index, asset in enumerate(assets)
    }
    closed: list[str] = []

    with temporary_archive_dir() as root, patched(
        assets_api,
        open_asset=handle_factory(payloads, closed),
        read_asset_bytes=lambda _path: (_ for _ in ()).throw(
            AssertionError("whole-file reads are forbidden")
        ),
    ):
        path = builder()(assets)
        assert path.parent == root
        assert path.exists()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names)) == 4, names
            assert names[0] == "same.txt", names
            assert names[1].startswith("task-dup_same"), names
            assert all(
                name not in {"", ".", ".."}
                and "/" not in name
                and "\\" not in name
                for name in names
            ), names
            assert [archive.read(name) for name in names] == contents

        assert closed == [asset.file_path for asset in assets]
        cleanup(path)
        assert list(root.iterdir()) == []


async def test_missing_selected_member_fails_and_removes_partial_archive() -> None:
    present = make_asset("asset-present", "present.bin")
    missing = make_asset("asset-missing", "missing.bin")
    closed: list[str] = []
    open_asset = handle_factory({present.file_path: [b"present"]}, closed)

    with temporary_archive_dir() as root, patched(
        assets_api,
        open_asset=open_asset,
    ):
        try:
            builder()([present, missing])
        except HTTPException as exc:
            assert exc.status_code == 404, exc
            assert exc.detail == "Selected asset file is missing", exc.detail
        else:
            raise AssertionError("missing selected member was silently omitted")

        assert closed == [present.file_path]
        assert list(root.iterdir()) == [], list(root.iterdir())


async def test_filestore_failure_maps_to_503_and_cleans_partial_archive() -> None:
    present = make_asset("asset-operational", "operational.bin")
    db = FakeDB([present])

    def unavailable(_path: str):
        raise FileStoreUnavailableError("raw backend failure")

    with temporary_archive_dir() as root, patched(
        assets_api,
        open_asset=unavailable,
        task_owner_map=owner_map,
    ):
        await expect_http(
            assets_api.batch_download([present.id], user=ADMIN, db=db),
            503,
            "文件存储服务暂时不可用",
        )
        assert list(root.iterdir()) == [], list(root.iterdir())


async def call_response(response: FileResponse, send) -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/assets/batch-download",
        "raw_path": b"/api/assets/batch-download",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }

    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    await response(scope, receive, send)


async def make_response(asset: Asset, payload: bytes) -> tuple[FileResponse, list[str]]:
    closed: list[str] = []
    db = FakeDB([asset])
    open_asset = handle_factory({asset.file_path: [payload[:2], payload[2:]]}, closed)
    with patched(
        assets_api,
        open_asset=open_asset,
        read_asset_bytes=lambda _path: (_ for _ in ()).throw(
            AssertionError("whole-file reads are forbidden")
        ),
        task_owner_map=owner_map,
    ):
        response = await assets_api.batch_download([asset.id], user=ADMIN, db=db)
    assert isinstance(response, FileResponse), type(response)
    return response, closed


async def test_file_response_streams_off_thread_and_cleans_up() -> None:
    asset = make_asset("asset-success", "success.bin")
    build_thread_ids: list[int] = []
    main_thread_id = threading.get_ident()
    original_builder = builder()

    def recording_builder(selected_assets) -> Path:
        build_thread_ids.append(threading.get_ident())
        return original_builder(selected_assets)

    with temporary_archive_dir() as root, patched(
        assets_api,
        _build_batch_archive=recording_builder,
    ):
        response, closed = await make_response(asset, b"streamed-payload")
        path = Path(response.path)
        assert path.exists() and path.parent == root
        assert response.background is not None
        assert build_thread_ids and build_thread_ids[0] != main_thread_id, build_thread_ids

        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await call_response(response, send)
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        assert body.startswith(b"PK"), body[:8]
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            assert archive.read("success.bin") == b"streamed-payload"
        assert not path.exists()
        assert closed == [asset.file_path]
        assert list(root.iterdir()) == []

        background_response, _ = await make_response(asset, b"background")
        background_path = Path(background_response.path)
        assert background_path.exists() and background_response.background is not None
        await background_response.background()
        assert not background_path.exists()
        assert list(root.iterdir()) == []


async def test_send_failure_removes_archive() -> None:
    asset = make_asset("asset-disconnect", "disconnect.bin")
    with temporary_archive_dir() as root:
        response, closed = await make_response(asset, b"disconnect-payload")
        path = Path(response.path)
        assert path.exists()
        body_sends = 0

        async def disconnecting_send(message: dict[str, Any]) -> None:
            nonlocal body_sends
            if message["type"] == "http.response.body":
                body_sends += 1
                raise ConnectionError("client disconnected")

        try:
            await call_response(response, disconnecting_send)
        except ConnectionError as exc:
            assert str(exc) == "client disconnected"
        else:
            raise AssertionError("send failure did not propagate")

        assert body_sends == 1
        assert not path.exists(), path
        assert closed == [asset.file_path]
        assert list(root.iterdir()) == []


async def test_request_cancellation_removes_late_archive() -> None:
    asset = make_asset("asset-cancelled", "cancelled.bin")
    db = FakeDB([asset])
    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    worker_finished = asyncio.Event()
    release_worker = threading.Event()
    completed_paths: list[Path] = []
    closed: list[str] = []
    original_builder = builder()

    def blocking_open_asset(file_path: str) -> ObjectHandle:
        loop.call_soon_threadsafe(worker_started.set)
        if not release_worker.wait(timeout=5):
            raise RuntimeError("test worker release timed out")
        return ObjectHandle(
            size=7,
            _stream=iter([b"payload"]),
            _cleanup=lambda: closed.append(file_path),
        )

    def recording_builder(selected_assets) -> Path:
        try:
            path = original_builder(selected_assets)
            completed_paths.append(path)
            return path
        finally:
            loop.call_soon_threadsafe(worker_finished.set)

    with temporary_archive_dir() as root, patched(
        assets_api,
        _build_batch_archive=recording_builder,
        open_asset=blocking_open_asset,
        task_owner_map=owner_map,
    ):
        request = asyncio.create_task(
            assets_api.batch_download([asset.id], user=ADMIN, db=db)
        )
        await asyncio.wait_for(worker_started.wait(), timeout=2)
        request.cancel()
        done, _ = await asyncio.wait({request}, timeout=2)
        try:
            assert request in done, "request cancellation did not propagate promptly"
            try:
                request.result()
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("batch_download swallowed request cancellation")
        finally:
            release_worker.set()

        await asyncio.wait_for(worker_finished.wait(), timeout=2)
        await wait_until(
            lambda: bool(completed_paths) and not completed_paths[0].exists()
        )
        assert closed == [asset.file_path]
        assert list(root.iterdir()) == []


async def test_cancelled_failing_worker_exception_is_consumed() -> None:
    asset = make_asset("asset-cancelled-error", "cancelled-error.bin")
    db = FakeDB([asset])
    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    worker_finished = asyncio.Event()
    release_worker = threading.Event()
    closed: list[str] = []
    leaked_contexts: list[dict[str, Any]] = []
    original_builder = builder()
    previous_handler = loop.get_exception_handler()

    def failing_chunks():
        yield b"partial"
        raise RuntimeError("late archive worker failure")

    def blocking_open_asset(file_path: str) -> ObjectHandle:
        loop.call_soon_threadsafe(worker_started.set)
        if not release_worker.wait(timeout=5):
            raise RuntimeError("test worker release timed out")
        return ObjectHandle(
            size=7,
            _stream=failing_chunks(),
            _cleanup=lambda: closed.append(file_path),
        )

    def recording_builder(selected_assets) -> Path:
        try:
            return original_builder(selected_assets)
        finally:
            loop.call_soon_threadsafe(worker_finished.set)

    def capture_exception(_loop, context: dict[str, Any]) -> None:
        leaked_contexts.append(context)

    loop.set_exception_handler(capture_exception)
    try:
        with temporary_archive_dir() as root, patched(
            assets_api,
            _build_batch_archive=recording_builder,
            open_asset=blocking_open_asset,
            task_owner_map=owner_map,
        ):
            request = asyncio.create_task(
                assets_api.batch_download([asset.id], user=ADMIN, db=db)
            )
            await asyncio.wait_for(worker_started.wait(), timeout=2)
            request.cancel()
            done, _ = await asyncio.wait({request}, timeout=2)
            try:
                assert request in done, "request cancellation did not propagate promptly"
                try:
                    request.result()
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("batch_download swallowed request cancellation")
            finally:
                release_worker.set()

            await asyncio.wait_for(worker_finished.wait(), timeout=2)
            await wait_until(lambda: list(root.iterdir()) == [])
            await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
            assert closed == [asset.file_path]
            assert not leaked_contexts, leaked_contexts
    finally:
        loop.set_exception_handler(previous_handler)


async def main() -> int:
    print("=== batch archive limits and lifecycle ===")
    await run_case(
        "51 IDs fail before database or file work",
        test_count_limit_precedes_database_and_files,
    )
    await run_case(
        "exactly 50 IDs pass the count gate",
        test_count_limit_allows_exactly_50_ids,
    )
    await run_case(
        "metadata above 1 GiB fails before archive creation",
        test_metadata_limit_precedes_archive_creation,
    )
    await run_case(
        "metadata exactly 1 GiB passes the metadata gate",
        test_metadata_limit_allows_exactly_1_gib,
    )
    await run_case(
        "actual streamed bytes above the limit remove the partial archive",
        test_actual_size_limit_removes_partial_and_closes_handle,
    )
    await run_case(
        "actual streamed bytes exactly 1 GiB are accepted",
        test_actual_size_limit_allows_exactly_1_gib,
    )
    await run_case(
        "archive names are safe unique basenames",
        test_archive_names_are_safe_unique_basenames,
    )
    await run_case(
        "a missing selected member fails and removes the partial archive",
        test_missing_selected_member_fails_and_removes_partial_archive,
    )
    await run_case(
        "FileStore failures map to 503 and remove partial archives",
        test_filestore_failure_maps_to_503_and_cleans_partial_archive,
    )
    await run_case(
        "FileResponse streams a worker-built archive and cleans it up",
        test_file_response_streams_off_thread_and_cleans_up,
    )
    await run_case(
        "ASGI send failure removes the temporary archive",
        test_send_failure_removes_archive,
    )
    await run_case(
        "request cancellation removes a late completed archive",
        test_request_cancellation_removes_late_archive,
    )
    await run_case(
        "cancelled failing worker exceptions are consumed",
        test_cancelled_failing_worker_exception_is_consumed,
    )

    print(f"\nRESULT: {PASS} passed / {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
