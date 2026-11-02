"""Focused checks that plugin restore does not trust arbitrary DB metadata.

Review finding M9: the upload path validates the registered name with
`_SAFE_NAME`, but `restore_plugins` (startup recovery) consumed `row.filename`
and `row.name` straight from the DB. A tampered row could point `filename` at a
path outside the plugins directory (or carry an unsafe registered name).
Restore must apply the same guard even when the file physically exists.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import plugins as plugins_api  # noqa: E402


class FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.committed = False

    async def execute(self, _query):
        return FakeRows(self.rows)

    def add(self, _row):
        pass

    async def commit(self):
        self.committed = True


def make_row(name: str, filename: str):
    return SimpleNamespace(
        name=name,
        filename=filename,
        label="x",
        enabled=True,
        last_error="",
        config={},
    )


def run_restore(rows, plugin_dir: Path):
    unregistered: list[str] = []
    configured: list[str] = []

    def fake_load_all():
        return []

    orig_dir = plugins_api.PLUGIN_DIR
    orig_load = plugins_api.load_all_plugins
    orig_unregister = plugins_api.unregister_provider
    orig_config = plugins_api.set_engine_config
    plugins_api.PLUGIN_DIR = plugin_dir
    plugins_api.load_all_plugins = fake_load_all
    plugins_api.unregister_provider = unregistered.append
    plugins_api.set_engine_config = lambda name, cfg: configured.append(name)
    try:
        db = FakeDB(rows)
        asyncio.run(plugins_api.restore_plugins(db))
    finally:
        plugins_api.PLUGIN_DIR = orig_dir
        plugins_api.load_all_plugins = orig_load
        plugins_api.unregister_provider = orig_unregister
        plugins_api.set_engine_config = orig_config
    return unregistered, configured


def test_restore_rejects_unsafe_filename_even_when_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        # traversal target physically exists -> must still be rejected by the name guard
        outside = plugin_dir.parent / "outside_evil.py"
        outside.write_text("# evil", encoding="utf-8")
        row = make_row("evil", "../outside_evil.py")
        try:
            unregistered, configured = run_restore([row], plugin_dir)
            assert "不合法" in (row.last_error or ""), row.last_error
            assert "evil" in unregistered, (unregistered, row.last_error)
            assert configured == [], configured
        finally:
            outside.unlink(missing_ok=True)


def test_restore_rejects_unsafe_registered_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "evil.py").write_text("# evil", encoding="utf-8")
        row = make_row("../evil", "evil.py")
        unregistered, configured = run_restore([row], plugin_dir)
        assert "不合法" in (row.last_error or ""), row.last_error
        assert "../evil" in unregistered, unregistered
        assert configured == [], configured


def test_restore_rejects_non_python_filename() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "agnes.txt").write_text("# not python", encoding="utf-8")
        row = make_row("agnes", "agnes.txt")
        unregistered, configured = run_restore([row], plugin_dir)
        assert "不合法" in (row.last_error or ""), row.last_error
        assert "agnes" in unregistered, unregistered
        assert configured == [], configured


def test_restore_keeps_plain_valid_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "agnes.py").write_text("# ok", encoding="utf-8")
        row = make_row("agnes", "agnes.py")
        unregistered, configured = run_restore([row], plugin_dir)
        assert row.last_error == "", row.last_error
        assert unregistered == [], unregistered


def main() -> int:
    tests = [
        test_restore_rejects_unsafe_filename_even_when_present,
        test_restore_rejects_unsafe_registered_name,
        test_restore_rejects_non_python_filename,
        test_restore_keeps_plain_valid_row,
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
