"""I16 回归：e2e_restart_recovery 的端口匹配必须精确到本地地址字段。

事故场景：旧版 `f":{port}" in line` 整行子串匹配，`:8000` 会命中
`0.0.0.0:80001` 这类监听行，taskkill 杀掉无关进程。
"""
import os
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

os.environ["COMFYUI_STUDIO_LIVE_TESTS_OK"] = "1"  # import 前有 guard，测试环境放行

import e2e_restart_recovery as e2e  # noqa: E402


def _fake_netstat(lines: list[str]):
    e2e.run_cmd = lambda cmd: "\n".join(lines)


def test_similar_port_must_not_match():
    # 只监听 80001 时，找 8000 必须返回 None（旧版子串匹配会错杀 PID 222）
    _fake_netstat([
        "  TCP    0.0.0.0:80001          0.0.0.0:0              LISTENING       222",
        "  TCP    127.0.0.1:80000        127.0.0.1:0            LISTENING       333",
    ])
    e2e.SERVICE_PORT = 8000
    assert e2e.listen_pid() is None


def test_exact_local_port_matches():
    _fake_netstat([
        "  TCP    0.0.0.0:80001          0.0.0.0:0              LISTENING       222",
        "  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       111",
        "  TCP    127.0.0.1:8000         127.0.0.1:51755        ESTABLISHED     444",
    ])
    e2e.SERVICE_PORT = 8000
    assert e2e.listen_pid() == 111


def test_foreign_address_port_ignored():
    # 远端地址恰好是 :8000 的连接行不算（且非 LISTENING）
    _fake_netstat([
        "  TCP    192.168.1.10:54321     192.168.1.1:8000       TIME_WAIT       0",
    ])
    e2e.SERVICE_PORT = 8000
    assert e2e.listen_pid() is None


def test_ipv6_loopback_listening():
    _fake_netstat([
        "  TCP    [::]:8000              [::]:0                 LISTENING       555",
    ])
    e2e.SERVICE_PORT = 8000
    assert e2e.listen_pid() == 555


def main() -> int:
    tests = [
        test_similar_port_must_not_match,
        test_exact_local_port_matches,
        test_foreign_address_port_ignored,
        test_ipv6_loopback_listening,
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
