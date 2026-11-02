"""M44 回归：README 的测试清单计数必须与磁盘实际一致。"""
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
README = REPO / "README.md"


def test_readme_test_counts_match_disk() -> None:
    text = README.read_text(encoding="utf-8")
    verify_count = len(list((BACKEND / "tests").glob("verify_*.py")))
    unit_count = len(list((BACKEND / "tests").glob("test_*.py")))
    assert f"`backend/tests/test_*.py`（{unit_count} 个" in text, (
        f"README 单元测试计数不是 {unit_count}"
    )
    assert f"### verify 脚本（{verify_count} 个 `verify_*.py`）" in text, (
        f"README verify 计数不是 {verify_count}"
    )


def test_readme_lists_no_missing_verify_scripts() -> None:
    text = README.read_text(encoding="utf-8")
    listed = set(re.findall(r"`(verify_[a-z0-9_]+\.py)`", text))
    on_disk = {path.name for path in (BACKEND / "tests").glob("verify_*.py")}
    missing = sorted(on_disk - listed)
    assert not missing, f"README 未列出这些 verify 脚本: {missing}"


def test_readme_lists_no_stale_verify_scripts() -> None:
    text = README.read_text(encoding="utf-8")
    listed = set(re.findall(r"`(verify_[a-z0-9_]+\.py)`", text))
    on_disk = {path.name for path in (BACKEND / "tests").glob("verify_*.py")}
    stale = sorted(listed - on_disk)
    assert not stale, f"README 列了不存在的 verify 脚本: {stale}"


def main() -> int:
    tests = [
        test_readme_test_counts_match_disk,
        test_readme_lists_no_missing_verify_scripts,
        test_readme_lists_no_stale_verify_scripts,
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
