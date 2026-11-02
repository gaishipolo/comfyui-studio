"""Focused checks for query-input validation.

Covers review findings:
- M4: illegal date query params must map to 400, not 500.
- M3: task list limit/offset must bound the query like the asset list does.
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import timeutils  # noqa: E402
from app.api import tasks as tasks_api  # noqa: E402


def test_illegal_date_maps_to_400() -> None:
    for bad in ("abc", "2026-13-99", "not-a-date"):
        try:
            timeutils.parse_day_or_400(bad)
        except HTTPException as exc:
            assert exc.status_code == 400, (bad, exc.status_code)
        else:
            raise AssertionError(f"illegal date {bad!r} did not raise 400")


def test_valid_date_still_parses() -> None:
    # APP_TIMEZONE 默认 Asia/Shanghai；数据库存 naive UTC。
    assert timeutils.parse_day_or_400("2026-10-29") == datetime(2026, 10, 28, 16, 0, 0)
    end = timeutils.parse_day_or_400("2026-10-29", end=True)
    assert end == datetime(2026, 10, 29, 15, 59, 59)
    assert timeutils.parse_day_or_400("2026-10-29T14:00+08:00") == datetime(2026, 10, 29, 6, 0, 0)


def test_basic_iso_date_without_dashes_is_rejected() -> None:
    try:
        timeutils.parse_day_or_400("20261029")
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
        assert "YYYY-MM-DD" in str(exc.detail)
    else:
        raise AssertionError("basic ISO date 20261029 should be rejected")


def test_invalid_server_timezone_is_not_reported_as_bad_user_date() -> None:
    old = timeutils.settings.APP_TIMEZONE
    timeutils.settings.APP_TIMEZONE = "Invalid/Timezone"
    try:
        try:
            timeutils.parse_day_or_400("2026-10-29")
        except HTTPException as exc:
            assert exc.status_code == 500, exc.status_code
            assert "时区" in str(exc.detail)
            assert "日期格式不合法" not in str(exc.detail)
        else:
            raise AssertionError("invalid server timezone should fail")
    finally:
        timeutils.settings.APP_TIMEZONE = old


def test_task_list_limit_and_offset_are_bounded() -> None:
    params = inspect.signature(tasks_api.list_tasks).parameters
    limit_default = params["limit"].default
    offset_default = params["offset"].default

    def bounds(field) -> tuple:
        ge = le = None
        for meta in getattr(field, "metadata", []):
            if type(meta).__name__ == "Ge":
                ge = meta.ge
            if type(meta).__name__ == "Le":
                le = meta.le
        return ge, le

    limit_ge, limit_le = bounds(limit_default)
    assert limit_ge == 1, limit_ge
    assert limit_le is not None, limit_le
    offset_ge, _ = bounds(offset_default)
    assert offset_ge == 0, offset_ge


def main() -> int:
    tests = [
        test_illegal_date_maps_to_400,
        test_valid_date_still_parses,
        test_basic_iso_date_without_dashes_is_rejected,
        test_invalid_server_timezone_is_not_reported_as_bad_user_date,
        test_task_list_limit_and_offset_are_bounded,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
