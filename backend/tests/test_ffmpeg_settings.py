"""Settings parity and ffmpeg override behavior."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app import thumbs  # noqa: E402


def test_ffmpeg_is_a_real_settings_field() -> None:
    assert "FFMPEG_PATH" in Settings.model_fields
    assert Settings.model_fields["FFMPEG_PATH"].default == ""


def test_dead_debug_setting_is_removed() -> None:
    assert "DEBUG" not in Settings.model_fields
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"(?m)^DEBUG=", text)


def test_env_example_has_exact_settings_parity() -> None:
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    declared = set(re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", text))
    assert declared == set(Settings.model_fields), (
        sorted(set(Settings.model_fields) - declared),
        sorted(declared - set(Settings.model_fields)),
    )


def test_ffmpeg_lookup_reads_settings_not_os_environ() -> None:
    original = thumbs._FFMPEG
    thumbs._FFMPEG = None
    try:
        with (
            patch.object(
                thumbs,
                "settings",
                SimpleNamespace(FFMPEG_PATH="configured-ffmpeg"),
                create=True,
            ),
            patch.dict(os.environ, {"FFMPEG_PATH": "environment-ffmpeg"}),
        ):
            assert thumbs.ffmpeg_exe() == "configured-ffmpeg"
    finally:
        thumbs._FFMPEG = original


if __name__ == "__main__":
    failures = []
    for test in (
        test_ffmpeg_is_a_real_settings_field,
        test_dead_debug_setting_is_removed,
        test_env_example_has_exact_settings_parity,
        test_ffmpeg_lookup_reads_settings_not_os_environ,
    ):
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {4 - len(failures)} passed / {len(failures)} failed")
    raise SystemExit(1 if failures else 0)
