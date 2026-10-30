"""从素材库选材作生成输入（/upload/from-asset）—— 成功路径验证（2026-10-29）

背景：之前只测了越权被拒（404），成功路径没人走过。
结果 `target.name` 的 NameError 藏在权限校验通过之后，测试完全看不见，
直到用户真在界面上「从素材库选共享素材当参考图」才炸出来。

这里补上成功路径：
- 自己的素材 → 应该正常推进引擎（200，不是 NameError 502）
- 别人共享的素材 → 同样能用
- 别人未共享的素材 → 404（回归守卫，别把隔离修没了）

需要后端在 127.0.0.1:8000 运行，且 ComfyUI 引擎可达（真发一次上传）。
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api"
ok_all = True


def req(path, method="GET", data=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ── 账号 ─────────────────────────────────────────────────
_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
ADMIN_T = tok["access_token"]
ADMIN_ID = tok["user"]["id"]

MEMBER_NAME = f"deltest-fa-{uuid.uuid4().hex[:8]}"
st, _ = req("/auth/register", "POST",
            {"username": MEMBER_NAME, "password": "pw123456", "role": "member"}, token=ADMIN_T)
report("创建临时成员", st == 200, f"status={st}")
_, m = req("/auth/login", "POST", {"username": MEMBER_NAME, "password": "pw123456"})
MEMBER_T = m["access_token"]
MEMBER_ID = m["user"]["id"]

# ── 造模板 + 素材 ─────────────────────────────────────────
N = f"选材验证-{uuid.uuid4().hex[:8]}"
st, tpl = req("/templates", "POST", {"name": N, "is_active": True}, token=ADMIN_T)
report("创建模板", st == 200, f"status={st}")
TPL_ID = tpl["id"]

SEEDED_TASKS: list[str] = []


async def seed(owner_id: str, shared: bool = False):
    """造一个任务 + 1 个真图片素材（真写磁盘），可选共享给团队"""
    from app.database import async_session
    from app.models.models import Task, Asset
    from app.config import settings

    task_id = uuid.uuid4().hex
    aid = uuid.uuid4().hex
    rel = f"deltest-{uuid.uuid4().hex[:8]}/ref.png"
    abs_path = settings.STORAGE_DIR / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    # 最小合法 PNG（1x1 透明），让引擎侧真能收下，而不是假字节被拒
    abs_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    async with async_session() as db:
        db.add(Task(
            id=task_id, template_id=TPL_ID, created_by=owner_id,
            batch_id=uuid.uuid4().hex, status="done", progress=100.0,
            params={}, result={}, error_msg="",
        ))
        db.add(Asset(
            id=aid, task_id=task_id, file_path=rel,
            file_name="ref.png", file_size=abs_path.stat().st_size,
            mime_type="image/png", tags=[], meta={}, is_shared=shared,
        ))
        await db.commit()
    SEEDED_TASKS.append(task_id)
    return aid


print("\n=== 从素材库选材作生成输入（成功路径）===")
a_admin_private = asyncio.run(seed(ADMIN_ID, shared=False))
a_member_shared = asyncio.run(seed(MEMBER_ID, shared=True))
a_member_private = asyncio.run(seed(MEMBER_ID, shared=False))


def check_upload(label, asset_id, token, expect_ok: bool):
    """调 /upload/from-asset，断言不出现 NameError，且可见性语义符合预期"""
    st, d = req("/upload/from-asset", "POST", {"asset_id": asset_id}, token=token)
    if expect_ok:
        # 成功 = 200 + 拿到引擎侧文件名；绝不能是 NameError
        ok = st == 200 and bool(d.get("filename")) and "not defined" not in json.dumps(d)
        report(label, ok, f"status={st} filename={d.get('filename', '')!r} detail={d.get('detail', '')!r}")
    else:
        ok = st == 404 and d.get("detail") == "素材不存在"
        report(label, ok, f"{st} {d.get('detail', '')}")


check_upload("admin 用自己素材 → 推进引擎", a_admin_private, ADMIN_T, True)
check_upload("member 用自己素材 → 推进引擎", a_member_private, MEMBER_T, True)
check_upload("admin 用别人共享素材 → 推进引擎", a_member_shared, ADMIN_T, True)
check_upload("member 用别人共享素材 → 推进引擎", a_member_shared, MEMBER_T, True)
check_upload("member 用别人未共享素材 → 404", a_admin_private, MEMBER_T, False)

print("\n=== 回归守卫 ===")
src = (Path(__file__).resolve().parents[1] / "app/api/uploads.py").read_text(encoding="utf-8")
report("upload_from_asset 不得引用未定义的 target", "target.name" not in src,
       "源码里应只有 asset.file_name")
# 两个字段各司其职：读字节用磁盘路径，推给引擎用文件名。
# 之前把 file_path 当文件名传过去，引擎侧收到一长串相对路径直接报错。
report("upload_from_asset 传给引擎的是 asset.file_name（不是磁盘路径）",
       "_forward_to_engine(body.engine, data, asset.file_name)" in src)
report("upload_from_asset 读字节用 asset.file_path",
       "read_asset_bytes(asset.file_path)" in src)

# ── 清理 ─────────────────────────────────────────────────
print("\n=== 清理 ===")
for tid in SEEDED_TASKS:
    st, d = req(f"/tasks/{tid}", "DELETE", token=ADMIN_T)
    report(f"清理任务 {tid[:8]}", st == 200, f"status={st}")
st, _ = req(f"/templates/{TPL_ID}", "DELETE", token=ADMIN_T)
report("清理模板", st == 200, f"status={st}")

import sqlite3
DB_PATH = Path(__file__).resolve().parents[1] / "workbench.db"
conn = sqlite3.connect(DB_PATH)
conn.execute("DELETE FROM users WHERE username LIKE 'deltest-fa-%'")
conn.commit()
conn.close()
report("清理临时成员", True, MEMBER_NAME)

print(f"\n{'全部通过 ✓' if ok_all else '存在失败 ✗'}")
sys.exit(0 if ok_all else 1)
