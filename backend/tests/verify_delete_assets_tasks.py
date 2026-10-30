"""B31 验证 —— 任务/素材删除能力（级联 + 磁盘清理 + 权限）

覆盖：
1. 删素材：记录消失 + 磁盘文件同步消失
2. 删任务：级联删掉它的全部素材（记录 + 文件），不留孤儿
3. 权限：普通用户删不了别人的；自己的能删；管理员可删任意
4. 404：删不存在的
5. 运行中的任务拒绝删除（防竞态）
6. 守卫：file_path 含 ../ 的记录不能借删除越界删文件

测试数据全部自建（uuid 前缀），跑完自清理，**不碰库里任何真实素材**。
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


_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
ADMIN_T = tok["access_token"]
ADMIN_ID = tok["user"]["id"]

# 建一个普通用户，用来测权限（uuid 用户名，跑完删掉）
MEMBER_NAME = f"deltest-{uuid.uuid4().hex[:8]}"
st, _ = req("/auth/register", "POST",
            {"username": MEMBER_NAME, "password": "pw123456", "role": "member"}, token=ADMIN_T)
report("创建普通用户", st == 200, f"status={st}")
_, mtok = req("/auth/login", "POST", {"username": MEMBER_NAME, "password": "pw123456"})
MEMBER_T = mtok["access_token"]
MEMBER_ID = mtok["user"]["id"]

# 模板（名字唯一）
N = f"删除能力-{uuid.uuid4().hex[:8]}"
st, tpl = req("/templates", "POST", {"name": N, "is_active": True}, token=ADMIN_T)
report("创建模板", st == 200, f"status={st}")
TPL_ID = tpl["id"]


# ── 直插测试任务 + 素材（不跑真生成，确定性强）────────────────
# seed() 造过的任务一律登记，cleanup 遍历登记表 —— 写死 (t1,t2,t5,t6)
# 这种元组迟早漏掉新增的测试组，漏一个就在共享素材库留一条脏数据。
SEEDED_TASKS: list[str] = []


async def seed(owner_id: str, n_assets: int, status: str = "done"):
    """造一个任务 + n 个素材（真写文件到磁盘），返回 (task_id, [(asset_id, abs_path)])"""
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
                mime_type="image/png", tags=["deltest"], meta={},
            ))
            made.append((aid, abs_path))
        await db.commit()
    SEEDED_TASKS.append(task_id)
    return task_id, made


async def asset_count(task_id: str) -> int:
    from app.database import async_session
    from app.models.models import Asset
    from sqlalchemy import func, select

    async with async_session() as db:
        r = await db.execute(
            select(func.count()).select_from(Asset).where(Asset.task_id == task_id))
        return r.scalar_one()


# ══════════════════════════════════════════════════════════════
print("\n=== 1. 删素材：记录 + 磁盘文件都要消失 ===")
t1, a1 = asyncio.run(seed(ADMIN_ID, 2))
aid, apath = a1[0]

st, body = req(f"/assets/{aid}", "DELETE", token=ADMIN_T)
report("删除素材返回 200", st == 200, f"status={st} {body}")
report("响应标出文件已删", body.get("file_removed") is True, f"file_removed={body.get('file_removed')}")
report("磁盘文件已消失", not apath.exists(), f"{apath.name}")

st, _ = req(f"/assets/{aid}", token=ADMIN_T)
report("素材记录已消失(404)", st == 404, f"status={st}")
report("同任务另一素材仍在", asyncio.run(asset_count(t1)) == 1)

print("\n=== 2. 删任务：级联删素材，不留孤儿 ===")
t2, a2 = asyncio.run(seed(ADMIN_ID, 3))
paths2 = [p for _, p in a2]

st, body = req(f"/tasks/{t2}", "DELETE", token=ADMIN_T)
report("删除任务返回 200", st == 200, f"status={st} {body}")
report("响应计数=3 个素材", body.get("assets_deleted") == 3, f"assets_deleted={body.get('assets_deleted')}")
report("3 个文件全部删除", body.get("files_removed") == 3, f"files_removed={body.get('files_removed')}")
report("磁盘上 3 个文件都不在", not any(p.exists() for p in paths2))
report("素材记录清零（无孤儿）", asyncio.run(asset_count(t2)) == 0)

st, _ = req(f"/tasks/{t2}", token=ADMIN_T)
report("任务记录已消失(404)", st == 404, f"status={st}")

print("\n=== 3. 权限：普通用户删不了别人的 ===")
t3, a3 = asyncio.run(seed(ADMIN_ID, 1))          # admin 的任务
t4, a4 = asyncio.run(seed(MEMBER_ID, 1))          # member 自己的任务

st, body = req(f"/assets/{a3[0][0]}", "DELETE", token=MEMBER_T)
report("删他人素材被拒 403", st == 403, f"status={st} {body.get('detail')}")
report("他人素材记录仍在", asyncio.run(asset_count(t3)) == 1)
report("他人文件未被删", a3[0][1].exists())

st, body = req(f"/tasks/{t3}", "DELETE", token=MEMBER_T)
report("删他人任务被拒 403", st == 403, f"status={st} {body.get('detail')}")
report("他人任务记录仍在", asyncio.run(asset_count(t3)) == 1)

print("\n=== 4. 权限：自己的能删，管理员可删任意 ===")
st, _ = req(f"/tasks/{t4}", "DELETE", token=MEMBER_T)
report("本人删自己的任务 200", st == 200, f"status={st}")
report("本人删除也清了文件", not a4[0][1].exists())

st, _ = req(f"/tasks/{t3}", "DELETE", token=ADMIN_T)
report("管理员删他人任务 200", st == 200, f"status={st}")
report("管理员删除清了文件", not a3[0][1].exists())

print("\n=== 5. 404 与运行中保护 ===")
fake = uuid.uuid4().hex
st, _ = req(f"/assets/{fake}", "DELETE", token=ADMIN_T)
report("删不存在的素材 404", st == 404, f"status={st}")
st, _ = req(f"/tasks/{fake}", "DELETE", token=ADMIN_T)
report("删不存在的任务 404", st == 404, f"status={st}")

t5, a5 = asyncio.run(seed(ADMIN_ID, 1, status="running"))
st, body = req(f"/tasks/{t5}", "DELETE", token=ADMIN_T)
report("运行中任务拒删 400", st == 400, f"status={st} {body.get('detail')}")
report("运行中任务记录仍在", asyncio.run(asset_count(t5)) == 1)
report("运行中任务文件仍在", a5[0][1].exists())

print("\n=== 6. 越界防护：file_path 含 ../ 不能借删除删掉库外文件 ===")
from app.config import settings

outside = settings.STORAGE_DIR.parent / f"deltest-outside-{uuid.uuid4().hex[:8]}.txt"
outside.write_text("must survive")
t6, _ = asyncio.run(seed(ADMIN_ID, 0))
async def plant_traversal(task_id: str) -> str:
    from app.database import async_session
    from app.models.models import Asset
    async with async_session() as db:
        rel = f"../../{outside.name}"
        aid = uuid.uuid4().hex
        db.add(Asset(id=aid, task_id=task_id, file_path=rel,
                     file_name=outside.name, file_size=1, mime_type="text/plain",
                     tags=[], meta={}))
        await db.commit()
        return aid

trav_id = asyncio.run(plant_traversal(t6))
st, body = req(f"/assets/{trav_id}", "DELETE", token=ADMIN_T)
report("越界记录删除返回 200（只删记录）", st == 200, f"status={st}")
report("响应标明未删文件", body.get("file_removed") is False, f"file_removed={body.get('file_removed')}")
report("库外文件完好", outside.exists(), str(outside))
try:
    outside.unlink(missing_ok=True)
except (OSError, RuntimeError, SystemExit):
    pass  # 环境 safe-delete 钩子可能拦；断言已通过，收尾失败不该翻红

# ── 7. 响应契约：文件没清掉时必须如实上报，不许假装成功 ──
print("\n=== 7. 响应契约：文件清理失败要如实上报 ===")


async def plant_ghost(task_id: str) -> str:
    """造一条【有记录、无文件】的素材 —— 确定性地触发 file_removed=False"""
    from app.database import async_session
    from app.models.models import Asset
    async with async_session() as db:
        aid = uuid.uuid4().hex
        db.add(Asset(id=aid, task_id=task_id,
                     file_path=f"deltest-ghost-{uuid.uuid4().hex[:8]}/gone.png",
                     file_name="gone.png", file_size=14,
                     mime_type="image/png", tags=[], meta={}))
        await db.commit()
        return aid


t7, _ = asyncio.run(seed(ADMIN_ID, 0))
ghost = asyncio.run(plant_ghost(t7))
st, body = req(f"/assets/{ghost}", "DELETE", token=ADMIN_T)
report("幽灵素材删除返回 200（记录仍删）", st == 200, f"status={st}")
report("file_removed=False", body.get("file_removed") is False, str(body.get("file_removed")))
report("file_error 说明原因", bool(body.get("file_error")), repr(body.get("file_error")))

t7b, _ = asyncio.run(seed(ADMIN_ID, 0))
asyncio.run(plant_ghost(t7b))
st, body = req(f"/tasks/{t7b}", "DELETE", token=ADMIN_T)
report("删任务仍返回 200", st == 200, f"status={st}")
report("assets_deleted 计数正确", body.get("assets_deleted") == 1, str(body.get("assets_deleted")))
report("files_removed 少于 assets_deleted",
       body.get("files_removed", 0) < body.get("assets_deleted", 0), str(body))
report("file_errors 是列表", isinstance(body.get("file_errors"), list),
       type(body.get("file_errors")).__name__)

# ── 8. 批量下载 arcname 清洗：解压方不能被写到任意路径（zip-slip）──
print("\n=== 8. 批量下载 arcname 清洗（zip-slip 防护）===")
import io
import zipfile

t8, a8 = asyncio.run(seed(ADMIN_ID, 1))
dirty_id = a8[0][0]


async def dirty_the_name(aid: str) -> None:
    """把 file_name 改成带 ../ 的脏值，模拟库里混进历史脏数据"""
    from app.database import async_session
    from app.models.models import Asset
    async with async_session() as db:
        a = await db.get(Asset, aid)
        a.file_name = "../../../evil.zip-slip.png"
        await db.commit()


asyncio.run(dirty_the_name(dirty_id))
# 批量下载返回二进制流，req() 只解 JSON，这里直读
zr = urllib.request.Request(BASE + "/assets/batch-download", method="POST")
zr.add_header("Content-Type", "application/json")
zr.add_header("Authorization", "Bearer " + ADMIN_T)
with urllib.request.urlopen(zr, json.dumps([dirty_id]).encode()) as resp:
    zipped = resp.read()
names = zipfile.ZipFile(io.BytesIO(zipped)).namelist()
report("批量下载返回合法 ZIP", zipped[:2] == b"PK", repr(zipped[:8]))
report("文件确实进了 ZIP", len(names) == 1, str(names))
report("arcname 不含 ../（防 zip-slip）", not any(".." in n for n in names), str(names))
report("arcname 不含路径分隔符", not any(("/" in n or "\\" in n) for n in names), str(names))

# ── 清理 ─────────────────────────────────────────────
print("\n=== 清理 ===")


async def cleanup():
    """清掉测试造的任务/素材/文件/用户。素材删不掉只能走 DB（这正是 B31 要补的）"""
    from app.database import async_session
    from app.models.models import Task, Asset, User
    from sqlalchemy import select

    async with async_session() as db:
        for tid in SEEDED_TASKS:
            r = await db.execute(select(Asset).where(Asset.task_id == tid))
            for a in r.scalars().all():
                # 用应用自己的 filestore helper：自带穿越守卫，且吞掉本环境
                # safe-delete 钩子抛的 SystemExit，清理不会因为环境拦一下就崩
                from app.filestore import delete_asset_file
                delete_asset_file(a.file_path)
                await db.delete(a)
            t = await db.get(Task, tid)
            if t:
                await db.delete(t)
        r = await db.execute(select(User).where(User.username == MEMBER_NAME))
        u = r.scalar_one_or_none()
        if u:
            await db.delete(u)
        await db.commit()


asyncio.run(cleanup())
st, _ = req(f"/templates/{TPL_ID}", "DELETE", token=ADMIN_T)
report("清理测试模板", st == 200, f"status={st}")

print()
print(f"{'全部通过 ✓' if ok_all else '存在失败 ✗'}")
sys.exit(0 if ok_all else 1)
