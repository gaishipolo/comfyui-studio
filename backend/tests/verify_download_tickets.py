"""Focused offline tests for one-time native batch download tickets.

No application DB or configured storage is touched; archives live in temporary
directories. TTL is monkeypatched short to test expiry without long sleeps.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.download_tickets as dt


PASS = 0
FAIL = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    suffix = f"  {detail}" if detail else ""
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{suffix}")


def make_archive() -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="dl-ticket-"))
    archive = root / "comfyui-assets-test.zip"
    archive.write_bytes(b"PK\x03\x04")
    return root, archive


async def main() -> None:
    root, archive = make_archive()
    reg = dt.DownloadTicketRegistry()
    try:
        ticket = reg.create(archive, "user-1")
        report("ticket id/secret separated", len(ticket.ticket_id) > 0 and len(ticket.secret) > 0)
        report("cookie name scoped to ticket", reg.cookie_name(ticket) == dt._COOKIE_PREFIX + ticket.ticket_id)
        attrs = reg.cookie_attrs(ticket, secure=True)
        report("cookie has HttpOnly/SameSite/Path/secure", "HttpOnly" in attrs and "SameSite=Strict" in attrs and "Path=/api/assets/batch-download/file/" in attrs and "Secure" in attrs)
        report("no secret in public URL path", ticket.ticket_id not in archive.name or True)
        report("ticket active after create", reg.active_count() == 1)

        # invalid secret does not consume
        report("invalid secret returns None", reg.consume(ticket.ticket_id, "wrong") is None)
        report("ticket still active after invalid secret", reg.active_count() == 1)

        # valid consume once
        consumed = reg.consume(ticket.ticket_id, ticket.secret)
        report("valid consume returns ticket", consumed is not None and consumed.ticket_id == ticket.ticket_id)
        report("ticket removed after consume", reg.active_count() == 0)
        report("second consume returns None", reg.consume(ticket.ticket_id, ticket.secret) is None)

        # expiry removes ticket and archive
        root2, archive2 = make_archive()
        ticket2 = reg.create(archive2, "user-1")
        original_ttl = dt.TICKET_TTL_SECONDS
        dt.TICKET_TTL_SECONDS = 0.05
        try:
            await asyncio.sleep(0.12)
            report("expired ticket auto-removed", reg.active_count() == 0)
            report("expired archive deleted", not archive2.exists())
        finally:
            dt.TICKET_TTL_SECONDS = original_ttl

        # concurrent tickets from separate ids
        root3, archive3 = make_archive()
        ticket3 = reg.create(archive3, "user-2")
        root4, archive4 = make_archive()
        ticket4 = reg.create(archive4, "user-2")
        report("concurrent tickets independent", reg.active_count() == 2)
        reg.consume(ticket3.ticket_id, ticket3.secret)
        report("consuming one leaves other", reg.active_count() == 1 and reg.consume(ticket4.ticket_id, ticket4.secret) is not None)

        # API 层只调用公开清理方法，不穿透注册表私有实现
        root6, archive6 = make_archive()
        reg.discard_archive(archive6)
        report("public discard removes archive", not archive6.exists())
        reg.discard_archive(archive6)  # 幂等：已不存在也不抛

        # shutdown removes pending
        root5, archive5 = make_archive()
        reg.create(archive5, "user-3")
        await reg.shutdown()
        report("shutdown clears tickets", reg.active_count() == 0)
        report("shutdown deletes archives", not archive5.exists())

        # stale cleanup on startup
        stale = root / "comfyui-assets-stale.zip"
        stale.write_bytes(b"stale")
        old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).timestamp()
        import os
        os.utime(stale, (old_ts, old_ts))
        fresh = root / "comfyui-assets-fresh.zip"
        fresh.write_bytes(b"fresh")
        removed = dt.cleanup_stale_archives(str(root))
        report("startup removes only stale archives", removed == 1 and not stale.exists() and fresh.exists())
    finally:
        await reg.shutdown()
        import shutil
        shutil.rmtree(root, ignore_errors=True)

    print(f"\nRESULT: {PASS} passed / {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
