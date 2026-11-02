"""M41 回归：冒烟脚本不得留机器绝对路径 / 工作流残留。"""
import re
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_smoke_script_has_no_machine_absolute_path() -> None:
    src = (TESTS / "smoke_fixes.py").read_text(encoding="utf-8")
    # 形如 E:\xx\... 或 E:/xx/... 的盘符绝对路径（脚本必须与机器无关）
    windows_absolute = re.findall(r"[A-Za-z]:[\\/][^\s\"']+", src)
    assert not windows_absolute, f"smoke_fixes.py 含机器绝对路径: {windows_absolute}"


def test_smoke_script_cleans_uploaded_workflow() -> None:
    src = (TESTS / "smoke_fixes.py").read_text(encoding="utf-8")
    assert "unlink" in src, "上传的工作流文件没有清理逻辑"


def main() -> int:
    tests = [
        test_smoke_script_has_no_machine_absolute_path,
        test_smoke_script_cleans_uploaded_workflow,
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
