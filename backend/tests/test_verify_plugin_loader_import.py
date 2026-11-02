"""M40 回归：import verify_plugin_loader 必须无 live 副作用。"""
import importlib.util
import os
from pathlib import Path


def test_import_is_side_effect_free() -> None:
    os.environ.pop("COMFYUI_STUDIO_LIVE_TESTS_OK", None)
    path = Path(__file__).with_name("verify_plugin_loader.py")
    spec = importlib.util.spec_from_file_location("verify_plugin_loader_import_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


if __name__ == "__main__":
    try:
        test_import_is_side_effect_free()
    except BaseException as exc:
        print(f"[FAIL] test_import_is_side_effect_free: {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 1 failed")
        raise SystemExit(1)
    print("[PASS] test_import_is_side_effect_free")
    print("\nRESULT: 1 passed / 0 failed")
