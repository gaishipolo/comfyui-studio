"""T3 回归：插件上传校验纯离线可测（不起服务、不连引擎）。

覆盖：语法错 / 依赖越界 / 只 import 不算子类 / 撞内置名 / 合法插件加载成功。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import plugin_loader  # noqa: E402
from app.engines.factory import unregister_provider  # noqa: E402

VALID_PLUGIN = '''
from app.engines.base import (
    CapabilitySpec, FieldSpec, GenerationProvider, GenerationResult, TaskStatus,
)


class OfflineProbeProvider(GenerationProvider):
    name = "offline_probe"
    label = "离线探针"

    capabilities = {
        "text_to_image": CapabilitySpec(
            kind="text_to_image",
            label="文生图",
            fields=[FieldSpec(name="prompt", label="提示词", type="string", required=True)],
        ),
    }
    config_schema = [
        FieldSpec(name="base_url", label="API 地址", type="string", required=True),
    ]

    async def submit(self, ctx, params):
        return "offline-task"

    async def get_status(self, task_id):
        return GenerationResult(status=TaskStatus.DONE, progress=100.0)

    async def download_output(self, filename, subfolder="", output_type="output"):
        return b""

    async def health_check(self):
        return True
'''


def _write(tmp: Path, name: str, source: str) -> Path:
    path = tmp / name
    path.write_text(source, encoding="utf-8")
    return path


def _expect_error(path: Path, *needles: str) -> str:
    try:
        plugin_loader.load_plugin_file(path)
    except plugin_loader.PluginError as exc:
        detail = str(exc)
        for needle in needles:
            assert needle in detail, f"{detail!r} 未包含 {needle!r}"
        return detail
    raise AssertionError(f"{path.name} 应当被拒绝，却加载成功了")


def test_syntax_error_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-syntax-") as raw:
        path = _write(Path(raw), "broken.py", "def broken(:\n  pass\n")
        detail = _expect_error(path, "语法错误")
        assert "第" in detail, detail


def test_dependency_outside_allowlist_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-dep-") as raw:
        path = _write(
            Path(raw),
            "bad_dep.py",
            "import numpy\nfrom app.engines.base import GenerationProvider\n",
        )
        _expect_error(path, "白名单")


def test_importing_provider_class_is_not_enough() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-subclass-") as raw:
        path = _write(
            Path(raw),
            "no_subclass.py",
            "from app.engines.comfyui import ComfyUIProvider\n",
        )
        _expect_error(path, "没找到")


def test_reserved_engine_name_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-reserved-") as raw:
        source = VALID_PLUGIN.replace('"offline_probe"', '"comfyui"').replace(
            "OfflineProbeProvider", "ReservedProvider"
        )
        path = _write(Path(raw), "reserved.py", source)
        _expect_error(path, "冲突")


def test_multiple_provider_classes_are_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-multi-") as raw:
        source = VALID_PLUGIN + VALID_PLUGIN.replace(
            "OfflineProbeProvider", "SecondProvider"
        ).replace('"offline_probe"', '"offline_probe_two"')
        path = _write(Path(raw), "multi.py", source)
        _expect_error(path, "只允许一个")


def test_invalid_provider_name_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-name-") as raw:
        source = VALID_PLUGIN.replace('"offline_probe"', '"Bad Name"')
        path = _write(Path(raw), "badname.py", source)
        _expect_error(path, "注册名")


def test_valid_plugin_loads_and_describes() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-ok-") as raw:
        path = _write(Path(raw), "probe.py", VALID_PLUGIN)
        try:
            info = plugin_loader.load_plugin_file(path)
            assert info["name"] == "offline_probe", info
            assert info["label"] == "离线探针", info
            assert "text_to_image" in info["capabilities"], info
            assert [f["name"] for f in info["config_schema"]] == ["base_url"], info
        finally:
            unregister_provider("offline_probe")


def test_module_does_not_leak_into_sys_modules() -> None:
    with tempfile.TemporaryDirectory(prefix="plugin-leak-") as raw:
        path = _write(Path(raw), "probe.py", VALID_PLUGIN)
        before = set(sys.modules)
        try:
            plugin_loader.load_plugin_file(path)
        finally:
            unregister_provider("offline_probe")
        leaked = {name for name in set(sys.modules) - before if name.startswith("engine_plugin_")}
        assert not leaked, leaked


def main() -> int:
    tests = [
        test_syntax_error_is_rejected,
        test_dependency_outside_allowlist_is_rejected,
        test_importing_provider_class_is_not_enough,
        test_reserved_engine_name_is_rejected,
        test_multiple_provider_classes_are_rejected,
        test_invalid_provider_name_is_rejected,
        test_valid_plugin_loads_and_describes,
        test_module_does_not_leak_into_sys_modules,
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
