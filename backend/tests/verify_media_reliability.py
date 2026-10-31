"""Offline verification for persistent thumbnails and derived-file cleanup.

Uses real Pillow and real FileStore implementations with temporary storage. Only
database/auth boundaries and the external MinIO client are replaced.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import tempfile
import threading
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from starlette.requests import Request

# These route functions receive an authenticated user directly in this verifier.
# Avoid loading optional JWT/password packages at the fake auth boundary.
auth_stub = types.ModuleType("app.auth")


async def get_current_user_stub():
    raise AssertionError("auth dependency must not run in direct route tests")


auth_stub.get_current_user = get_current_user_stub
sys.modules.setdefault("app.auth", auth_stub)

import app.api.assets as assets_api
import app.api.tasks as tasks_api
import app.filestore as filestore
import app.thumbs as thumbs
from app.config import settings
from app.filestore import LocalFileStore, MinioFileStore, ObjectHandle, safe_key
from app.models.models import Asset, Task


PASS = 0
FAIL = 0
_MISSING = object()
ADMIN = SimpleNamespace(id="admin", role="admin")


def report(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    suffix = f"  {detail}" if detail else ""
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{suffix}")


async def run_case(name: str, test) -> None:
    try:
        await test()
    except BaseException as exc:
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


class TrackingLocalFileStore(LocalFileStore):
    def __init__(self) -> None:
        self.read_calls: list[str] = []
        self.save_calls: list[str] = []
        self.open_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.closed: list[str] = []
        self.read_threads: list[int] = []
        self.save_threads: list[int] = []

    @staticmethod
    def _key(file_path: str) -> str:
        return safe_key(file_path) or file_path

    def read_bytes(self, file_path: str) -> bytes | None:
        self.read_calls.append(self._key(file_path))
        self.read_threads.append(threading.get_ident())
        return super().read_bytes(file_path)

    def save(
        self,
        file_path: str,
        data: bytes,
        content_type: str | None = None,
    ) -> None:
        self.save_calls.append(self._key(file_path))
        self.save_threads.append(threading.get_ident())
        super().save(file_path, data, content_type)

    def open(self, file_path: str) -> ObjectHandle | None:
        key = self._key(file_path)
        self.open_calls.append(key)
        handle = super().open(file_path)
        if handle is not None:
            prior_cleanup = handle._cleanup

            def cleanup() -> None:
                self.closed.append(key)
                if callable(prior_cleanup):
                    prior_cleanup()

            handle._cleanup = cleanup
        return handle

    def delete(self, file_path: str) -> tuple[bool, str]:
        self.delete_calls.append(self._key(file_path))
        return super().delete(file_path)


class BlockingLocalFileStore(TrackingLocalFileStore):
    def __init__(self, source_key: str) -> None:
        super().__init__()
        self.source_key = source_key
        self.read_started = threading.Event()
        self.release_read = threading.Event()

    def read_bytes(self, file_path: str) -> bytes | None:
        key = self._key(file_path)
        if key == self.source_key:
            self.read_started.set()
            if not self.release_read.wait(timeout=5):
                raise AssertionError("thumbnail source read was never released")
        return super().read_bytes(file_path)


@contextmanager
def temporary_local_store(
    store: LocalFileStore | None = None,
) -> Iterator[LocalFileStore]:
    original_dir = settings.STORAGE_DIR
    original_backend = settings.STORAGE_BACKEND
    original_store = filestore._store
    with tempfile.TemporaryDirectory(prefix="media-reliability-") as raw_dir:
        settings.STORAGE_DIR = Path(raw_dir)
        settings.STORAGE_BACKEND = "local"
        active = store or TrackingLocalFileStore()
        filestore._store = active
        try:
            yield active
        finally:
            filestore._store = original_store
            settings.STORAGE_BACKEND = original_backend
            settings.STORAGE_DIR = original_dir


@contextmanager
def selected_store(store) -> Iterator[None]:
    original_store = filestore._store
    filestore._store = store
    try:
        yield
    finally:
        filestore._store = original_store


def seed_local(store: LocalFileStore, key: str, data: bytes) -> None:
    LocalFileStore.save(store, key, data)


def png_bytes(
    mode: str,
    size: tuple[int, int],
    color,
) -> bytes:
    with Image.new(mode, size, color) as image, io.BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def patterned_png(size: tuple[int, int] = (800, 200)) -> bytes:
    width, height = size
    raw = bytes(
        channel
        for y in range(height)
        for x in range(width)
        for channel in ((x * 17 + y * 7) % 256, (x * 3 + y * 29) % 256, (x + y * 11) % 256)
    )
    with Image.frombytes("RGB", size, raw) as image, io.BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def exif_rotated_jpeg() -> bytes:
    with Image.new("RGB", (40, 20), (30, 100, 200)) as image, io.BytesIO() as stream:
        exif = image.getexif()
        exif[274] = 6
        image.save(stream, format="JPEG", exif=exif)
        return stream.getvalue()


def jpeg_dht_tables(data: bytes) -> tuple[bytes, ...]:
    assert data.startswith(b"\xff\xd8"), data[:8]
    tables: list[bytes] = []
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if marker in {0xD9, 0xDA}:
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        payload = data[offset + 2:offset + length]
        if marker == 0xC4:
            tables.append(payload)
        offset += length
    return tuple(tables)


def assert_jpeg(data: bytes, expected_size: tuple[int, int] | None = None) -> None:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.format == "JPEG", image.format
        assert image.mode == "RGB", image.mode
        assert max(image.size) <= 400, image.size
        if expected_size is not None:
            assert image.size == expected_size, image.size


def assert_optimized_jpeg(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as image, io.BytesIO() as baseline:
        image.load()
        image.save(baseline, format="JPEG", quality=85, optimize=False)
        standard_tables = jpeg_dht_tables(baseline.getvalue())
    actual_tables = jpeg_dht_tables(data)
    assert actual_tables, "JPEG has no Huffman tables"
    assert actual_tables != standard_tables, "JPEG uses default, unoptimized Huffman tables"


def make_asset(
    asset_id: str,
    task_id: str,
    file_name: str,
    mime_type: str,
    *,
    file_path: str | None = None,
) -> Asset:
    return Asset(
        id=asset_id,
        task_id=task_id,
        file_path=file_path or f"{task_id}/{file_name}",
        file_name=file_name,
        file_size=1,
        mime_type=mime_type,
        tags=[],
        meta={},
        is_shared=False,
    )


class ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self) -> list:
        if self.value is None:
            return []
        return self.value if isinstance(self.value, list) else [self.value]


class ThumbDB:
    def __init__(self, asset: Asset) -> None:
        self.asset = asset

    async def execute(self, _query) -> ScalarResult:
        return ScalarResult(self.asset)


class AssetDeleteDB:
    def __init__(self, asset: Asset, owner: str = "owner") -> None:
        self.asset = asset
        self.owner = owner
        self.deleted: list[object] = []
        self.commits = 0

    async def get(self, _model, _identity):
        return self.asset

    async def execute(self, _query) -> ScalarResult:
        return ScalarResult(self.owner)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    async def commit(self) -> None:
        self.commits += 1


class TaskMutationDB:
    def __init__(self, task: Task, assets: list[Asset]) -> None:
        self.task = task
        self.assets = assets
        self.deleted: list[object] = []
        self.commits = 0
        self.refreshes = 0

    async def get(self, _model, _identity):
        return self.task

    async def execute(self, _query) -> ScalarResult:
        return ScalarResult(self.assets)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value: object) -> None:
        self.refreshes += 1


def request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/thumbnail",
            "raw_path": b"/thumbnail",
            "query_string": b"",
            "headers": raw_headers,
            "client": ("test", 1),
            "server": ("test", 80),
        }
    )


async def owner_map(_db, task_ids: set[str]) -> dict[str, str]:
    return {task_id: "owner" for task_id in task_ids}


async def call_thumb(
    asset: Asset,
    *,
    headers: dict[str, str] | None = None,
):
    with patched(tasks_api, task_owner_map=owner_map):
        return await tasks_api.download_asset_thumb(
            asset.task_id,
            asset.file_name,
            request=request(headers),
            user=ADMIN,
            db=ThumbDB(asset),
        )


async def response_bytes(response) -> bytes:
    if isinstance(response, FileResponse):
        return Path(response.path).read_bytes()
    if isinstance(response, StreamingResponse):
        return b"".join([chunk async for chunk in response.body_iterator])
    return bytes(response.body)


async def expect_http(awaitable, status: int) -> HTTPException:
    try:
        await awaitable
    except HTTPException as exc:
        assert exc.status_code == status, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTP {status}")


async def wait_for_thread_event(event: threading.Event) -> None:
    observed = await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3)
    assert observed, "worker event was not observed"


async def wait_until(predicate, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def thumbnail_task_registries() -> list[dict]:
    return [
        value
        for name, value in vars(tasks_api).items()
        if "thumb" in name.lower() and isinstance(value, dict)
    ]


async def test_rgb_thumb_is_bounded_optimized_jpeg() -> None:
    result = thumbs.make_image_thumb(patterned_png())
    assert result is not None
    assert_jpeg(result, (400, 100))
    assert_optimized_jpeg(result)


async def test_rgba_transparency_is_composited_on_white() -> None:
    with Image.new("RGBA", (100, 100), (255, 0, 0, 0)) as image, io.BytesIO() as stream:
        for y in range(40, 60):
            for x in range(40, 60):
                image.putpixel((x, y), (0, 0, 255, 255))
        image.save(stream, format="PNG")
        result = thumbs.make_image_thumb(stream.getvalue())
    assert result is not None
    with Image.open(io.BytesIO(result)) as output:
        output.load()
        corner = output.convert("RGB").getpixel((5, 5))
        assert all(channel >= 245 for channel in corner), corner


async def test_exif_orientation_is_applied() -> None:
    result = thumbs.make_image_thumb(exif_rotated_jpeg())
    assert result is not None
    assert_jpeg(result, (20, 40))


async def test_invalid_image_returns_none() -> None:
    assert thumbs.make_image_thumb(b"not an image") is None


async def test_image_thumb_has_no_in_memory_lru() -> None:
    cached = [
        name
        for name, value in vars(thumbs).items()
        if callable(value) and hasattr(value, "cache_info")
    ]
    assert not cached, cached


async def test_image_generation_persists_and_uses_worker_thread() -> None:
    main_thread = threading.get_ident()
    source_key = "task-image/source.png"
    derived_key = "task-image/thumb_source.png.jpg"
    asset = make_asset("asset-image", "task-image", "source.png", "image/png")
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, patterned_png((640, 320)))
        response = await call_thumb(asset)
        assert isinstance(response, FileResponse), type(response)
        generated = store.read_bytes(derived_key)
        assert generated is not None
        assert_jpeg(generated, (400, 200))
        assert source_key in store.read_calls
        assert store.save_calls.count(derived_key) == 1, store.save_calls
        assert all(thread != main_thread for thread in store.read_threads[:-1])
        assert all(thread != main_thread for thread in store.save_threads)
        assert derived_key in store.closed, store.closed


async def test_existing_thumbnail_is_reused_without_source_read() -> None:
    source_key = "task-existing/source.png"
    derived_key = "task-existing/thumb_source.png.jpg"
    asset = make_asset("asset-existing", "task-existing", "source.png", "image/png")
    existing = thumbs.make_image_thumb(png_bytes("RGB", (20, 10), (12, 34, 56)))
    assert existing is not None
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source must not be read")
        seed_local(store, derived_key, existing)
        response = await call_thumb(asset)
        assert isinstance(response, FileResponse), type(response)
        assert await response_bytes(response) == existing
        assert source_key not in store.read_calls, store.read_calls
        assert not store.save_calls, store.save_calls
        assert derived_key in store.closed, store.closed


async def test_concurrent_requests_generate_once() -> None:
    source_key = "task-concurrent/source.png"
    derived_key = "task-concurrent/thumb_source.png.jpg"
    asset = make_asset("asset-concurrent", "task-concurrent", "source.png", "image/png")
    store = BlockingLocalFileStore(source_key)
    with temporary_local_store(store):
        seed_local(store, source_key, patterned_png((500, 250)))
        first = asyncio.create_task(call_thumb(asset))
        await wait_for_thread_event(store.read_started)
        second = asyncio.create_task(call_thumb(asset))
        await wait_until(lambda: store.open_calls.count(derived_key) >= 2)
        store.release_read.set()
        responses = await asyncio.gather(first, second)
        assert all(isinstance(response, FileResponse) for response in responses)
        assert store.read_calls.count(source_key) == 1, store.read_calls
        assert store.save_calls.count(derived_key) == 1, store.save_calls


async def test_cancelled_waiter_does_not_cancel_or_leak_generation() -> None:
    source_key = "task-cancel/source.png"
    derived_key = "task-cancel/thumb_source.png.jpg"
    asset = make_asset("asset-cancel", "task-cancel", "source.png", "image/png")
    store = BlockingLocalFileStore(source_key)
    with temporary_local_store(store):
        seed_local(store, source_key, patterned_png((500, 250)))
        cancelled = asyncio.create_task(call_thumb(asset))
        await wait_for_thread_event(store.read_started)
        survivor = asyncio.create_task(call_thumb(asset))
        await wait_until(lambda: store.open_calls.count(derived_key) >= 2)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("request cancellation was not propagated")
        store.release_read.set()
        response = await survivor
        assert isinstance(response, FileResponse), type(response)
        await wait_until(
            lambda: all(not registry for registry in thumbnail_task_registries())
        )
        assert thumbnail_task_registries(), "no strongly held thumbnail task registry"
        assert store.read_calls.count(source_key) == 1, store.read_calls
        assert store.save_calls.count(derived_key) == 1, store.save_calls


async def test_failed_image_generation_returns_404_and_cleans_inflight() -> None:
    source_key = "task-invalid/source.png"
    derived_key = "task-invalid/thumb_source.png.jpg"
    asset = make_asset("asset-invalid", "task-invalid", "source.png", "image/png")
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"invalid image bytes")
        capture = LogCapture()
        prior_level = tasks_api.logger.level
        prior_propagate = tasks_api.logger.propagate
        tasks_api.logger.addHandler(capture)
        tasks_api.logger.setLevel(logging.WARNING)
        tasks_api.logger.propagate = False
        try:
            await expect_http(call_thumb(asset), 404)
            await wait_until(
                lambda: all(not registry for registry in thumbnail_task_registries())
            )
        finally:
            tasks_api.logger.removeHandler(capture)
            tasks_api.logger.setLevel(prior_level)
            tasks_api.logger.propagate = prior_propagate
        assert not store.exists(derived_key)
        assert store.save_calls.count(derived_key) == 0
        assert any(
            "generation failed" in message.lower() and derived_key in message
            for message in capture.messages
        ), capture.messages


async def test_video_without_thumbnail_never_reads_source() -> None:
    source_key = "task-video/source.mp4"
    asset = make_asset("asset-video", "task-video", "source.mp4", "video/mp4")
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"video source bytes")
        await expect_http(call_thumb(asset), 404)
        assert source_key not in store.read_calls, store.read_calls
        assert not store.save_calls, store.save_calls


async def test_audio_never_generates_thumbnail() -> None:
    source_key = "task-audio/source.wav"
    asset = make_asset("asset-audio", "task-audio", "source.wav", "audio/wav")
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"audio source bytes")
        await expect_http(call_thumb(asset), 404)
        assert source_key not in store.read_calls, store.read_calls
        assert not store.save_calls, store.save_calls


async def test_etag_cache_headers_and_revalidation() -> None:
    source_key = "task-etag/source.png"
    derived_key = "task-etag/thumb_source.png.jpg"
    asset = make_asset("asset-etag", "task-etag", "source.png", "image/png")
    existing = thumbs.make_image_thumb(png_bytes("RGB", (12, 8), "navy"))
    assert existing is not None
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source must not be read")
        seed_local(store, derived_key, existing)
        response = await call_thumb(asset)
        assert response.headers["etag"] == '"asset-etag"', response.headers
        assert response.headers["cache-control"] == "private, no-cache", response.headers

        store.read_calls.clear()
        revalidated = await call_thumb(
            asset,
            headers={"If-None-Match": '"asset-etag"'},
        )
        assert revalidated.status_code == 304, revalidated.status_code
        assert await response_bytes(revalidated) == b""
        assert source_key not in store.read_calls, store.read_calls
        assert revalidated.headers["etag"] == '"asset-etag"'
        assert revalidated.headers["cache-control"] == "private, no-cache"


class FakeObjectResponse:
    def __init__(self, payload: bytes, closed: list[str]) -> None:
        self.stream = io.BytesIO(payload)
        self.closed = closed

    def read(self, amount: int = -1) -> bytes:
        return self.stream.read(amount)

    def close(self) -> None:
        self.closed.append("close")

    def release_conn(self) -> None:
        self.closed.append("release")


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.closed: list[str] = []

    def put_object(self, _bucket, key, data, length, **_kwargs) -> None:
        payload = data.read()
        assert len(payload) == length
        self.objects[key] = payload

    def stat_object(self, _bucket, key):
        if key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return SimpleNamespace(size=len(self.objects[key]))

    def get_object(self, _bucket, key) -> FakeObjectResponse:
        return FakeObjectResponse(self.objects[key], self.closed)

    def remove_object(self, _bucket, key) -> None:
        self.objects.pop(key, None)


async def test_object_thumbnail_streams_and_closes_handle() -> None:
    asset = make_asset("asset-object", "task-object", "source.mp4", "video/mp4")
    derived_key = "task-object/thumb_source.mp4.jpg"
    payload = b"object-backed-thumbnail"
    client = FakeS3()
    store = MinioFileStore(client=client)
    store.save(derived_key, payload, content_type="image/jpeg")
    with selected_store(store):
        response = await call_thumb(asset)
        assert isinstance(response, StreamingResponse), type(response)
        assert await response_bytes(response) == payload
        assert "close" in client.closed and "release" in client.closed, client.closed


def make_task(task_id: str, status: str = "done") -> Task:
    return Task(
        id=task_id,
        template_id="template",
        created_by="owner",
        batch_id="batch",
        status=status,
        progress=100.0,
        params={},
        result={},
        error_msg="",
    )


async def test_asset_delete_removes_source_and_thumbnail() -> None:
    asset = make_asset("asset-delete", "task-asset-delete", "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-asset-delete/thumb_source.png.jpg"
    db = AssetDeleteDB(asset)
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumb")
        body = await assets_api.delete_asset(asset.id, user=ADMIN, db=db)
        assert not store.exists(source_key)
        assert not store.exists(derived_key)
        assert store.delete_calls == [source_key, derived_key], store.delete_calls
        assert body["file_removed"] is True and body["file_error"] == ""
        assert body["thumbnail_removed"] is True and body["thumbnail_error"] == ""
        assert db.deleted == [asset] and db.commits == 1


async def test_task_delete_removes_sources_and_thumbnails() -> None:
    task = make_task("task-delete")
    asset = make_asset("asset-task-delete", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-delete/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumb")
        body = await tasks_api.delete_task(task.id, user=ADMIN, db=db)
        assert not store.exists(source_key)
        assert not store.exists(derived_key)
        assert store.delete_calls == [source_key, derived_key], store.delete_calls
        assert body["files_removed"] == 1 and body["file_errors"] == []
        assert body["thumbnails_removed"] == 1 and body["thumbnail_errors"] == []
        assert db.deleted == [asset, task] and db.commits == 1


async def test_retry_removes_sources_and_thumbnails() -> None:
    task = make_task("task-retry", status="error")
    asset = make_asset("asset-retry", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-retry/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    spawned: list[str] = []

    async def spawn(task_id: str) -> bool:
        spawned.append(task_id)
        return True

    with temporary_local_store() as raw_store, patched(tasks_api, spawn_task=spawn):
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumb")
        result = await tasks_api.retry_task(task.id, user=ADMIN, db=db)
        assert result is task
        assert not store.exists(source_key)
        assert not store.exists(derived_key)
        assert store.delete_calls == [source_key, derived_key], store.delete_calls
        assert db.deleted == [asset] and db.commits == 1 and db.refreshes == 1
        assert spawned == [task.id]


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def test_cleanup_failures_do_not_restore_database_records() -> None:
    source_failure = make_asset(
        "asset-source-failure",
        "task-source-failure",
        "source.png",
        "image/png",
        file_path="../outside.png",
    )
    source_db = AssetDeleteDB(source_failure)
    derived_key = "task-source-failure/thumb_source.png.jpg"
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, derived_key, b"thumb")
        body = await assets_api.delete_asset(source_failure.id, user=ADMIN, db=source_db)
        assert body["file_removed"] is False and body["file_error"]
        assert body["thumbnail_removed"] is True and body["thumbnail_error"] == ""
        assert source_db.deleted == [source_failure] and source_db.commits == 1

    derivative_failure = make_asset(
        "asset-thumb-failure",
        "..",
        "source.png",
        "image/png",
        file_path="safe/source.png",
    )
    derivative_db = AssetDeleteDB(derivative_failure)
    capture = LogCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    try:
        with temporary_local_store() as raw_store:
            store = raw_store
            assert isinstance(store, TrackingLocalFileStore)
            seed_local(store, derivative_failure.file_path, b"source")
            body = await assets_api.delete_asset(
                derivative_failure.id,
                user=ADMIN,
                db=derivative_db,
            )
            assert body["file_removed"] is True and body["file_error"] == ""
            assert body["thumbnail_removed"] is False and body["thumbnail_error"]
            assert derivative_db.deleted == [derivative_failure]
            assert derivative_db.commits == 1
    finally:
        root_logger.removeHandler(capture)
    assert any("thumbnail" in message.lower() for message in capture.messages), capture.messages


TESTS = [
    ("RGB thumbnail is a bounded optimized JPEG", test_rgb_thumb_is_bounded_optimized_jpeg),
    ("RGBA transparency is composited onto white", test_rgba_transparency_is_composited_on_white),
    ("EXIF orientation is applied", test_exif_orientation_is_applied),
    ("invalid image bytes return None", test_invalid_image_returns_none),
    ("image thumbnails have no in-memory LRU", test_image_thumb_has_no_in_memory_lru),
    ("image generation persists through FileStore off-loop", test_image_generation_persists_and_uses_worker_thread),
    ("existing persistent thumbnail is reused", test_existing_thumbnail_is_reused_without_source_read),
    ("concurrent image requests generate exactly once", test_concurrent_requests_generate_once),
    ("request cancellation does not cancel or leak shared generation", test_cancelled_waiter_does_not_cancel_or_leak_generation),
    ("failed image generation returns 404 and clears in-flight state", test_failed_image_generation_returns_404_and_cleans_inflight),
    ("missing video thumbnail does not read source", test_video_without_thumbnail_never_reads_source),
    ("audio never generates a thumbnail", test_audio_never_generates_thumbnail),
    ("ETag, no-cache, and 304 revalidation work", test_etag_cache_headers_and_revalidation),
    ("object-backed thumbnail streams and closes its handle", test_object_thumbnail_streams_and_closes_handle),
    ("asset deletion removes source and thumbnail", test_asset_delete_removes_source_and_thumbnail),
    ("task deletion removes sources and thumbnails", test_task_delete_removes_sources_and_thumbnails),
    ("task retry removes sources and thumbnails", test_retry_removes_sources_and_thumbnails),
    ("cleanup failures are reported without restoring DB records", test_cleanup_failures_do_not_restore_database_records),
]


async def main() -> int:
    print("=== Persistent thumbnail and derived-file reliability ===")
    for name, test in TESTS:
        await run_case(name, test)
    print(f"\n{PASS} passed, {FAIL} failed")
    return int(FAIL != 0)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
