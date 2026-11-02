"""M12/M13/M22 回归：插件扫描、注册名和依赖提示一致。"""
import ast
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import plugin_loader  # noqa: E402
from app.engines.base import CapabilitySpec, GenerationProvider  # noqa: E402


class InvalidNameProvider(GenerationProvider):
    name = "Bad Provider Name"
    capabilities = {"text": CapabilitySpec(kind="text", label="Text")}

    async def submit(self, ctx, params): return "id"
    async def get_status(self, task_id): return None
    async def download_output(self, filename, subfolder="", output_type="output"): return b""
    async def health_check(self): return True


def test_crashed_upload_temp_is_removed_not_loaded() -> None:
    old_dir = plugin_loader.PLUGIN_DIR
    with tempfile.TemporaryDirectory(prefix="plugin-scan-") as raw_dir:
        plugin_loader.PLUGIN_DIR = Path(raw_dir)
        temp = plugin_loader.PLUGIN_DIR / "_upload_deadbeef.py"
        temp.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
        try:
            assert plugin_loader.load_all_plugins() == []
            assert not temp.exists()
        finally:
            plugin_loader.PLUGIN_DIR = old_dir


def test_invalid_provider_name_rejected_by_loader() -> None:
    try:
        plugin_loader._validate(InvalidNameProvider)
    except plugin_loader.PluginError as exc:
        assert "注册名" in str(exc)
    else:
        raise AssertionError("invalid provider name passed loader validation")


def test_dependency_error_describes_actual_app_allowlist() -> None:
    try:
        plugin_loader._check_dependencies(ast.parse("import numpy"))
    except plugin_loader.PluginError as exc:
        detail = str(exc)
        assert "app 顶层包" in detail, detail
        assert "app.engines.base" not in detail, detail
    else:
        raise AssertionError("disallowed dependency was accepted")


if __name__ == "__main__":
    tests = [
        test_crashed_upload_temp_is_removed_not_loaded,
        test_invalid_provider_name_rejected_by_loader,
        test_dependency_error_describes_actual_app_allowlist,
    ]
    failures = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    raise SystemExit(1 if failures else 0)
