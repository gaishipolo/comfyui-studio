"""I5 回归：破坏性 live 脚本必须带「目标可丢弃」自检。

会打真实实例 / 改真实库的脚本，只有显式 COMFYUI_STUDIO_LIVE_TESTS_OK=1
才允许跑——防止对「恰好还是 admin/admin123」的在用实例误执行。
"""
import os
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import live_guard  # noqa: E402
from live_guard import ENV_FLAG, require_live_ok  # noqa: E402


def test_guard_exits_when_flag_missing():
    old = os.environ.pop(ENV_FLAG, None)
    try:
        try:
            require_live_ok()
        except SystemExit as exc:
            assert ENV_FLAG in str(exc)
        else:
            raise AssertionError("flag 缺失时应 SystemExit")
    finally:
        if old is not None:
            os.environ[ENV_FLAG] = old


def test_guard_rejects_non_one_values():
    for value in ("0", "", "yes", "true"):
        os.environ[ENV_FLAG] = value
        try:
            try:
                require_live_ok()
            except SystemExit:
                pass
            else:
                raise AssertionError(f"flag={value!r} 应 SystemExit")
        finally:
            del os.environ[ENV_FLAG]


def test_guard_passes_when_flag_is_one():
    os.environ[ENV_FLAG] = "1"
    try:
        require_live_ok()  # 不抛 = 通过
    finally:
        del os.environ[ENV_FLAG]


# 会打真实实例 / 改真实库 / 杀进程的脚本（只读检查与纯 in-process 脚本不在列）。
# 漏列一个 = 又出现一条没有防线的生产事故通道。
DESTRUCTIVE_LIVE_SCRIPTS = [
    "align_template_fields.py",
    "cleanup_orphan_workflows.py",
    "cleanup_test_rows.py",
    "e2e_restart_recovery.py",
    "e2e_upload_generate.py",
    "fix_bool_select_options.py",
    "purge_test_residue.py",
    "smoke_fixes.py",
    "verify_asset_filters.py",
    "verify_b5_b6.py",
    "verify_cancel.py",
    "verify_delete_assets_tasks.py",
    "verify_delete_semantics.py",
    "verify_filestore_live.py",
    "verify_from_asset_upload.py",
    "verify_frontend_contract.py",
    "verify_multi_image_api.py",
    "verify_p0_fixes.py",
    "verify_plugin_loader.py",
    "verify_schema_normalization.py",
    "verify_template_export.py",
    "verify_thumbnail_perf.py",
    "verify_upload_kinds.py",
    "verify_usage_stats.py",
    "verify_user_isolation.py",
]


def test_every_destructive_script_calls_the_guard():
    missing = []
    for name in DESTRUCTIVE_LIVE_SCRIPTS:
        path = TESTS / name
        if not path.exists():
            missing.append(f"{name}(文件不存在)")
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if "require_live_ok(" not in src:
            missing.append(name)
    assert not missing, f"以下破坏性脚本缺少 require_live_ok 自检: {missing}"


def main() -> int:
    tests = [
        test_guard_exits_when_flag_missing,
        test_guard_rejects_non_one_values,
        test_guard_passes_when_flag_is_one,
        test_every_destructive_script_calls_the_guard,
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
