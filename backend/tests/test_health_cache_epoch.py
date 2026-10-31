"""Deterministic cache-generation and Provider failure logging checks."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app.api import health as health_api  # noqa: E402
from app.api import uploads as uploads_api  # noqa: E402
from provider_test_utils import lease_acquirer  # noqa: E402


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def rendered(record: logging.LogRecord | None) -> str:
    return logging.Formatter("%(levelname)s %(message)s").format(record) if record else ""


async def test_invalidation_epoch_blocks_stale_inflight_cache_write() -> None:
    health_api._cache.clear()
    health_api._inflight.clear()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def gated_probe(engine_name: str) -> dict:
        nonlocal calls
        calls += 1
        call = calls
        if call == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return {"name": engine_name, "status": "connected", "generation": call}

    with patch.object(health_api, "_probe", gated_probe):
        stale = asyncio.create_task(health_api._probe_cached("epoch-engine"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        health_api.invalidate_cache("epoch-engine")
        fresh = asyncio.create_task(health_api._probe_cached("epoch-engine"))
        try:
            await asyncio.wait_for(second_started.wait(), timeout=0.2)
        finally:
            release_first.set()
        fresh_result, stale_result = await asyncio.gather(fresh, stale)

    assert fresh_result["generation"] == 2, fresh_result
    assert stale_result["generation"] == 1, stale_result
    cached = health_api._cache.get("epoch-engine")
    assert cached is not None and cached[1]["generation"] == 2, cached
    health_api.invalidate_cache("epoch-engine")


async def test_health_failure_logs_raw_traceback_but_returns_safe_detail() -> None:
    raw = "health failed http://10.0.0.5/private?api_token=HEALTHSECRET"

    def fail_acquire(_engine_name: str):
        raise RuntimeError(raw)

    records = Records()
    logger = logging.getLogger(health_api.__name__)
    logger.addHandler(records)
    old_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with patch.object(health_api, "acquire_provider", fail_acquire):
            result = await health_api._probe("health-context")
    finally:
        logger.removeHandler(records)
        logger.setLevel(old_level)

    assert result["status"] == "error"
    assert "10.0.0.5" not in result["detail"]
    assert "HEALTHSECRET" not in result["detail"]
    record = next((item for item in records.records if "health-context" in item.getMessage()), None)
    assert record is not None and raw not in record.getMessage()
    assert record.exc_info is not None and raw in rendered(record)


async def test_upload_failure_logs_raw_traceback_but_returns_safe_detail() -> None:
    raw = "upload failed http://10.0.0.6/input?auth_token=UPLOADSECRET"

    class FailingProvider:
        async def upload_input(self, _data: bytes, _name: str) -> str:
            raise RuntimeError(raw)

    records = Records()
    logger = logging.getLogger(uploads_api.__name__)
    logger.addHandler(records)
    old_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with patch.object(
            uploads_api,
            "acquire_provider",
            lease_acquirer(lambda _engine: FailingProvider()),
        ):
            try:
                await uploads_api._forward_to_engine("upload-context", b"data", "input.png")
            except HTTPException as exc:
                error = exc
            else:
                raise AssertionError("upload failure escaped HTTP boundary")
    finally:
        logger.removeHandler(records)
        logger.setLevel(old_level)

    assert error.status_code == 502
    assert "10.0.0.6" not in str(error.detail)
    assert "UPLOADSECRET" not in str(error.detail)
    record = next((item for item in records.records if "upload-context" in item.getMessage()), None)
    assert record is not None and raw not in record.getMessage()
    assert record.exc_info is not None and raw in rendered(record)


async def main() -> int:
    tests = [
        test_invalidation_epoch_blocks_stale_inflight_cache_write,
        test_health_failure_logs_raw_traceback_but_returns_safe_detail,
        test_upload_failure_logs_raw_traceback_but_returns_safe_detail,
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
