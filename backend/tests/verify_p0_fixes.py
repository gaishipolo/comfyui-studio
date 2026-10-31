# -*- coding: utf-8 -*-
"""Focused stable-baseline contracts without real Provider or cloud traffic.

Service checks require an isolated backend with admin/admin123. Set
COMFYUI_STUDIO_TEST_BASE_URL to its origin; the manual default remains port 8000.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app import filestore  # noqa: E402
from app.api import assets as assets_api  # noqa: E402
from app.api import health as health_api  # noqa: E402
from app.config import settings  # noqa: E402
from app.engines import factory  # noqa: E402
from app.engines.base import GenerationProvider  # noqa: E402
from app.filestore import ObjectHandle  # noqa: E402
from app.safe_detail import safe_detail  # noqa: E402
from app.thumbs import cleanup_asset_files, make_image_thumb, thumb_key  # noqa: E402
from test_env import SERVICE_BASE_URL  # noqa: E402


BASE = SERVICE_BASE_URL
ADMIN_PASSWORD = "admin123"
FORCE_FAILURE_ENV = "COMFYUI_STUDIO_P0_FORCE_FAILURE_AFTER_PLUGIN_UPLOAD"
PASS = 0
FAIL = 0


def report(name: str, ok: bool, detail: object = "") -> None:
    global PASS, FAIL
    PASS += int(bool(ok))
    FAIL += int(not ok)
    suffix = f"  {detail}" if detail else ""
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{suffix}")


def _decode(data: bytes, raw: bool):
    if raw:
        return data
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError:
        return data.decode("utf-8", "replace")


def req(method: str, path: str, token: str | None = None, json_body=None, raw=False):
    headers: dict[str, str] = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, _decode(response.read(), raw)
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read(), raw)


def multipart(
    path: str,
    token: str,
    filename: str,
    data: bytes,
    content_type: str,
    query: str = "",
):
    boundary = f"----p0-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        BASE + path + query,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, _decode(response.read(), False)
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read(), False)


def provider_source(name: str, label: str) -> bytes:
    source = f'''
from app.engines.base import CapabilitySpec, FieldSpec, GenerationProvider, GenerationResult

class P0ContractProvider(GenerationProvider):
    name = {name!r}
    label = {label!r}
    capabilities = {{"text_to_image": CapabilitySpec(
        kind="text_to_image",
        label="contract",
        fields=[FieldSpec(name="prompt", label="Prompt", required=True)],
    )}}
    config_schema = []

    async def submit(self, ctx, params):
        raise NotImplementedError()

    async def get_status(self, task_id):
        raise NotImplementedError()

    async def download_output(self, filename, subfolder="", output_type="output"):
        raise NotImplementedError()

    async def upload_input(self, data, filename):
        if filename.startswith("leak"):
            raise RuntimeError(
                "connect http://192.168.0.1:9999/upload?token=URLSECRET "
                "Authorization: Bearer HEADERSECRET password=PASSWORDSECRET"
            )
        return f"p0:{{filename}}:{{len(data)}}"

    async def health_check(self):
        return True
'''
    return source.encode()


def service_contracts(plugin_name: str) -> None:
    print("\n=== isolated service contracts ===")
    code, body = req(
        "POST",
        "/api/auth/login",
        json_body={"username": "admin", "password": ADMIN_PASSWORD},
    )
    report("fixed test admin can log in", code == 200, f"HTTP {code}")
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        return None
    token = body["access_token"]
    plugin_uploaded = False

    try:
        ids = [str(uuid.uuid4()) for _ in range(51)]
        code, body = req(
            "POST",
            "/api/assets/batch-download",
            token=token,
            json_body=ids,
            raw=True,
        )
        report("51 assets are rejected before archive work", code == 400, f"HTTP {code}")
        report("archive limit response identifies 50", b"50" in body, body[:100])

        oversized = b"x" * (20 * 1024 * 1024 + 1)
        code, body = multipart(
            "/api/upload/file",
            token,
            "oversized.png",
            oversized,
            "image/png",
            "?kind=image",
        )
        report("image upload rejects max plus one byte", code == 400, f"HTTP {code}")
        report("image limit response identifies 20MB", "20MB" in str(body), body)

        code, body = multipart(
            "/api/plugins/upload",
            token,
            f"{plugin_name}.py",
            provider_source(plugin_name, "P0 contract v1"),
            "text/x-python",
        )
        plugin_uploaded = code == 200
        report("unique temporary Provider uploads", plugin_uploaded, f"HTTP {code} {body}")
        if plugin_uploaded and os.environ.get(FORCE_FAILURE_ENV) == "1":
            raise RuntimeError("forced failure after temporary Provider upload")

        if plugin_uploaded:
            code, body = multipart(
                "/api/upload/file",
                token,
                "tiny.png",
                b"small-image-fixture",
                "image/png",
                f"?engine={urllib.parse.quote(plugin_name)}&kind=image",
            )
            report(
                "small upload reaches temporary Provider",
                code == 200 and str(body.get("filename", "")).startswith("p0:tiny.png:"),
                f"HTTP {code} {body}",
            )

            code, body = multipart(
                "/api/upload/file",
                token,
                "leak.png",
                b"small-image-fixture",
                "image/png",
                f"?engine={urllib.parse.quote(plugin_name)}&kind=image",
            )
            detail = str(body.get("detail", "")) if isinstance(body, dict) else str(body)
            report("Provider upload failures remain 502", code == 502, f"HTTP {code}")
            report(
                "Provider upload failure is sanitized",
                all(
                    secret not in detail
                    for secret in (
                        "192.168.0.1",
                        "URLSECRET",
                        "HEADERSECRET",
                        "PASSWORDSECRET",
                    )
                )
                and "<url>" in detail
                and "<redacted>" in detail,
                detail,
            )

            code, body = multipart(
                "/api/plugins/upload",
                token,
                f"{plugin_name}-upgrade.py",
                provider_source(plugin_name, "P0 contract v2"),
                "text/x-python",
            )
            report("replacement is keyed by declared Provider.name", code == 200, f"HTTP {code}")
            code, plugins = req("GET", "/api/plugins", token=token)
            current = next(
                (item for item in plugins if item.get("name") == plugin_name),
                {},
            ) if isinstance(plugins, list) else {}
            report("replacement updates the registered Provider", current.get("label") == "P0 contract v2")

        code, body = multipart(
            "/api/plugins/upload",
            token,
            "reserved.py",
            provider_source("comfyui", "reserved"),
            "text/x-python",
        )
        report(
            "reserved built-in Provider name is rejected",
            code == 400 and "内置" in str(body),
            f"HTTP {code} {body}",
        )
    finally:
        if plugin_uploaded:
            try:
                code, _ = req("DELETE", f"/api/plugins/{plugin_name}", token=token)
                report("temporary Provider is removed", code in {200, 404}, f"HTTP {code}")
            except Exception as exc:  # noqa: BLE001 - cleanup failure must affect aggregate exit
                report("temporary Provider cleanup", False, f"{type(exc).__name__}: {exc}")


def sanitizer_contract() -> None:
    print("\n=== sanitizer contract ===")
    detail = safe_detail(
        RuntimeError(
            "request http://10.0.0.5/private?api_key=URLSECRET "
            "Authorization: Bearer HEADERSECRET token=TOKEN_SECRET"
        )
    )
    report("safe_detail keeps the exception type", detail.startswith("RuntimeError:"), detail)
    report(
        "safe_detail removes URLs and credentials",
        all(secret not in detail for secret in ("10.0.0.5", "URLSECRET", "HEADERSECRET", "TOKEN_SECRET"))
        and "<url>" in detail
        and "<redacted>" in detail,
        detail,
    )
    report("safe_detail caps browser detail at 200 characters", len(detail) <= 200, len(detail))


def archive_contract() -> None:
    print("\n=== temporary archive contract ===")
    payloads = {"first": b"one", "second": b"two"}
    closed: list[str] = []
    assets = [
        SimpleNamespace(file_path="first", file_name="../same.png", task_id="task-one"),
        SimpleNamespace(file_path="second", file_name="..\\same.png", task_id="task-two"),
    ]

    def open_fixture(key: str):
        return ObjectHandle(
            size=len(payloads[key]),
            _stream=iter([payloads[key]]),
            _cleanup=lambda key=key: closed.append(key),
        )

    archive_path: Path | None = None
    try:
        with patch.object(assets_api, "open_asset", open_fixture):
            archive_path = assets_api._build_batch_archive(assets)
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            contents = {archive.read(name) for name in names}
        report(
            "archive uses safe unique basenames",
            len(names) == 2
            and len(set(names)) == 2
            and all("/" not in name and "\\" not in name and ".." not in name for name in names),
            names,
        )
        report("archive streams every fixture", contents == {b"one", b"two"}, contents)
        report("archive closes every ObjectHandle", closed == ["first", "second"], closed)
    finally:
        if archive_path is not None:
            assets_api._remove_temp_archive(archive_path)
            report("temporary archive is removed", not archive_path.exists(), archive_path)


class _LifecycleProvider(GenerationProvider):
    instances: list[_LifecycleProvider] = []

    def __init__(self, config=None):
        super().__init__(config)
        self.closed = asyncio.Event()
        type(self).instances.append(self)

    async def submit(self, ctx, params):
        raise NotImplementedError()

    async def get_status(self, task_id):
        raise NotImplementedError()

    async def download_output(self, filename, subfolder="", output_type="output"):
        raise NotImplementedError()

    async def health_check(self):
        return True

    async def close(self):
        self.closed.set()


async def _lifecycle_contract() -> None:
    print("\n=== Provider lifecycle contract ===")
    replace_name = f"p0_lifecycle_replace_{uuid.uuid4().hex[:8]}"
    shutdown_name = f"p0_lifecycle_shutdown_{uuid.uuid4().hex[:8]}"
    _LifecycleProvider.instances.clear()
    try:
        factory.register_provider(replace_name, _LifecycleProvider)
        old = factory.get_provider(replace_name)
        factory.set_engine_config(replace_name, {"version": 2})
        await asyncio.wait_for(old.closed.wait(), timeout=1)
        report("reconfiguration closes the retired Provider", old.closed.is_set())

        replacement = factory.get_provider(replace_name)
        factory.unregister_provider(replace_name)
        await asyncio.wait_for(replacement.closed.wait(), timeout=1)
        report("unregister closes the cached Provider", replacement.closed.is_set())

        factory.register_provider(shutdown_name, _LifecycleProvider)
        live = factory.get_provider(shutdown_name)
        await factory.close_all_providers()
        report("shutdown closes live Providers", live.closed.is_set())
        report("retirement tasks are drained", not factory._retire_tasks)
    finally:
        factory.unregister_provider(replace_name)
        factory.unregister_provider(shutdown_name)
        await factory.close_all_providers()


async def _health_cache_contract() -> None:
    print("\n=== fresh health cache contract ===")
    old_cache = dict(health_api._cache)
    old_inflight = dict(health_api._inflight)
    calls: list[str] = []

    async def fake_probe(name: str):
        calls.append(name)
        return {"name": name, "status": "connected", "detail": "admin-only"}

    health_api._cache.clear()
    health_api._inflight.clear()
    try:
        with (
            patch.object(health_api, "list_engines", return_value=["p0-health"]),
            patch.object(health_api, "_probe", fake_probe),
        ):
            member = SimpleNamespace(id="member", role="member")
            admin = SimpleNamespace(id="admin", role="admin")
            await health_api.engines(user=member, fresh=False)
            calls.clear()
            member_result = await health_api.engines(user=member, fresh=True)
            member_cached = not calls
            admin_result = await health_api.engines(user=admin, fresh=True)
            report("member fresh=true still uses cache", member_cached)
            report("admin fresh=true starts a new probe", calls == ["p0-health"], calls)
            report("member engine detail remains hidden", "detail" not in member_result[0])
            report("admin engine detail remains available", admin_result[0].get("detail") == "admin-only")
    finally:
        health_api._cache.clear()
        health_api._cache.update(old_cache)
        health_api._inflight.clear()
        health_api._inflight.update(old_inflight)


def thumbnail_contract() -> None:
    print("\n=== thumbnail and media cleanup contract ===")
    with io.BytesIO() as source:
        with Image.new("RGBA", (800, 200), (30, 60, 90, 128)) as image:
            image.save(source, format="PNG")
        thumbnail = make_image_thumb(source.getvalue())

    valid_thumbnail = False
    if thumbnail:
        with Image.open(io.BytesIO(thumbnail)) as image:
            valid_thumbnail = (
                image.format == "JPEG"
                and image.mode == "RGB"
                and max(image.size) <= 400
                and image.size == (400, 100)
            )
    report("RGBA input becomes a bounded RGB JPEG", valid_thumbnail)
    report("invalid image bytes return no thumbnail", make_image_thumb(b"not-an-image") is None)

    original_dir = settings.STORAGE_DIR
    original_backend = settings.STORAGE_BACKEND
    with tempfile.TemporaryDirectory(prefix="p0-media-") as raw_dir:
        settings.STORAGE_BACKEND = "local"
        settings.STORAGE_DIR = Path(raw_dir)
        filestore.reset_store()
        source_key = "task-p0/source.png"
        derived_key = thumb_key("task-p0", "source.png")
        try:
            filestore.save_asset_file(source_key, b"source", "image/png")
            filestore.save_asset_file(derived_key, thumbnail or b"thumbnail", "image/jpeg")
            source_result, thumbnail_result = cleanup_asset_files(
                source_key,
                "task-p0",
                "source.png",
            )
            report("media cleanup removes the source", source_result == (True, ""), source_result)
            report(
                "media cleanup removes the derived thumbnail",
                thumbnail_result == (True, ""),
                thumbnail_result,
            )
            report(
                "media cleanup leaves no source or thumbnail",
                not filestore.asset_exists(source_key)
                and not filestore.asset_exists(derived_key),
            )
        finally:
            filestore.reset_store()
            settings.STORAGE_DIR = original_dir
            settings.STORAGE_BACKEND = original_backend
            filestore.reset_store()


def run_contract(name: str, contract) -> None:
    try:
        result = contract()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except Exception as exc:  # noqa: BLE001 - keep running to produce an aggregate result
        report(name, False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    started = time.monotonic()
    plugin_name = f"p0_contract_{uuid.uuid4().hex[:10]}"
    try:
        service_contracts(plugin_name)
    except Exception as exc:  # noqa: BLE001 - offline contracts should still run
        report("isolated service contracts", False, f"{type(exc).__name__}: {exc}")

    run_contract("sanitizer contract", sanitizer_contract)
    run_contract("temporary archive contract", archive_contract)
    run_contract("Provider lifecycle contract", _lifecycle_contract)
    run_contract("fresh health cache contract", _health_cache_contract)
    run_contract("thumbnail and media cleanup contract", thumbnail_contract)

    print(f"\nRESULT: {PASS} passed / {FAIL} failed ({time.monotonic() - started:.1f}s)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
