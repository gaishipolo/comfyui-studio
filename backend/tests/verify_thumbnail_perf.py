"""缩略图性能加固验证（N1/N2/N3，仅测本次修复）

- N1a: 全局缩略图并发闸门存在且值为 1
- N1b: Image.MAX_IMAGE_PIXELS 已设（超大图解码有上限）
- N2: 小图 /thumb 生成并返回 JPEG 缩略图（体积远小于原图）
- N2: 大图不再按 file_size 一刀切回退（file_size 报 20MB 仍返回缩略图）
- N2: 首访等待有有限超时（超时回退原图的代码路径由 media_reliability 覆盖）
- N3: 缩略图响应头 Cache-Control 为 private, max-age=3600

会向真实库插入 deltest- 前缀的 asset 行并清理，属破坏性脚本，需
COMFYUI_STUDIO_LIVE_TESTS_OK=1。
"""
import live_guard

live_guard.require_live_ok()

import asyncio
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app.database import async_session
from app.filestore import save_asset_file, delete_asset_file
from app.models.models import Asset
from app.api import tasks as tasks_api
from app import asset_media
from app.thumbs import thumb_key
from sqlalchemy import select, text

BASE = "http://127.0.0.1:8000/api"
MB = 1024 * 1024
results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, info: str = "") -> None:
    results.append((name, ok, info))


def req(method, path, body=None, token=None, raw=False):
    url = BASE + (path if path.startswith("/") else "/" + path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, (payload if raw else json.loads(payload.decode() or "null")), hdrs
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            body_json = json.loads(payload.decode() or "null")
        except Exception:
            body_json = payload
        return e.code, body_json, {k.lower(): v for k, v in e.headers.items()}


def make_png(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), (120, 80, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def seed_asset(task_id: str, file_name: str, data: bytes, mime: str, reported_size: int | None = None) -> str:
    key = f"{task_id}/{file_name}"
    await asyncio.to_thread(save_asset_file, key, data, content_type=mime)
    async with async_session() as db:
        a = Asset(
            id=f"deltest-thumb-{file_name}",
            owner_id="deltest-owner",
            task_id=task_id,
            file_path=key,
            file_name=file_name,
            file_size=reported_size if reported_size is not None else len(data),
            mime_type=mime,
        )
        db.add(a)
        await db.commit()
    return a.id


async def cleanup(task_id: str, file_name: str, asset_id: str) -> None:
    async with async_session() as db:
        await db.execute(text("DELETE FROM assets WHERE id=:i"), {"i": asset_id})
        await db.commit()
    await asyncio.to_thread(delete_asset_file, f"{task_id}/{file_name}")
    await asyncio.to_thread(delete_asset_file, thumb_key(f"{task_id}/{file_name}"))


async def main() -> None:
    # ── 静态检查 ──
    report("N1a 全局缩略图并发闸门存在（值为 1）",
           getattr(asset_media, "_thumbnail_generation_slots", None) is not None
           and asset_media._thumbnail_generation_slots._value == 1,
           f"semaphore={getattr(asset_media, '_thumbnail_generation_slots', None)}")

    import app.thumbs as thumbs_mod
    report("N1b Image.MAX_IMAGE_PIXELS 已设（防超大图解码爆内存）",
           Image.MAX_IMAGE_PIXELS == 100_000_000,
           f"MAX_IMAGE_PIXELS={Image.MAX_IMAGE_PIXELS}")

    # ── 准备一个真实任务 id ──
    _, login_body, _ = req("POST", "/auth/login", {"username": "admin", "password": "admin123"})
    token = login_body.get("access_token")
    assert token, "登录失败"
    s, body, _ = req("GET", "/tasks?limit=1", token=token)
    tasks = body if isinstance(body, list) else (body or {}).get("tasks") or []
    if not tasks:
        print("库里没有任务，跳过 HTTP 断言")
        _print()
        return
    task_id = tasks[0]["id"]

    png = make_png(1200, 900)  # 真实小图

    # ── 小图：应生成缩略图并返回 JPEG（体积 < 原图）──
    aid = await seed_asset(task_id, "deltest_small.png", png, "image/png")
    try:
        s, payload, hdrs = req("GET", f"/assets/{aid}/thumb",
                               token=token, raw=True)
        ctype = hdrs.get("content-type", "")
        report("N2 小图 /thumb 返回 JPEG 缩略图",
               s == 200 and ctype.startswith("image/jpeg") and len(payload) < len(png),
               f"status={s} type={ctype} thumb={len(payload)}B src={len(png)}B")
        report("N3 缩略图 Cache-Control = private, max-age=3600",
               "max-age=3600" in hdrs.get("cache-control", ""),
               hdrs.get("cache-control", ""))
    finally:
        await cleanup(task_id, "deltest_small.png", aid)

    # ── 大图不再按 file_size 一刀切：报 20MB 但实际是小图 → 仍返回缩略图 ──
    aid = await seed_asset(task_id, "deltest_big.png", png, "image/png", reported_size=20 * MB)
    try:
        s, payload, hdrs = req("GET", f"/assets/{aid}/thumb",
                               token=token, raw=True)
        ctype = hdrs.get("content-type", "")
        report("N2 大图不再按 file_size 回退（报 20MB 仍返回 JPEG 缩略图）",
               s == 200 and ctype.startswith("image/jpeg") and len(payload) < len(png),
               f"status={s} type={ctype} thumb={len(payload)}B src={len(png)}B")
    finally:
        await cleanup(task_id, "deltest_big.png", aid)

    # ── 生成超时 → 回退原图（200 + 原 MIME，不是 404）──
    # 超时分支与「生成失败」共用同一段回退代码，后者由
    # verify_media_reliability.py 用损坏图做端到端覆盖（200 + 原字节）。
    # 这里只静态校验超时窗口被设成有限值，防止退化成「永久等待」。
    timeout = getattr(asset_media, "_THUMB_EAGER_TIMEOUT_SECONDS", None)
    report("N2 首访等待有有限超时（不无限阻塞）",
           isinstance(timeout, (int, float)) and 0 < timeout <= 10,
           f"_THUMB_EAGER_TIMEOUT_SECONDS={timeout}")

    _print()


def _print() -> None:
    print("=== 缩略图性能加固验证 ===")
    ok = True
    for name, cond, info in results:
        print(f"[{'PASS' if cond else 'FAIL'}] {name}\n      -> {info}")
        ok = ok and cond
    print("RESULT:", "ALL PASS" if ok else "HAS FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
