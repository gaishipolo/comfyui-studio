"""I15 回归：verify_tag_* 脚本必须用临时库，不碰真实 workbench.db。

事故场景：两个脚本直接 INSERT 真实库，中途 Ctrl+C/崩溃留残留；
负向断言（期望某标签命中 0 条）还会被真实数据误报 FAIL。
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run_script(name: str):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [PYTHON, str(BACKEND / "tests" / name)],
        capture_output=True,
        cwd=BACKEND,
        timeout=180,
        env=env,
    )
    out = (
        proc.stdout.decode("utf-8", errors="replace")
        + proc.stderr.decode("utf-8", errors="replace")
    )
    return SimpleNamespace(returncode=proc.returncode, out=out)


def real_db_test_assets() -> int:
    db = BACKEND / "workbench.db"
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM assets WHERE task_id IN ('persist-1', 'je-1')"
        ).fetchone()[0]
    finally:
        con.close()


def test_tag_persist_uses_temp_db_and_leaves_real_db_untouched():
    before = real_db_test_assets()
    r = run_script("verify_tag_persist.py")
    out = r.out
    assert r.returncode == 0, out
    assert "临时数据库" in out, "脚本未声明自己使用临时数据库:\n" + out
    assert real_db_test_assets() == before, "真实库出现了测试残留行"


def test_tag_json_each_uses_temp_db_and_leaves_real_db_untouched():
    before = real_db_test_assets()
    r = run_script("verify_tag_json_each.py")
    out = r.out
    assert r.returncode == 0, out
    assert "临时数据库" in out, "脚本未声明自己使用临时数据库:\n" + out
    assert real_db_test_assets() == before, "真实库出现了测试残留行"


def main() -> int:
    tests = [
        test_tag_persist_uses_temp_db_and_leaves_real_db_untouched,
        test_tag_json_each_uses_temp_db_and_leaves_real_db_untouched,
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
