"""用户隔离验证 —— 任务/素材的归属边界 + 手动共享（2026-10-29）

产品拍板：默认隔离 + 手动共享。
- 任务：各看各的，admin 全见（带创建人显示名）
- 素材：默认归生成者（归属走 task.created_by）；共享给团队后全员可见可下载，
  **但不可改**（打标签/删除/共享开关仍是作者或管理员的权利）
- 读接口越权报 404（不泄露「存在但无权」），写接口越权报 403

测试数据全部自建（deltest- 前缀 + uuid），跑完自清理，不碰库里真实数据。
需要后端在 127.0.0.1:8000 运行。
"""
import asyncio
import inspect
import json
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api"
DB_PATH = Path(__file__).resolve().parents[1] / "workbench.db"
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


def req_raw(path, method="GET", data=None, token=None):
    """二进制响应（ZIP / 文件下载）用这个 —— json.loads 会把字节流炸掉"""
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ── 账号：admin + 两个互不相干的普通用户 ─────────────────────
_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
ADMIN_T = tok["access_token"]
ADMIN_ID = tok["user"]["id"]


def make_member(tag: str):
    name = f"deltest-{tag}-{uuid.uuid4().hex[:8]}"
    st, _ = req("/auth/register", "POST",
                {"username": name, "password": "pw123456", "display_name": f"隔离测试-{tag}",
                 "role": "member"}, token=ADMIN_T)
    assert st == 200, f"建用户失败 {st}"
    _, mtok = req("/auth/login", "POST", {"username": name, "password": "pw123456"})
    return name, mtok["access_token"], mtok["user"]["id"]


A_NAME, A_T, A_ID = make_member("A")
B_NAME, B_T, B_ID = make_member("B")

N = f"隔离验证-{uuid.uuid4().hex[:8]}"
st, tpl = req("/templates", "POST", {"name": N, "is_active": True}, token=ADMIN_T)
report("创建模板", st == 200, f"status={st}")
TPL_ID = tpl["id"]


# ── 直插测试任务 + 素材（不跑真生成，确定性强）────────────────
SEEDED_TASKS: list[str] = []


async def seed(owner_id: str, n_assets: int, status: str = "done"):
    """造一个任务 + n 个素材（真写文件到磁盘），返回 (task_id, [(asset_id, file_name)])"""
    from app.database import async_session
    from app.models.models import Task, Asset
    from app.config import settings

    task_id = uuid.uuid4().hex
    async with async_session() as db:
        db.add(Task(
            id=task_id, template_id=TPL_ID, created_by=owner_id,
            batch_id=uuid.uuid4().hex, status=status, progress=100.0,
            params={}, result={}, error_msg="",
        ))
        made = []
        for i in range(n_assets):
            aid = uuid.uuid4().hex
            rel = f"deltest-{uuid.uuid4().hex[:8]}/img_{i}.png"
            abs_path = settings.STORAGE_DIR / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(b"\x89PNG-test-bytes")
            db.add(Asset(
                id=aid, task_id=task_id, file_path=rel,
                file_name=f"img_{i}.png", file_size=14,
                mime_type="image/png", tags=["deltest-iso"], meta={},
            ))
            made.append((aid, f"img_{i}.png"))
        await db.commit()
    SEEDED_TASKS.append(task_id)
    return task_id, made


# ══════════════════════════════════════════════════════════════
print("\n=== 1. 任务：各看各的，admin 全见 ===")

t_a, _ = asyncio.run(seed(A_ID, 1))   # A 的任务
t_b, _ = asyncio.run(seed(B_ID, 1))   # B 的任务
t_o, _ = asyncio.run(seed(ADMIN_ID, 1))  # admin 的任务

st, lst = req("/tasks", token=A_T)
ids_a = {t["id"] for t in lst}
report("A 只能看见自己的任务", st == 200 and t_a in ids_a and t_b not in ids_a and t_o not in ids_a,
       f"返回 {len(lst)} 条")
report("A 的列表里每条都是自己的", all(t["created_by"] == A_ID for t in lst))

st, lst = req("/tasks", token=B_T)
ids_b = {t["id"] for t in lst}
report("B 只能看见自己的任务", st == 200 and t_b in ids_b and t_a not in ids_b and t_o not in ids_b,
       f"返回 {len(lst)} 条")

st, lst = req("/tasks", token=ADMIN_T)
ids_admin = {t["id"] for t in lst}
report("admin 看得见全部三个人的任务",
       st == 200 and {t_a, t_b, t_o} <= ids_admin, f"返回 {len(lst)} 条")
report("admin 列表带创建人显示名",
       any(t["id"] == t_a and t["creator_name"] == "隔离测试-A" for t in lst))

st, _ = req(f"/tasks/{t_a}", token=A_T)
report("A 看自己的任务详情 200", st == 200)
st, _ = req(f"/tasks/{t_a}", token=B_T)
report("B 看 A 的任务详情 404（不泄露存在性）", st == 404, f"status={st}")
st, _ = req(f"/tasks/{t_a}", token=ADMIN_T)
report("admin 看任意任务详情 200", st == 200)

st, _ = req(f"/tasks/{t_a}/assets", token=B_T)
report("B 拉 A 任务的素材 404", st == 404, f"status={st}")

t_noowner, _ = asyncio.run(seed("", 0))  # 无主任务（历史数据语义）
st, lst = req("/tasks", token=A_T)
report("无主任务不进普通用户的列表", st == 200 and t_noowner not in {t["id"] for t in lst})
st, lst = req("/tasks", token=ADMIN_T)
report("无主任务 admin 看得见", st == 200 and t_noowner in {t["id"] for t in lst})

print("\n=== 2. 任务写操作：越权 403 ===")
st, d = req(f"/tasks/{t_a}/retry", "POST", token=B_T)
report("B 重试 A 的任务 403", st == 403, f"{st} {d.get('detail', '')}")
st, d = req(f"/tasks/{t_a}/cancel", "POST", token=B_T)
report("B 取消 A 的任务 403", st == 403, f"{st} {d.get('detail', '')}")
st, d = req(f"/tasks/{t_a}", "DELETE", token=B_T)
report("B 删 A 的任务 403", st == 403, f"{st} {d.get('detail', '')}")
st, _ = req(f"/tasks/{t_a}", "DELETE", token=ADMIN_T)
report("admin 删任意任务 200（顺手清掉 t_a）", st == 200)
SEEDED_TASKS.remove(t_a)

# ══════════════════════════════════════════════════════════════
print("\n=== 3. 素材：默认隔离（归生成者）===")

t_a2, (a_asset,) = asyncio.run(seed(A_ID, 1))
a_id, a_name = a_asset

st, lst = req("/assets", token=A_T)
ids = {x["id"] for x in lst}
report("A 看得见自己的素材", st == 200 and a_id in ids)

st, lst = req("/assets", token=B_T)
ids = {x["id"] for x in lst}
report("B 看不见 A 的素材", st == 200 and a_id not in ids, f"返回 {len(lst)} 条")

st, lst = req("/assets", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("admin 看得见 A 的素材", st == 200 and a_id in ids)
report("素材响应带归属与共享字段",
       any(x["id"] == a_id and x["created_by"] == A_ID and x["creator_name"] == "隔离测试-A"
           and x["is_shared"] is False for x in lst))

st, _ = req(f"/assets/{a_id}", token=B_T)
report("B 拉 A 的素材详情 404", st == 404, f"status={st}")
st, _ = req(f"/assets/{a_id}", token=A_T)
report("A 拉自己的素材详情 200", st == 200)

st, _ = req_raw(f"/tasks/{t_a2}/assets/{a_name}", token=B_T)
report("B 下载 A 的素材 404", st == 404, f"status={st}")
st, _ = req_raw(f"/tasks/{t_a2}/assets/{a_name}", token=A_T)
report("A 下载自己的素材 200", st == 200, f"status={st}")

st, d = req(f"/assets/{a_id}/tags?tag=x", "POST", token=B_T)
report("B 给 A 的素材打标签 403", st == 403, f"{st} {d.get('detail', '')}")
st, d = req(f"/assets/{a_id}", "DELETE", token=B_T)
report("B 删 A 的素材 403", st == 403, f"{st} {d.get('detail', '')}")

# ══════════════════════════════════════════════════════════════
print("\n=== 4. 手动共享：可见可下载，但不可改 ===")

st, d = req(f"/assets/{a_id}/share", "POST", {"shared": True}, token=B_T)
report("B 改 A 的素材共享状态 403", st == 403, f"status={st}")

st, resp = req(f"/assets/{a_id}/share", "POST", {"shared": True}, token=A_T)
report("A 共享自己的素材 200 且 is_shared=true",
       st == 200 and resp.get("is_shared") is True, f"status={st}")

st, lst = req("/assets", token=B_T)
ids = {x["id"] for x in lst}
report("共享后 B 看得见了", st == 200 and a_id in ids)
report("共享素材带来源人（B 视角）",
       any(x["id"] == a_id and x["creator_name"] == "隔离测试-A" and x["is_shared"] is True
           for x in lst))

st, _ = req(f"/assets/{a_id}", token=B_T)
report("共享后 B 拉详情 200", st == 200)
st, _ = req_raw(f"/tasks/{t_a2}/assets/{a_name}", token=B_T)
report("共享后 B 能下载", st == 200, f"status={st}")

st, d = req(f"/assets/{a_id}/tags?tag=x", "POST", token=B_T)
report("共享后 B 打标签仍 403（共享≠共同所有）", st == 403, f"{st} {d.get('detail', '')}")
st, d = req(f"/assets/{a_id}", "DELETE", token=B_T)
report("共享后 B 删除仍 403", st == 403, f"{st} {d.get('detail', '')}")

st, _ = req(f"/assets/{a_id}/share", "POST", {"shared": False}, token=ADMIN_T)
report("admin 可代管共享开关（收回）", st == 200)

st, lst = req("/assets", token=B_T)
ids = {x["id"] for x in lst}
report("收回后 B 又看不见了", st == 200 and a_id not in ids)

# ══════════════════════════════════════════════════════════════
print("\n=== 5. 标签云 / 批量下载 / 素材作输入：跟着可见性走 ===")

# 用独有标签验可见性 —— 别用 seed() 的公共标签，B 自己的素材也带它，
# 拿它断言「B 看不见」必假红（第一版就栽在这）。
SECRET_TAG = f"iso-secret-{uuid.uuid4().hex[:6]}"
st, _ = req(f"/assets/{a_id}/tags?tag={SECRET_TAG}", "POST", token=A_T)
report("A 给自己的素材打独有标签", st == 200, f"status={st}")

st, tags = req("/assets/tags", token=B_T)
report("B 的标签云不含 A 未共享素材的独有标签",
       st == 200 and not any(x["name"] == SECRET_TAG for x in tags))

req(f"/assets/{a_id}/share", "POST", {"shared": True}, token=A_T)  # 再共享
st, tags = req("/assets/tags", token=B_T)
report("共享后 B 的标签云能看到该标签", st == 200 and any(x["name"] == SECRET_TAG for x in tags))

st, raw = req_raw("/assets/batch-download", "POST", [a_id], token=B_T)
report("共享素材 B 批量下载 200（ZIP）", st == 200 and raw[:2] == b"PK", f"status={st}")

req(f"/assets/{a_id}/share", "POST", {"shared": False}, token=A_T)  # 收回
st, _ = req_raw("/assets/batch-download", "POST", [a_id], token=B_T)
report("收回后 B 批量下载拿不到", st == 404, f"status={st}")

st, _ = req_raw("/assets/batch-download", "POST", [a_id], token=A_T)
report("A 批量下载自己的 200", st == 200, f"status={st}")

# 从素材库选作生成输入：越权的素材进不了引擎（404 在引擎交互之前）
st, d = req("/upload/from-asset", "POST", {"asset_id": a_id}, token=B_T)
report("B 拿 A 的未共享素材当生成输入 404", st == 404 and d.get("detail") == "素材不存在",
       f"{st} {d.get('detail', '')}")

# ══════════════════════════════════════════════════════════════
print("\n=== 6. 回归守卫（防将来绕过隔离）===")

import app.api.tasks as tmod
import app.api.assets as amod
import app.api.uploads as umod
import app.database as dbmod

report("tasks/ assets/ uploads/ 都必须引用 access 规则",
       "app.access" in inspect.getsource(tmod)
       and "app.access" in inspect.getsource(amod)
       and "app.access" in inspect.getsource(umod))
report("retry 必须有归属校验",
       "can_modify_task" in inspect.getsource(tmod.retry_task))
report("素材共享列进了列迁移（老库才升得上来）",
       any(m[1] == "is_shared" for m in dbmod._COLUMN_MIGRATIONS))
report("AssetResponse 必须带共享/归属字段",
       {"is_shared", "created_by", "creator_name"}
       <= set(__import__("app.schemas.schemas", fromlist=["AssetResponse"]).AssetResponse.model_fields))
report("共享开关必须校验 can_modify_asset（共享≠转让）",
       "can_modify_asset" in inspect.getsource(amod.share_asset))

# ══════════════════════════════════════════════════════════════
print("\n=== 清理 ===")

for tid in list(SEEDED_TASKS):
    st, _ = req(f"/tasks/{tid}", "DELETE", token=ADMIN_T)
    report(f"清理任务 {tid[:8]}", st == 200, f"status={st}")

st, _ = req(f"/templates/{TPL_ID}", "DELETE", token=ADMIN_T)
report("清理测试模板", st == 200, f"status={st}")

# 测试用户没有删除接口，直接清库（先确认名下已无任务，再删）
db = sqlite3.connect(DB_PATH)
cur = db.cursor()
for name in (A_NAME, B_NAME):
    uid = cur.execute("select id from users where username=?", (name,)).fetchone()[0]
    left = cur.execute("select count(*) from tasks where created_by=?", (uid,)).fetchone()[0]
    report(f"{name} 名下无残留任务", left == 0)
    cur.execute("delete from users where id=?", (uid,))
db.commit()
db.close()
report("清理测试用户", True, f"{A_NAME} / {B_NAME}")

print()
print("全部通过 ✓" if ok_all else "存在失败 ✗")
sys.exit(0 if ok_all else 1)
