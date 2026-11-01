"""一次性批量下载票据 —— 让浏览器原生下载大 ZIP，不把整包读进页面内存。

准备接口带 JWT 生成 ZIP 并登记票据；下载接口只凭按票据隔离的 HttpOnly Cookie
消费一次。票据独立成模块是为了隔离安全与清理逻辑，便于直接单元测试。
"""
from __future__ import annotations

import asyncio
import hmac
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TICKET_TTL_SECONDS = 5 * 60
_STALE_MARGIN_SECONDS = 2 * 60
_COOKIE_PREFIX = "comfyui_batch_dl_"


@dataclass
class DownloadTicket:
    ticket_id: str
    secret: str
    archive_path: Path
    user_id: str
    created_at: datetime
    expires_at: datetime
    consumed: bool = False
    expiry_task: asyncio.Task[Any] | None = field(default=None, repr=False)


class DownloadTicketRegistry:
    """进程内票据登记表。单进程架构下有效；keyed by ticket_id。"""

    def __init__(self) -> None:
        self._tickets: dict[str, DownloadTicket] = {}

    def create(
        self,
        archive_path: Path,
        user_id: str,
    ) -> DownloadTicket:
        ticket_id = secrets.token_hex(8)
        secret = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        ticket = DownloadTicket(
            ticket_id=ticket_id,
            secret=secret,
            archive_path=archive_path,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(seconds=TICKET_TTL_SECONDS),
        )
        self._tickets[ticket_id] = ticket
        loop = asyncio.get_running_loop()
        ticket.expiry_task = loop.create_task(self._expire_later(ticket_id))
        return ticket

    async def _expire_later(self, ticket_id: str) -> None:
        try:
            await asyncio.sleep(TICKET_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        self.remove(ticket_id)

    def cookie_name(self, ticket: DownloadTicket) -> str:
        return _COOKIE_PREFIX + ticket.ticket_id

    def cookie_attrs(
        self,
        ticket: DownloadTicket,
        *,
        secure: bool,
    ) -> str:
        parts = [
            f"{self.cookie_name(ticket)}={ticket.secret}",
            "Path=/api/assets/batch-download/file/" + ticket.ticket_id,
            "Max-Age=" + str(TICKET_TTL_SECONDS),
            "HttpOnly",
            "SameSite=Strict",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def validate_secret(self, ticket: DownloadTicket, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(ticket.secret, candidate)

    def consume(self, ticket_id: str, secret: str | None) -> DownloadTicket | None:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return None
        if ticket.consumed or datetime.now(timezone.utc) > ticket.expires_at:
            self.remove(ticket_id)
            return None
        if not self.validate_secret(ticket, secret):
            return None
        ticket.consumed = True
        self._tickets.pop(ticket_id, None)
        if ticket.expiry_task is not None and not ticket.expiry_task.done():
            ticket.expiry_task.cancel()
        return ticket

    def remove(self, ticket_id: str) -> None:
        ticket = self._tickets.pop(ticket_id, None)
        if ticket is None:
            return
        if ticket.expiry_task is not None and not ticket.expiry_task.done():
            ticket.expiry_task.cancel()
        self._unlink(ticket.archive_path)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass

    async def shutdown(self) -> None:
        tickets = list(self._tickets.values())
        self._tickets.clear()
        for ticket in tickets:
            if ticket.expiry_task is not None and not ticket.expiry_task.done():
                ticket.expiry_task.cancel()
            self._unlink(ticket.archive_path)

    def active_count(self) -> int:
        return len(self._tickets)


# 全局单例，应用生命周期内共享。
registry = DownloadTicketRegistry()


def stale_archive_epoch() -> datetime:
    return datetime.now(timezone.utc) - timedelta(
        seconds=TICKET_TTL_SECONDS + _STALE_MARGIN_SECONDS
    )


def cleanup_stale_archives(temp_dir: str | None = None) -> int:
    """启动时删除超过票据生命周期+安全余量的 comfyui-assets-*.zip。"""
    directory = Path(temp_dir or tempfile.gettempdir())
    removed = 0
    cutoff = stale_archive_epoch()
    if not directory.is_dir():
        return 0
    for candidate in directory.glob("comfyui-assets-*.zip"):
        try:
            mtime = datetime.fromtimestamp(
                candidate.stat().st_mtime,
                tz=timezone.utc,
            )
        except OSError:
            continue
        if mtime < cutoff:
            try:
                candidate.unlink()
                removed += 1
            except OSError:
                pass
    return removed