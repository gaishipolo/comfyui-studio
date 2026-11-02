"""C1 回归：清理脚本的模板前缀不得命中产品演示模板。

事故场景：TEST_TEMPLATE_PREFIXES 曾包含宽前缀「测试模板-」，
会命中管理后台创建的演示模板「测试模板-M5」（UI 快照脚本依赖它存在），
--apply 时连带级联删除其全部任务与素材。
"""
import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "tests" / "cleanup_test_rows.py"


def _load():
    spec = importlib.util.spec_from_file_location("cleanup_test_rows", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ct = _load()


def test_demo_template_never_selected_even_if_prefix_hits():
    # 「测试模板-M5」是产品演示模板：即便它命中某个测试前缀，也绝不允许被选中
    rows = [("t1", "测试模板-M5"), ("t2", "Flux2文生图")]
    victims = ct.select_template_victims(rows)
    assert victims == [], f"产品模板被选为清理目标: {victims}"


def test_demo_template_in_protected_list():
    assert "测试模板-M5" in ct.PROTECTED_TEMPLATE_NAMES


def test_no_prefix_matches_demo_template():
    for prefix in ct.TEST_TEMPLATE_PREFIXES:
        assert not "测试模板-M5".startswith(prefix), (
            f"前缀 {prefix!r} 会命中产品演示模板「测试模板-M5」"
        )


def test_real_test_fixtures_still_matched():
    # 各验证脚本实际创建的模板名前缀必须仍能被清理（改了前缀别把残留漏掉）
    fixtures = [
        "验证用-B5-generic-abc12345",   # verify_b5_b6.py
        "选材验证-abc12345",            # verify_from_asset_upload.py
        "素材筛选-abc12345",            # verify_asset_filters.py
        "删除能力-abc12345",            # verify_delete_assets_tasks.py
        "用量验证-abc12345",            # verify_usage_stats.py
        "隔离验证-abc12345",            # verify_user_isolation.py
        "停用语义-abc12345",            # verify_delete_semantics.py
        "deltest-无引擎-abc123",        # verify_template_export.py
        "deltest-multi-abc123",         # verify_multi_image_api.py
    ]
    rows = [(f"id-{i}", name) for i, name in enumerate(fixtures)]
    victims = ct.select_template_victims(rows)
    got = {name for _, name in victims}
    missing = [n for n in fixtures if n not in got]
    assert not missing, f"以下测试残留不再被清理: {missing}"


def test_empty_or_none_names_skipped():
    rows = [("t1", None), ("t2", ""), ("t3", "真实模板")]
    assert ct.select_template_victims(rows) == []


def main() -> int:
    tests = [
        test_demo_template_never_selected_even_if_prefix_hits,
        test_demo_template_in_protected_list,
        test_no_prefix_matches_demo_template,
        test_real_test_fixtures_still_matched,
        test_empty_or_none_names_skipped,
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
