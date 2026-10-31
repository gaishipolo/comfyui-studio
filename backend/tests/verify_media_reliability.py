"""Offline verification for persistent thumbnails and derived-file cleanup.

Uses real Pillow and real FileStore implementations with temporary storage. Only
database/auth boundaries and the external MinIO client are replaced.
"""
from __future__ import annotations

import asyncio
import gc
import io
import logging
import sys
import tempfile
import threading
import types
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
from app.database import Base
from app.filestore import LocalFileStore, MinioFileStore, ObjectHandle, safe_key
from app.models.models import Asset, Task, Template


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


class BlockingThumbnailSaveStore(TrackingLocalFileStore):
    def __init__(self, derived_key: str) -> None:
        super().__init__()
        self.derived_key = derived_key
        self.save_started = threading.Event()
        self.release_save = threading.Event()

    def save(
        self,
        file_path: str,
        data: bytes,
        content_type: str | None = None,
    ) -> None:
        if self._key(file_path) == self.derived_key:
            self.save_started.set()
            if not self.release_save.wait(timeout=5):
                raise AssertionError("thumbnail save was never released")
        super().save(file_path, data, content_type)


class BlockingCleanupStore(TrackingLocalFileStore):
    def __init__(self, source_key: str, derived_key: str) -> None:
        super().__init__()
        self.source_key = source_key
        self.derived_key = derived_key
        self.cleanup_started = threading.Event()
        self.cleanup_finished = threading.Event()
        self.release_cleanup = threading.Event()

    def delete(self, file_path: str) -> tuple[bool, str]:
        key = self._key(file_path)
        if key == self.source_key:
            self.cleanup_started.set()
            if not self.release_cleanup.wait(timeout=5):
                raise AssertionError("asset cleanup was never released")
        result = super().delete(file_path)
        if key == self.derived_key:
            self.cleanup_finished.set()
        return result


class StaleMissLocalFileStore(TrackingLocalFileStore):
    def __init__(self, derived_key: str) -> None:
        super().__init__()
        self.derived_key = derived_key
        self.stale_next_open = False
        self.stale_miss_observed = threading.Event()
        self.release_stale_miss = threading.Event()

    def open(self, file_path: str) -> ObjectHandle | None:
        key = self._key(file_path)
        if self.stale_next_open and key == self.derived_key:
            self.stale_next_open = False
            self.open_calls.append(key)
            self.stale_miss_observed.set()
            if not self.release_stale_miss.wait(timeout=5):
                raise AssertionError("stale thumbnail miss was never released")
            return None
        return super().open(file_path)


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


@asynccontextmanager
async def temporary_sqlite_sessions():
    with tempfile.TemporaryDirectory(prefix="media-reliability-db-") as raw_dir:
        database_path = (Path(raw_dir) / "media.sqlite3").resolve().as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            yield async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
        finally:
            await engine.dispose()


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
        self.gets = 0
        self.refreshes = 0

    async def get(self, _model, _identity):
        self.gets += 1
        return self.task

    async def execute(self, _query) -> ScalarResult:
        return ScalarResult(self.assets)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value: object) -> None:
        self.refreshes += 1


class ConcurrentRetryDB(TaskMutationDB):
    def __init__(self, task: Task) -> None:
        super().__init__(task, [])
        self.get_calls = 0

    async def get(self, _model, _identity):
        self.get_calls += 1
        return await super().get(_model, _identity)

    async def execute(self, _query) -> ScalarResult:
        return ScalarResult([])


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


def lifecycle_waiter_count(key: str) -> int:
    lock = thumbs._thumbnail_lifecycle_locks.get(key)
    waiters = getattr(lock, "_waiters", None) if lock is not None else None
    return len(waiters) if waiters else 0


async def completed_while_generation_blocked(task: asyncio.Task) -> bool:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
    except TimeoutError:
        return False
    return True


async def contend_for_lifecycle(key: str, acquired: asyncio.Event) -> None:
    async with thumbs.thumbnail_lifecycle([key]):
        acquired.set()


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


async def test_lifecycle_lock_registry_releases_unused_keys() -> None:
    lifecycle = getattr(thumbs, "thumbnail_lifecycle")
    for index in range(20):
        async with lifecycle([f"transient/thumb_{index}.jpg"]):
            pass
    gc.collect()
    await asyncio.sleep(0)
    registry = getattr(thumbs, "_thumbnail_lifecycle_locks")
    assert not registry, list(registry)


async def test_lifecycle_keys_are_acquired_in_sorted_order() -> None:
    lifecycle = getattr(thumbs, "thumbnail_lifecycle")
    entered: list[str] = []

    async def own(keys: list[str], label: str) -> None:
        async with lifecycle(keys):
            entered.append(label)

    async with lifecycle(["ordering/a"]):
        first = asyncio.create_task(
            own(["ordering/a", "ordering/b"], "first")
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            own(["ordering/b", "ordering/a"], "second")
        )
        await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert entered == ["first", "second"], entered


async def test_cancelled_parent_observes_failed_child() -> None:
    child_started = asyncio.Event()
    release_child = asyncio.Event()
    capture = LogCapture()
    prior_level = thumbs.logger.level
    prior_propagate = thumbs.logger.propagate

    async def failing_child() -> None:
        child_started.set()
        await release_child.wait()
        raise RuntimeError("commit failed after cancellation")

    thumbs.logger.addHandler(capture)
    thumbs.logger.setLevel(logging.ERROR)
    thumbs.logger.propagate = False
    try:
        child = asyncio.create_task(
            failing_child(),
            name="failing-retry-mutation",
        )
        parent = asyncio.create_task(thumbs.wait_for_task_completion(child))
        await child_started.wait()
        parent.cancel()
        await asyncio.sleep(0)
        release_child.set()
        try:
            await parent
        except BaseException as exc:
            observed = exc
        else:
            raise AssertionError("failed child unexpectedly returned")
    finally:
        thumbs.logger.removeHandler(capture)
        thumbs.logger.setLevel(prior_level)
        thumbs.logger.propagate = prior_propagate

    assert isinstance(observed, RuntimeError), type(observed)
    assert "commit failed" in str(observed)
    assert any(
        "failing-retry-mutation" in message and "commit failed" in message
        for message in capture.messages
    ), capture.messages


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


async def test_stale_second_miss_does_not_regenerate_thumbnail() -> None:
    source_key = "task-stale/source.png"
    derived_key = "task-stale/thumb_source.png.jpg"
    asset = make_asset("asset-stale", "task-stale", "source.png", "image/png")
    store = StaleMissLocalFileStore(derived_key)
    with temporary_local_store(store):
        seed_local(store, source_key, patterned_png((500, 250)))
        first = await call_thumb(asset)
        assert isinstance(first, FileResponse), type(first)
        await wait_until(
            lambda: all(not registry for registry in thumbnail_task_registries())
        )

        store.stale_next_open = True
        second_task = asyncio.create_task(call_thumb(asset))
        await wait_for_thread_event(store.stale_miss_observed)
        assert all(not registry for registry in thumbnail_task_registries())
        store.release_stale_miss.set()
        second = await second_task

        assert isinstance(second, FileResponse), type(second)
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
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        retained_ownership = not cancelled.done()
        store.release_read.set()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("request cancellation was not propagated")
        response = await survivor
        assert isinstance(response, FileResponse), type(response)
        await wait_until(
            lambda: all(not registry for registry in thumbnail_task_registries())
        )
        assert thumbnail_task_registries(), "no strongly held thumbnail task registry"
        assert retained_ownership, "cancelled request released lifecycle ownership early"
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
        await expect_http(
            call_thumb(asset, headers={"If-None-Match": '"asset-video"'}),
            404,
        )
        assert source_key not in store.read_calls, store.read_calls
        assert not store.save_calls, store.save_calls


async def test_audio_never_generates_thumbnail() -> None:
    source_key = "task-audio/source.wav"
    asset = make_asset("asset-audio", "task-audio", "source.wav", "audio/wav")
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"audio source bytes")
        await expect_http(
            call_thumb(asset, headers={"If-None-Match": '"asset-audio"'}),
            404,
        )
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


async def test_missing_image_matching_etag_skips_filestore() -> None:
    source_key = "task-etag-missing/source.png"
    derived_key = "task-etag-missing/thumb_source.png.jpg"
    asset = make_asset(
        "asset-etag-missing",
        "task-etag-missing",
        "source.png",
        "image/png",
    )
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, patterned_png((500, 250)))

        response = await call_thumb(
            asset,
            headers={"If-None-Match": '"asset-etag-missing"'},
        )

        assert response.status_code == 304, response.status_code
        assert await response_bytes(response) == b""
        assert store.open_calls == [], store.open_calls
        assert store.read_calls == [], store.read_calls
        assert store.save_calls == [], store.save_calls
        assert not store.exists(derived_key)


async def test_existing_video_matching_etag_returns_304() -> None:
    source_key = "task-video-etag/source.mp4"
    derived_key = "task-video-etag/thumb_source.mp4.jpg"
    asset = make_asset(
        "asset-video-etag",
        "task-video-etag",
        "source.mp4",
        "video/mp4",
    )
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"video source must not be read")
        seed_local(store, derived_key, b"existing thumbnail")

        response = await call_thumb(
            asset,
            headers={"If-None-Match": '"asset-video-etag"'},
        )

        assert response.status_code == 304, response.status_code
        assert await response_bytes(response) == b""
        assert source_key not in store.read_calls, store.read_calls
        assert derived_key in store.closed, store.closed


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


async def seed_sqlite_task(session_factory, task: Task, asset: Asset) -> None:
    async with session_factory() as db:
        db.add(Template(id="template", name=f"Template {task.id}"))
        db.add_all([task, asset])
        await db.commit()


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


async def test_asset_delete_cannot_race_thumbnail_save() -> None:
    asset = make_asset("asset-delete-race", "task-delete-race", "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-delete-race/thumb_source.png.jpg"
    db = AssetDeleteDB(asset)
    store = BlockingThumbnailSaveStore(derived_key)
    with temporary_local_store(store):
        seed_local(store, source_key, patterned_png((500, 250)))
        thumbnail_request = asyncio.create_task(call_thumb(asset))
        await wait_for_thread_event(store.save_started)
        deletion = asyncio.create_task(
            assets_api.delete_asset(asset.id, user=ADMIN, db=db)
        )
        try:
            completed_early = await completed_while_generation_blocked(deletion)
        finally:
            store.release_save.set()
        await thumbnail_request
        await deletion

        assert not completed_early, "asset deletion committed before generation settled"
        assert not store.exists(source_key)
        assert not store.exists(derived_key), "thumbnail reappeared after asset deletion"


async def test_cancelled_asset_delete_finishes_mutation_before_propagating() -> None:
    asset = make_asset("asset-delete-cancel", "task-delete-cancel", "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-delete-cancel/thumb_source.png.jpg"
    db = AssetDeleteDB(asset)
    store = BlockingCleanupStore(source_key, derived_key)
    with temporary_local_store(store):
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumbnail")
        deletion = asyncio.create_task(
            assets_api.delete_asset(asset.id, user=ADMIN, db=db)
        )
        await wait_for_thread_event(store.cleanup_started)

        deletion.cancel()
        await asyncio.sleep(0)
        cancellation_waited = not deletion.done()
        lifecycle_acquired = asyncio.Event()
        contender = asyncio.create_task(
            contend_for_lifecycle(derived_key, lifecycle_acquired)
        )
        await asyncio.sleep(0)
        lock_retained = not lifecycle_acquired.is_set()

        store.release_cleanup.set()
        await wait_for_thread_event(store.cleanup_finished)
        try:
            await deletion
        except asyncio.CancelledError:
            cancellation_propagated = True
        else:
            cancellation_propagated = False
        await contender

        assert cancellation_waited, "asset delete propagated cancellation before cleanup"
        assert lock_retained, "asset delete released lifecycle ownership during cleanup"
        assert cancellation_propagated
        assert db.deleted == [asset] and db.commits == 1
        assert not store.exists(source_key)
        assert not store.exists(derived_key)


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


async def test_zero_asset_task_delete_uses_task_lifecycle() -> None:
    task = make_task("task-delete-empty")
    db = TaskMutationDB(task, [])
    lifecycle_key = getattr(thumbs, "task_lifecycle_key")(task.id)

    async with thumbs.thumbnail_lifecycle([lifecycle_key]):
        deletion = asyncio.create_task(tasks_api.delete_task(task.id, user=ADMIN, db=db))
        await asyncio.sleep(0)
        serialized = not deletion.done() and db.gets == 0 and db.commits == 0

    body = await deletion
    assert serialized, "zero-asset task deletion bypassed task lifecycle ownership"
    assert body["assets_deleted"] == 0
    assert db.deleted == [task] and db.commits == 1
    assert db.gets == 1 and db.refreshes == 0


async def test_task_delete_rejects_terminal_runner_before_cleanup() -> None:
    task = make_task("task-delete-finishing", status="error")
    asset = make_asset("asset-delete-finishing", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-delete-finishing/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumbnail")
        tasks_api._running_tasks.add(task.id)
        try:
            error = await expect_http(
                tasks_api.delete_task(task.id, user=ADMIN, db=db),
                409,
            )
        finally:
            tasks_api._running_tasks.discard(task.id)

        assert "finishing" in str(error.detail).lower()
        assert db.gets == 1 and db.refreshes == 0
        assert db.deleted == [] and db.commits == 0
        assert store.exists(source_key)
        assert store.exists(derived_key)


async def test_task_delete_cannot_race_thumbnail_save() -> None:
    task = make_task("task-delete-race")
    asset = make_asset("asset-task-delete-race", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-delete-race/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    store = BlockingThumbnailSaveStore(derived_key)
    with temporary_local_store(store):
        seed_local(store, source_key, patterned_png((500, 250)))
        thumbnail_request = asyncio.create_task(call_thumb(asset))
        await wait_for_thread_event(store.save_started)
        deletion = asyncio.create_task(tasks_api.delete_task(task.id, user=ADMIN, db=db))
        try:
            completed_early = await completed_while_generation_blocked(deletion)
        finally:
            store.release_save.set()
        await thumbnail_request
        await deletion

        assert not completed_early, "task deletion committed before generation settled"
        assert not store.exists(source_key)
        assert not store.exists(derived_key), "thumbnail reappeared after task deletion"


async def test_cancelled_task_delete_finishes_mutation_before_propagating() -> None:
    task = make_task("task-delete-cancel")
    asset = make_asset("asset-task-delete-cancel", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-delete-cancel/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    store = BlockingCleanupStore(source_key, derived_key)
    with temporary_local_store(store):
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumbnail")
        deletion = asyncio.create_task(tasks_api.delete_task(task.id, user=ADMIN, db=db))
        await wait_for_thread_event(store.cleanup_started)

        deletion.cancel()
        await asyncio.sleep(0)
        cancellation_waited = not deletion.done()
        lifecycle_acquired = asyncio.Event()
        contender = asyncio.create_task(
            contend_for_lifecycle(derived_key, lifecycle_acquired)
        )
        await asyncio.sleep(0)
        lock_retained = not lifecycle_acquired.is_set()

        store.release_cleanup.set()
        await wait_for_thread_event(store.cleanup_finished)
        try:
            await deletion
        except asyncio.CancelledError:
            cancellation_propagated = True
        else:
            cancellation_propagated = False
        await contender

        assert cancellation_waited, "task delete propagated cancellation before cleanup"
        assert lock_retained, "task delete released lifecycle ownership during cleanup"
        assert cancellation_propagated
        assert db.deleted == [asset, task] and db.commits == 1
        assert not store.exists(source_key)
        assert not store.exists(derived_key)


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
        assert db.deleted == [asset] and db.commits == 1
        assert db.gets == 1 and db.refreshes == 1
        assert spawned == [task.id]


async def test_retry_rejects_terminal_runner_before_cleanup() -> None:
    task = make_task("task-retry-finishing", status="error")
    asset = make_asset("asset-retry-finishing", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-retry-finishing/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    with temporary_local_store() as raw_store:
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumbnail")
        tasks_api._running_tasks.add(task.id)
        try:
            error = await expect_http(
                tasks_api.retry_task(task.id, user=ADMIN, db=db),
                409,
            )
        finally:
            tasks_api._running_tasks.discard(task.id)

        assert "finishing" in str(error.detail).lower()
        assert task.status == "error"
        assert db.gets == 1 and db.refreshes == 0
        assert db.deleted == [] and db.commits == 0
        assert store.exists(source_key)
        assert store.exists(derived_key)


async def test_retry_reloads_assets_after_runner_finishes() -> None:
    task = make_task("task-retry-final-asset", status="error")
    db = TaskMutationDB(task, [])
    asset = make_asset("asset-retry-final", task.id, "final.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-retry-final-asset/thumb_final.png.jpg"
    spawned: list[str] = []

    async def spawn(task_id: str) -> bool:
        spawned.append(task_id)
        return True

    lifecycle_key = thumbs.task_lifecycle_key(task.id)
    with temporary_local_store() as raw_store, patched(tasks_api, spawn_task=spawn):
        store = raw_store
        assert isinstance(store, TrackingLocalFileStore)
        tasks_api._running_tasks.add(task.id)
        try:
            async with thumbs.thumbnail_lifecycle([lifecycle_key]):
                retry = asyncio.create_task(
                    tasks_api.retry_task(task.id, user=ADMIN, db=db)
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                db.assets = [asset]
                seed_local(store, source_key, b"final source")
                seed_local(store, derived_key, b"final thumbnail")
                tasks_api._running_tasks.discard(task.id)
            await retry
        finally:
            tasks_api._running_tasks.discard(task.id)

        assert db.deleted == [asset]
        assert spawned == [task.id]
        assert not store.exists(source_key)
        assert not store.exists(derived_key)


async def test_concurrent_zero_asset_retries_spawn_once() -> None:
    task = make_task("task-retry-concurrent", status="error")
    db = ConcurrentRetryDB(task)
    spawned: list[str] = []
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def spawn(task_id: str) -> bool:
        spawned.append(task_id)
        spawn_started.set()
        await release_spawn.wait()
        return True

    async def invoke_retry():
        try:
            return await tasks_api.retry_task(task.id, user=ADMIN, db=db)
        except HTTPException as exc:
            return exc

    with patched(tasks_api, spawn_task=spawn):
        first = asyncio.create_task(invoke_retry())
        second = asyncio.create_task(invoke_retry())
        await spawn_started.wait()
        try:
            await wait_until(lambda: len(spawned) == 2, timeout=0.25)
        except TimeoutError:
            pass
        release_spawn.set()
        results = await asyncio.gather(first, second)

    failures = [result for result in results if isinstance(result, HTTPException)]
    successes = [result for result in results if isinstance(result, Task)]
    assert len(successes) == 1, results
    assert len(failures) == 1 and failures[0].status_code == 409, results
    assert spawned == [task.id], spawned
    assert db.deleted == [] and db.commits == 1
    assert db.get_calls == 2 and db.gets == 2 and db.refreshes == 1
    assert task.status == "pending"


async def test_retry_spawn_refusal_persists_recoverable_error() -> None:
    task = make_task("task-retry-spawn-refused", status="error")
    db = TaskMutationDB(task, [])
    spawn_calls: list[str] = []

    async def refuse_spawn(task_id: str) -> bool:
        spawn_calls.append(task_id)
        return False

    with patched(tasks_api, spawn_task=refuse_spawn):
        error = await expect_http(
            tasks_api.retry_task(task.id, user=ADMIN, db=db),
            409,
        )

    assert "start" in str(error.detail).lower()
    assert spawn_calls == [task.id]
    assert task.status == "error" and task.error_msg
    assert db.commits == 2 and db.gets == 1 and db.refreshes == 2


async def test_retry_cannot_race_thumbnail_save() -> None:
    task = make_task("task-retry-race", status="error")
    asset = make_asset("asset-retry-race", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-retry-race/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    store = BlockingThumbnailSaveStore(derived_key)
    spawned: list[str] = []

    async def spawn(task_id: str) -> bool:
        spawned.append(task_id)
        return True

    with temporary_local_store(store), patched(tasks_api, spawn_task=spawn):
        seed_local(store, source_key, patterned_png((500, 250)))
        thumbnail_request = asyncio.create_task(call_thumb(asset))
        await wait_for_thread_event(store.save_started)
        retry = asyncio.create_task(tasks_api.retry_task(task.id, user=ADMIN, db=db))
        try:
            completed_early = await completed_while_generation_blocked(retry)
        finally:
            store.release_save.set()
        await thumbnail_request
        await retry

        assert not completed_early, "retry committed before generation settled"
        assert not store.exists(source_key)
        assert not store.exists(derived_key), "thumbnail reappeared after retry cleanup"
        assert spawned == [task.id]


async def test_cancelled_retry_finishes_and_spawns_before_propagating() -> None:
    task = make_task("task-retry-cancel", status="error")
    asset = make_asset("asset-retry-cancel", task.id, "source.png", "image/png")
    source_key = asset.file_path
    derived_key = "task-retry-cancel/thumb_source.png.jpg"
    db = TaskMutationDB(task, [asset])
    store = BlockingCleanupStore(source_key, derived_key)
    spawned: list[str] = []
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def spawn(task_id: str) -> bool:
        spawned.append(task_id)
        spawn_started.set()
        await release_spawn.wait()
        return True

    with temporary_local_store(store), patched(tasks_api, spawn_task=spawn):
        seed_local(store, source_key, b"source")
        seed_local(store, derived_key, b"thumbnail")
        retry = asyncio.create_task(tasks_api.retry_task(task.id, user=ADMIN, db=db))
        await wait_for_thread_event(store.cleanup_started)

        retry.cancel()
        await asyncio.sleep(0)
        cancellation_waited = not retry.done()
        lifecycle_acquired = asyncio.Event()
        contender = asyncio.create_task(
            contend_for_lifecycle(derived_key, lifecycle_acquired)
        )
        await asyncio.sleep(0)
        lock_retained_during_cleanup = not lifecycle_acquired.is_set()

        store.release_cleanup.set()
        await wait_for_thread_event(store.cleanup_finished)
        spawn_waiter = asyncio.create_task(spawn_started.wait())
        done, _pending = await asyncio.wait(
            {spawn_waiter, retry},
            timeout=2,
            return_when=asyncio.FIRST_COMPLETED,
        )
        spawn_reached = spawn_started.is_set()
        lock_retained_through_spawn = not lifecycle_acquired.is_set()
        release_spawn.set()
        if spawn_waiter not in done:
            spawn_waiter.cancel()
            try:
                await spawn_waiter
            except asyncio.CancelledError:
                pass
        try:
            await retry
        except asyncio.CancelledError:
            cancellation_propagated = True
        else:
            cancellation_propagated = False
        await contender

        assert cancellation_waited, "retry propagated cancellation before cleanup"
        assert lock_retained_during_cleanup, "retry released lifecycle lock during cleanup"
        assert spawn_reached, "retry was left pending without spawning"
        assert lock_retained_through_spawn, "retry released lifecycle lock before spawn"
        assert cancellation_propagated
        assert db.deleted == [asset] and db.commits == 1
        assert db.gets == 1 and db.refreshes == 1
        assert task.status == "pending"
        assert spawned == [task.id]
        assert not store.exists(source_key)
        assert not store.exists(derived_key)


async def test_delete_then_retry_loads_current_task_inside_lifecycle() -> None:
    task_id = "task-sqlite-delete-first"
    task = make_task(task_id, status="error")
    asset = make_asset(
        "asset-sqlite-delete-first",
        task_id,
        "source.png",
        "image/png",
    )
    source_key = asset.file_path
    derived_key = f"{task_id}/thumb_source.png.jpg"
    lifecycle_key = thumbs.task_lifecycle_key(task_id)
    spawned: list[str] = []

    async def spawn(candidate_id: str) -> bool:
        spawned.append(candidate_id)
        return True

    async with temporary_sqlite_sessions() as session_factory:
        await seed_sqlite_task(session_factory, task, asset)
        with temporary_local_store() as raw_store, patched(tasks_api, spawn_task=spawn):
            store = raw_store
            assert isinstance(store, TrackingLocalFileStore)
            seed_local(store, source_key, b"source")
            seed_local(store, derived_key, b"thumbnail")

            async with session_factory() as delete_db, session_factory() as retry_db:
                async with thumbs.thumbnail_lifecycle([lifecycle_key]):
                    deletion = asyncio.create_task(
                        tasks_api.delete_task(task_id, user=ADMIN, db=delete_db)
                    )
                    await wait_until(
                        lambda: lifecycle_waiter_count(lifecycle_key) >= 1
                    )
                    retry = asyncio.create_task(
                        tasks_api.retry_task(task_id, user=ADMIN, db=retry_db)
                    )
                    await wait_until(
                        lambda: lifecycle_waiter_count(lifecycle_key) >= 2
                    )

                delete_result, retry_result = await asyncio.gather(
                    deletion,
                    retry,
                    return_exceptions=True,
                )

            assert isinstance(delete_result, dict), type(delete_result)
            assert delete_result["assets_deleted"] == 1
            assert isinstance(retry_result, HTTPException), type(retry_result)
            assert retry_result.status_code == 404, retry_result.status_code
            assert spawned == [], spawned
            assert store.delete_calls == [source_key, derived_key], store.delete_calls
            assert not store.exists(source_key)
            assert not store.exists(derived_key)

        async with session_factory() as verification_db:
            assert await verification_db.get(Task, task_id) is None
            assert await verification_db.get(Asset, asset.id) is None


async def test_retry_then_delete_loads_current_task_inside_lifecycle() -> None:
    task_id = "task-sqlite-retry-first"
    task = make_task(task_id, status="error")
    asset = make_asset(
        "asset-sqlite-retry-first",
        task_id,
        "source.png",
        "image/png",
    )
    source_key = asset.file_path
    derived_key = f"{task_id}/thumb_source.png.jpg"
    lifecycle_key = thumbs.task_lifecycle_key(task_id)
    spawned: list[str] = []

    async def spawn(candidate_id: str) -> bool:
        spawned.append(candidate_id)
        return True

    async with temporary_sqlite_sessions() as session_factory:
        await seed_sqlite_task(session_factory, task, asset)
        with temporary_local_store() as raw_store, patched(tasks_api, spawn_task=spawn):
            store = raw_store
            assert isinstance(store, TrackingLocalFileStore)
            seed_local(store, source_key, b"source")
            seed_local(store, derived_key, b"thumbnail")

            async with session_factory() as retry_db, session_factory() as delete_db:
                async with thumbs.thumbnail_lifecycle([lifecycle_key]):
                    retry = asyncio.create_task(
                        tasks_api.retry_task(task_id, user=ADMIN, db=retry_db)
                    )
                    await wait_until(
                        lambda: lifecycle_waiter_count(lifecycle_key) >= 1
                    )
                    deletion = asyncio.create_task(
                        tasks_api.delete_task(task_id, user=ADMIN, db=delete_db)
                    )
                    await wait_until(
                        lambda: lifecycle_waiter_count(lifecycle_key) >= 2
                    )

                retry_result, delete_result = await asyncio.gather(
                    retry,
                    deletion,
                    return_exceptions=True,
                )

            assert isinstance(retry_result, Task), type(retry_result)
            assert retry_result.status == "pending", retry_result.status
            assert isinstance(delete_result, HTTPException), type(delete_result)
            assert delete_result.status_code == 409, delete_result.status_code
            assert spawned == [task_id], spawned
            assert store.delete_calls == [source_key, derived_key], store.delete_calls
            assert not store.exists(source_key)
            assert not store.exists(derived_key)

        async with session_factory() as verification_db:
            persisted_task = await verification_db.get(Task, task_id)
            assert persisted_task is not None
            assert persisted_task.status == "pending", persisted_task.status
            assert await verification_db.get(Asset, asset.id) is None


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
    ("lifecycle lock registry releases unused keys", test_lifecycle_lock_registry_releases_unused_keys),
    ("lifecycle keys are acquired in sorted order", test_lifecycle_keys_are_acquired_in_sorted_order),
    ("failed child overrides earlier caller cancellation", test_cancelled_parent_observes_failed_child),
    ("image generation persists through FileStore off-loop", test_image_generation_persists_and_uses_worker_thread),
    ("existing persistent thumbnail is reused", test_existing_thumbnail_is_reused_without_source_read),
    ("concurrent image requests generate exactly once", test_concurrent_requests_generate_once),
    ("stale second miss does not regenerate a thumbnail", test_stale_second_miss_does_not_regenerate_thumbnail),
    ("request cancellation does not cancel or leak shared generation", test_cancelled_waiter_does_not_cancel_or_leak_generation),
    ("failed image generation returns 404 and clears in-flight state", test_failed_image_generation_returns_404_and_cleans_inflight),
    ("missing video thumbnail does not read source", test_video_without_thumbnail_never_reads_source),
    ("audio never generates a thumbnail", test_audio_never_generates_thumbnail),
    ("ETag, no-cache, and 304 revalidation work", test_etag_cache_headers_and_revalidation),
    ("missing image revalidation skips all FileStore work", test_missing_image_matching_etag_skips_filestore),
    ("existing video revalidation returns 304", test_existing_video_matching_etag_returns_304),
    ("object-backed thumbnail streams and closes its handle", test_object_thumbnail_streams_and_closes_handle),
    ("asset deletion removes source and thumbnail", test_asset_delete_removes_source_and_thumbnail),
    ("asset deletion cannot race thumbnail persistence", test_asset_delete_cannot_race_thumbnail_save),
    ("cancelled asset deletion finishes before propagating", test_cancelled_asset_delete_finishes_mutation_before_propagating),
    ("task deletion removes sources and thumbnails", test_task_delete_removes_sources_and_thumbnails),
    ("zero-asset task deletion uses task lifecycle", test_zero_asset_task_delete_uses_task_lifecycle),
    ("task deletion rejects a finishing terminal runner", test_task_delete_rejects_terminal_runner_before_cleanup),
    ("task deletion cannot race thumbnail persistence", test_task_delete_cannot_race_thumbnail_save),
    ("cancelled task deletion finishes before propagating", test_cancelled_task_delete_finishes_mutation_before_propagating),
    ("task retry removes sources and thumbnails", test_retry_removes_sources_and_thumbnails),
    ("retry rejects a finishing terminal runner", test_retry_rejects_terminal_runner_before_cleanup),
    ("retry reloads assets after runner finishes", test_retry_reloads_assets_after_runner_finishes),
    ("concurrent zero-asset retries spawn once", test_concurrent_zero_asset_retries_spawn_once),
    ("retry spawn refusal persists recoverable error", test_retry_spawn_refusal_persists_recoverable_error),
    ("task retry cannot race thumbnail persistence", test_retry_cannot_race_thumbnail_save),
    ("cancelled retry finishes and spawns before propagating", test_cancelled_retry_finishes_and_spawns_before_propagating),
    ("delete then retry reloads the task under lifecycle ownership", test_delete_then_retry_loads_current_task_inside_lifecycle),
    ("retry then delete reloads the task under lifecycle ownership", test_retry_then_delete_loads_current_task_inside_lifecycle),
    ("cleanup failures are reported without restoring DB records", test_cleanup_failures_do_not_restore_database_records),
]


async def main() -> int:
    print("=== Persistent thumbnail and derived-file reliability ===")
    with patched(tasks_api, task_owner_map=owner_map):
        for name, test in TESTS:
            await run_case(name, test)
    print(f"\n{PASS} passed, {FAIL} failed")
    return int(FAIL != 0)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
