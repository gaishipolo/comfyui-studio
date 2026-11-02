"""M42 回归：依赖必须带上限，避免重建环境时被上游破坏性升级打穿。"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_requirements_pin_upper_bounds() -> None:
    lines = [
        line.strip()
        for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    missing = []
    for line in lines:
        if "==" in line:
            continue
        if ">=" in line and "<" not in line:
            missing.append(line)
    assert not missing, f"以下依赖没有上限: {missing}"


def test_requirements_keep_bcrypt_compat_pin() -> None:
    text = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    assert "bcrypt>=3.2,<4" in text, text


def main() -> int:
    tests = [
        test_requirements_pin_upper_bounds,
        test_requirements_keep_bcrypt_compat_pin,
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
