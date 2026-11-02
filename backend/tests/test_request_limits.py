"""Pure-ASGI checks for protected request admission before body parsing."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import create_token  # noqa: E402
from app.request_limits import ProtectedBodyLimitMiddleware  # noqa: E402


MiB = 1024 * 1024
VALID_TOKEN = create_token(
    SimpleNamespace(id="request-limit-user", username="request-limit", role="member")
)


def scope_for(
    path: str,
    *,
    method: str = "POST",
    token: str | None = VALID_TOKEN,
    content_length: int | str | None = None,
    query: dict[str, str] | None = None,
) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": urlencode(query or {}).encode("ascii"),
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }


class Receive:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.calls = 0

    async def __call__(self) -> dict:
        self.calls += 1
        body = self.chunks.pop(0) if self.chunks else b""
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(self.chunks),
        }


class RecordingApp:
    def __init__(self):
        self.calls = 0
        self.bodies: list[bytes] = []

    async def __call__(self, _scope, receive, send) -> None:
        self.calls += 1
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.bodies.append(b"".join(chunks))
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [],
        })
        await send({"type": "http.response.body", "body": b""})


async def invoke(scope: dict, chunks: list[bytes]):
    downstream = RecordingApp()
    receive = Receive(chunks)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await ProtectedBodyLimitMiddleware(downstream)(scope, receive, send)
    status = next(
        (message["status"] for message in sent if message["type"] == "http.response.start"),
        None,
    )
    raw = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    detail = json.loads(raw.decode("utf-8")) if raw else None
    return downstream, receive, status, detail


async def test_missing_bearer_is_rejected_without_consuming_body() -> None:
    app, receive, status, detail = await invoke(
        scope_for("/api/upload/file", token=None, content_length=4),
        [b"body"],
    )
    assert status == 401, status
    assert detail == {"detail": "Invalid or missing bearer token"}, detail
    assert receive.calls == 0, receive.calls
    assert app.calls == 0, app.calls


async def test_invalid_bearer_is_rejected_without_consuming_body() -> None:
    app, receive, status, detail = await invoke(
        scope_for("/api/assets/batch-download", token="not-a-jwt", content_length=2),
        [b"[]"],
    )
    assert status == 401, status
    assert detail == {"detail": "Invalid or missing bearer token"}, detail
    assert receive.calls == 0, receive.calls
    assert app.calls == 0, app.calls


async def test_oversized_content_length_never_reaches_body_or_app() -> None:
    cases = [
        ("/api/upload/image", 21 * MiB + 1, None),
        ("/api/upload/file", 51 * MiB + 1, {"kind": "audio"}),
        ("/api/upload/file", 201 * MiB + 1, {"kind": "video"}),
        ("/api/assets/batch-download", 64 * 1024 + 1, None),
        ("/api/assets/batch-download/prepare", 64 * 1024 + 1, None),
    ]
    for path, length, query in cases:
        app, receive, status, detail = await invoke(
            scope_for(path, content_length=length, query=query),
            [b"must-not-be-read"],
        )
        assert status == 413, (path, length, status)
        assert detail == {"detail": "Request body too large"}, detail
        assert receive.calls == 0, (path, receive.calls)
        assert app.calls == 0, (path, app.calls)


async def test_invalid_or_duplicate_content_length_is_400() -> None:
    scopes = [
        scope_for("/api/upload/file", content_length="invalid", query={"kind": "image"}),
        scope_for("/api/assets/batch-download", content_length=-1),
    ]
    duplicate = scope_for("/api/assets/batch-download", content_length=2)
    duplicate["headers"].append((b"content-length", b"2"))
    scopes.append(duplicate)

    for scope in scopes:
        app, receive, status, detail = await invoke(scope, [b"must-not-be-read"])
        assert status == 400, (scope["headers"], status)
        assert detail == {"detail": "Invalid Content-Length"}, detail
        assert receive.calls == 0
        assert app.calls == 0


async def test_chunked_overflow_is_consumed_only_by_gate_and_never_reaches_app() -> None:
    chunks = [b"x" * (32 * 1024), b"y" * (32 * 1024), b"z"]
    app, receive, status, detail = await invoke(
        scope_for("/api/assets/batch-download"),
        chunks,
    )
    assert status == 413, status
    assert detail == {"detail": "Request body too large"}, detail
    assert receive.calls == 3, receive.calls
    assert app.calls == 0, app.calls


async def test_exact_batch_limit_and_exact_file_payload_reach_app_unchanged() -> None:
    batch_body = b" " * (64 * 1024)
    app, _receive, status, _detail = await invoke(
        scope_for("/api/assets/batch-download", content_length=len(batch_body)),
        [batch_body[:123], batch_body[123:]],
    )
    assert status == 204, status
    assert app.bodies == [batch_body]

    boundary = b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.png\"\r\n\r\n"
    upload_body = boundary + (b"x" * (20 * MiB)) + b"\r\n--boundary--\r\n"
    app, _receive, status, _detail = await invoke(
        scope_for("/api/upload/file", content_length=len(upload_body), query={"kind": "image"}),
        [upload_body],
    )
    assert status == 204, status
    assert app.bodies == [upload_body]


async def test_audio_video_raw_allowance_boundaries_reach_app() -> None:
    for kind, length in (("audio", 51 * MiB), ("video", 201 * MiB)):
        app, receive, status, _detail = await invoke(
            scope_for(
                "/api/upload/file",
                content_length=length,
                query={"kind": kind},
            ),
            [b"small-test-body"],
        )
        assert status == 204, (kind, status)
        assert receive.calls == 1, (kind, receive.calls)
        assert app.bodies == [b"small-test-body"]


async def test_prepare_batch_endpoint_shares_batch_limit() -> None:
    # /prepare 是现行批量下载入口：必须和旧 /batch-download 同一道 64KB 闸
    oversized = b"[" + b'"0123456789abcdef",' * (20 * 1024) + b"]"  # >64KB
    app, receive, status, detail = await invoke(
        scope_for("/api/assets/batch-download/prepare", content_length=len(oversized)),
        [oversized[:123], oversized[123:]],
    )
    assert status == 413, (len(oversized), status)
    assert detail == {"detail": "Request body too large"}, detail
    assert app.calls == 0, app.calls

    prepare_body = b" " * (64 * 1024)
    app, _receive, status, _detail = await invoke(
        scope_for("/api/assets/batch-download/prepare", content_length=len(prepare_body)),
        [prepare_body[:123], prepare_body[123:]],
    )
    assert status == 204, status
    assert app.bodies == [prepare_body]

    app, receive, status, detail = await invoke(
        scope_for("/api/assets/batch-download/prepare", token=None, content_length=4),
        [b"[]"],
    )
    assert status == 401, status
    assert receive.calls == 0
    assert app.calls == 0


async def test_options_and_non_target_paths_are_unchanged() -> None:
    cases = [
        scope_for("/api/upload/file", method="OPTIONS", token=None, content_length=4),
        scope_for("/api/upload/from-asset", token=None, content_length=4),
        scope_for("/api/assets/batch-download", method="GET", token=None, content_length=4),
    ]
    for scope in cases:
        app, receive, status, _detail = await invoke(scope, [b"body"])
        assert status == 204, (scope["method"], scope["path"], status)
        assert receive.calls == 1
        assert app.bodies == [b"body"]


async def main() -> int:
    tests = [
        test_missing_bearer_is_rejected_without_consuming_body,
        test_invalid_bearer_is_rejected_without_consuming_body,
        test_oversized_content_length_never_reaches_body_or_app,
        test_invalid_or_duplicate_content_length_is_400,
        test_chunked_overflow_is_consumed_only_by_gate_and_never_reaches_app,
        test_exact_batch_limit_and_exact_file_payload_reach_app_unchanged,
        test_prepare_batch_endpoint_shares_batch_limit,
        test_audio_video_raw_allowance_boundaries_reach_app,
        test_options_and_non_target_paths_are_unchanged,
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
