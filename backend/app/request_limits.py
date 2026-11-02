"""Request admission for large protected bodies before FastAPI parses them."""

from __future__ import annotations

import asyncio
import json
import tempfile
from urllib.parse import parse_qs

from app.auth import decode_token


MiB = 1024 * 1024
_MULTIPART_OVERHEAD = 1 * MiB
_UPLOAD_RAW_LIMITS = {
    "image": 20 * MiB + _MULTIPART_OVERHEAD,
    "audio": 50 * MiB + _MULTIPART_OVERHEAD,
    "video": 200 * MiB + _MULTIPART_OVERHEAD,
}
_BATCH_JSON_RAW_LIMIT = 64 * 1024
_SPOOL_MEMORY_LIMIT = 1 * MiB
_REPLAY_CHUNK_SIZE = 1 * MiB

_UNAUTHORIZED_DETAIL = "Invalid or missing bearer token"
_TOO_LARGE_DETAIL = "Request body too large"
_INVALID_CONTENT_LENGTH_DETAIL = "Invalid Content-Length"


def _raw_limit(scope: dict) -> int | None:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return None

    path = scope.get("path")
    if path == "/api/assets/batch-download":
        return _BATCH_JSON_RAW_LIMIT
    # /prepare 是现行批量下载入口（生成 ZIP + 票据），body 是 asset_ids JSON 数组，
    # 与旧 /batch-download 同一道闸：50 个 UUID 远小于 64KB
    if path == "/api/assets/batch-download/prepare":
        return _BATCH_JSON_RAW_LIMIT
    if path == "/api/upload/image":
        return _UPLOAD_RAW_LIMITS["image"]
    if path != "/api/upload/file":
        return None

    query = parse_qs(
        scope.get("query_string", b"").decode("ascii", errors="replace"),
        keep_blank_values=True,
    )
    kind = (query.get("kind", ["image"])[-1] or "image").strip().lower()
    # Unknown kinds still reach the route's normal 400 validation, but remain
    # bounded by the largest supported upload request.
    return _UPLOAD_RAW_LIMITS.get(kind, _UPLOAD_RAW_LIMITS["video"])


def _header_values(scope: dict, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


def _valid_bearer(scope: dict) -> bool:
    values = _header_values(scope, b"authorization")
    if len(values) != 1:
        return False
    try:
        scheme, token = values[0].decode("latin-1").split(None, 1)
    except ValueError:
        return False
    if scheme.lower() != "bearer" or not token.strip():
        return False
    try:
        payload = decode_token(token.strip())
    except Exception:  # decode_token intentionally exposes no JWT diagnostics here
        return False
    return bool(payload.get("sub"))


def _content_length(scope: dict) -> int | None:
    values = _header_values(scope, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("ambiguous Content-Length")
    try:
        value = int(values[0].decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid Content-Length") from exc
    if value < 0:
        raise ValueError("invalid Content-Length")
    return value


async def _json_response(send, status: int, detail: str) -> None:
    body = json.dumps(
        {"detail": detail},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class ProtectedBodyLimitMiddleware:
    """Authenticate and bound selected request bodies before parser allocation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        limit = _raw_limit(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        if not _valid_bearer(scope):
            await _json_response(send, 401, _UNAUTHORIZED_DETAIL)
            return

        try:
            declared_length = _content_length(scope)
        except ValueError:
            await _json_response(send, 400, _INVALID_CONTENT_LENGTH_DETAIL)
            return
        if declared_length is not None and declared_length > limit:
            await _json_response(send, 413, _TOO_LARGE_DETAIL)
            return

        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_LIMIT)
        total = 0
        try:
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
                if message.get("type") != "http.request":
                    continue
                body = message.get("body", b"")
                total += len(body)
                if total > limit:
                    await _json_response(send, 413, _TOO_LARGE_DETAIL)
                    return
                if body:
                    await asyncio.to_thread(spool.write, body)
                if not message.get("more_body", False):
                    break

            await asyncio.to_thread(spool.seek, 0)
            sent = 0

            async def replay_receive() -> dict:
                nonlocal sent
                if total == 0:
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
                if sent >= total:
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
                chunk = await asyncio.to_thread(spool.read, _REPLAY_CHUNK_SIZE)
                sent += len(chunk)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": sent < total,
                }

            await self.app(scope, replay_receive, send)
        finally:
            spool.close()
