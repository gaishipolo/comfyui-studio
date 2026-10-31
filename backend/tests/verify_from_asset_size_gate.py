"""Behavior checks for size-safe, isolated asset forwarding.

This script calls the route function with in-memory fakes. It never starts the
service, opens the configured database, or touches the configured FileStore.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import uploads as uploads_api
from app.filestore import ObjectHandle
from app.models.models import Asset
from app.schemas.schemas import AssetUploadRequest
from provider_test_utils import lease_acquirer


MB = 1024 * 1024
OWNER = SimpleNamespace(id="owner", role="member")
OTHER_USER = SimpleNamespace(id="other", role="member")


class FakeDb:
    def __init__(self, asset):
        self.asset = asset

    async def get(self, model, asset_id):
        assert model is Asset
        if self.asset is None or self.asset.id != asset_id:
            return None
        return self.asset


class FakeProvider:
    def __init__(self):
        self.calls: list[tuple[bytes, str]] = []

    async def upload_input(self, data: bytes, name: str) -> str:
        self.calls.append((data, name))
        return f"engine-{name}"


class ReportedSizeBytes(bytes):
    """Small allocation with a synthetic length for exact-limit checks."""

    def __new__(cls, data: bytes, reported_size: int):
        value = super().__new__(cls, data)
        value.reported_size = reported_size
        return value

    def __len__(self) -> int:
        return self.reported_size


def make_asset(
    *,
    asset_id: str = "asset-id",
    file_size: int = 1,
    mime_type: str = "image/png",
):
    return SimpleNamespace(
        id=asset_id,
        task_id="task-id",
        file_path="task-id/input.png",
        file_name="input.png",
        file_size=file_size,
        mime_type=mime_type,
        is_shared=False,
    )


async def owner_map(_db, task_ids: set[str]) -> dict[str, str]:
    return {task_id: "owner" for task_id in task_ids}


@contextmanager
def route_dependencies(*, provider, open_asset, read_asset_bytes, owners=owner_map):
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            uploads_api,
            "acquire_provider",
            lease_acquirer(lambda _engine: provider),
        ))
        # create=True makes the RED run reach an assertion even before production
        # imports open_asset.
        stack.enter_context(patch.object(uploads_api, "open_asset", open_asset, create=True))
        stack.enter_context(patch.object(uploads_api, "read_asset_bytes", read_asset_bytes))
        stack.enter_context(patch.object(uploads_api, "task_owner_map", owners))
        yield


async def call_route(asset, *, provider, open_asset, read_asset_bytes, user=OWNER):
    with route_dependencies(
        provider=provider,
        open_asset=open_asset,
        read_asset_bytes=read_asset_bytes,
    ):
        return await uploads_api.upload_from_asset(
            body=AssetUploadRequest(asset_id="asset-id", engine="fake"),
            user=user,
            db=FakeDb(asset),
        )


async def expect_http(coro, status: int, detail: str) -> HTTPException:
    try:
        await coro
    except HTTPException as exc:
        assert exc.status_code == status, (exc.status_code, exc.detail)
        assert detail in str(exc.detail), exc.detail
        return exc
    raise AssertionError(f"expected HTTP {status}")


async def test_authoritative_size_rejects_stale_metadata_before_read() -> None:
    limit = 20 * MB
    for db_size in (0, 1, limit - 1):
        provider = FakeProvider()
        closed: list[int] = []

        def open_large(_path: str):
            return ObjectHandle(size=limit + 1, _cleanup=lambda: closed.append(db_size))

        def forbidden_read(_path: str):
            raise AssertionError(f"bytes were read for stale DB size {db_size}")

        await expect_http(
            call_route(
                make_asset(file_size=db_size),
                provider=provider,
                open_asset=open_large,
                read_asset_bytes=forbidden_read,
            ),
            400,
            "20MB",
        )
        assert provider.calls == [], provider.calls
        assert closed == [db_size], closed


async def test_oversized_metadata_rejects_before_filestore_or_provider() -> None:
    provider = FakeProvider()

    def forbidden_file_work(_path: str):
        raise AssertionError("oversized DB metadata must reject before FileStore access")

    await expect_http(
        call_route(
            make_asset(file_size=20 * MB + 1),
            provider=provider,
            open_asset=forbidden_file_work,
            read_asset_bytes=forbidden_file_work,
        ),
        400,
        "20MB",
    )
    assert provider.calls == [], provider.calls


async def test_post_read_size_rejects_growth_before_provider() -> None:
    limit = 20 * MB
    provider = FakeProvider()
    closed: list[str] = []
    data = ReportedSizeBytes(b"larger-after-read", limit + 1)

    def read_after_close(_path: str):
        assert closed == ["closed"], "metadata handle must close before reading bytes"
        return data

    await expect_http(
        call_route(
            make_asset(file_size=1),
            provider=provider,
            open_asset=lambda _path: ObjectHandle(
                size=limit,
                _cleanup=lambda: closed.append("closed"),
            ),
            read_asset_bytes=read_after_close,
        ),
        400,
        "20MB",
    )
    assert provider.calls == [], provider.calls
    assert closed == ["closed"], closed


async def test_missing_handle_is_404_without_read_or_provider() -> None:
    provider = FakeProvider()

    def forbidden_read(_path: str):
        raise AssertionError("missing object must not be read")

    await expect_http(
        call_route(
            make_asset(),
            provider=provider,
            open_asset=lambda _path: None,
            read_asset_bytes=forbidden_read,
        ),
        404,
        "素材文件找不到了",
    )
    assert provider.calls == [], provider.calls


async def test_exact_size_limits_are_accepted_and_forwarded_once() -> None:
    for mime_type, limit in (("image/png", 20 * MB), ("audio/wav", 50 * MB), ("video/mp4", 200 * MB)):
        provider = FakeProvider()
        closed: list[str] = []
        data = ReportedSizeBytes(b"at-limit", limit)

        def read_after_close(_path: str):
            assert closed == [mime_type]
            return data

        result = await call_route(
            make_asset(file_size=limit, mime_type=mime_type),
            provider=provider,
            open_asset=lambda _path, size=limit, mime=mime_type: ObjectHandle(
                size=size,
                _cleanup=lambda: closed.append(mime),
            ),
            read_asset_bytes=read_after_close,
        )

        assert result == {"filename": "engine-input.png", "engine": "fake"}, result
        assert provider.calls == [(data, "input.png")], provider.calls
        assert closed == [mime_type], closed


async def test_valid_file_is_forwarded_once_after_handle_close() -> None:
    provider = FakeProvider()
    data = b"valid-image"
    closed: list[str] = []

    def read_after_close(path: str):
        assert path == "task-id/input.png"
        assert closed == [path], "ObjectHandle was not closed before the full read"
        return data

    result = await call_route(
        make_asset(file_size=0),
        provider=provider,
        open_asset=lambda path: ObjectHandle(size=len(data), _cleanup=lambda: closed.append(path)),
        read_asset_bytes=read_after_close,
    )

    assert result == {"filename": "engine-input.png", "engine": "fake"}, result
    assert provider.calls == [(data, "input.png")], provider.calls
    assert closed == ["task-id/input.png"], closed


async def test_permissions_mime_and_missing_bytes_keep_existing_contract() -> None:
    def forbidden_file_work(_path: str):
        raise AssertionError("file work must not run")

    denied_provider = FakeProvider()
    await expect_http(
        call_route(
            make_asset(),
            provider=denied_provider,
            open_asset=forbidden_file_work,
            read_asset_bytes=forbidden_file_work,
            user=OTHER_USER,
        ),
        404,
        "素材不存在",
    )
    assert denied_provider.calls == []

    mime_provider = FakeProvider()
    await expect_http(
        call_route(
            make_asset(mime_type="text/plain"),
            provider=mime_provider,
            open_asset=forbidden_file_work,
            read_asset_bytes=forbidden_file_work,
        ),
        400,
        "该素材类型暂不支持",
    )
    assert mime_provider.calls == []

    missing_provider = FakeProvider()
    closed: list[str] = []
    await expect_http(
        call_route(
            make_asset(),
            provider=missing_provider,
            open_asset=lambda _path: ObjectHandle(
                size=1,
                _cleanup=lambda: closed.append("closed"),
            ),
            read_asset_bytes=lambda _path: None,
        ),
        404,
        "素材文件找不到了",
    )
    assert missing_provider.calls == []
    assert closed == ["closed"]


async def main() -> None:
    tests = [
        test_authoritative_size_rejects_stale_metadata_before_read,
        test_oversized_metadata_rejects_before_filestore_or_provider,
        test_post_read_size_rejects_growth_before_provider,
        test_missing_handle_is_404_without_read_or_provider,
        test_exact_size_limits_are_accepted_and_forwarded_once,
        test_valid_file_is_forwarded_once_after_handle_close,
        test_permissions_mime_and_missing_bytes_keep_existing_contract,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            await test()
        except Exception as exc:  # noqa: BLE001 - verification runner reports every case
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"[PASS] {test.__name__}")

    if failures:
        raise AssertionError(f"{len(failures)} from-asset verification(s) failed")


if __name__ == "__main__":
    asyncio.run(main())
