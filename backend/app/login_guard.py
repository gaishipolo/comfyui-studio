"""登录失败限速 —— 暴力破解的第一道防线。

内存计数（单进程部署；重启清零可接受——重启后攻击面同样重置）。
键 = (小写用户名, 客户端 IP)：连错 max_failures 次锁 lock_seconds 秒，
锁定期内一律 429（含正确密码，不给探测机会）；成功后计数清零。

锁定期内的失败不延长锁（防止攻击者用持续错误把锁无限续期）。
"""
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, lock_seconds: int = 900,
                 now: Callable[[], float] | None = None):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._now = now or time.monotonic
        self._failures: dict[tuple[str, str], int] = defaultdict(int)
        self._locked_until: dict[tuple[str, str], float] = {}

    def is_locked(self, key: tuple[str, str]) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if self._now() >= until:
            # 锁已过期：连同失败计数一起清掉，下一轮从零开始
            self._locked_until.pop(key, None)
            self._failures.pop(key, None)
            return False
        return True

    def record_failure(self, key: tuple[str, str]) -> None:
        if self.is_locked(key):
            return  # 锁定期内的失败不累计、不延长
        self._failures[key] += 1
        if self._failures[key] >= self.max_failures:
            self._locked_until[key] = self._now() + self.lock_seconds
            self._failures[key] = 0

    def record_success(self, key: tuple[str, str]) -> None:
        self._failures.pop(key, None)


def login_key(username: str, request: Request) -> tuple[str, str]:
    client = request.client
    ip = client.host if client is not None else "unknown"
    return (username.strip().lower(), ip)


# 全局实例（测试可整体替换：auth_api 一律通过 login_guard.limiter 访问）
limiter = LoginRateLimiter()
