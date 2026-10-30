"""素材列表筛选验证 —— 时间范围 + 创建人（P1-3，2026-10-29）

覆盖：
1. created_from / created_to 时间范围（纯日期的「止」要含当天全天）
2. creator 按创建人筛（admin 视角挑人）
3. 组合筛选（时间 + 创建人 + 隔离规则叠加）
4. 回归守卫：筛选实现别被将来改没

测试数据全部自建（deltest- 前缀 + uuid），跑完自清理，不碰库里真实数据。
需要后端在 127.0.0.1:8000 运行。
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
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

N = f"素材筛选-{uuid.uuid4().hex[:8]}"
st, tpl = req("/templates", "POST", {"name": N, "is_active": True}, token=ADMIN_T)
report("创建模板", st == 200, f"status={st}")
TPL_ID = tpl["id"]

SEEDED_TASKS: list[str] = []


async def seed(owner_id: str, created_at: datetime, name: str):
    """造一个任务 + 1 个素材，created_at 指定（时间筛选要确定性）"""
    from app.database import async_session
    from app.models.models import Task, Asset
    from app.config import settings

    task_id = uuid.uuid4().hex
    aid = uuid.uuid4().hex
    async with async_session() as db:
        db.add(Task(
            id=task_id, template_id=TPL_ID, created_by=owner_id,
            batch_id=uuid.uuid4().hex, status="done", progress=100.0,
            params={}, result={}, error_msg="",
            created_at=created_at,
        ))
        rel = f"deltest-{uuid.uuid4().hex[:8]}/{name}.png"
        abs_path = settings.STORAGE_DIR / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"\x89PNG-test-bytes")
        db.add(Asset(
            id=aid, task_id=task_id, file_path=rel,
            file_name=f"{name}.png", file_size=14,
            mime_type="image/png", tags=[], meta={},
            created_at=created_at,
        ))
        await db.commit()
    SEEDED_TASKS.append(task_id)
    return aid


# 老素材（2026-10-01）归 admin，新素材（今天）也归 admin，再加一个今天的「他人」素材
OLD = datetime(2026, 10, 1, 12, 0, 0)
TODAY = datetime.now().replace(microsecond=0)

a_old = asyncio.run(seed(ADMIN_ID, OLD, "old-asset"))
a_new = asyncio.run(seed(ADMIN_ID, TODAY, "new-asset"))

# 「他人」素材：建临时用户（跑完删），不用真实账号 —— 真实账号密码未知，登录会跳过断言
MEMBER_NAME = f"deltest-{uuid.uuid4().hex[:8]}"
st, _ = req("/auth/register", "POST",
            {"username": MEMBER_NAME, "password": "pw123456", "role": "member"}, token=ADMIN_T)
report("创建临时成员", st == 200, f"status={st}")
_, m = req("/auth/login", "POST", {"username": MEMBER_NAME, "password": "pw123456"})
MEMBER_T = m["access_token"]
MEMBER_ID = m["user"]["id"]
a_other = asyncio.run(seed(MEMBER_ID, TODAY, "other-asset"))

report("造好三份测试素材", True, f"old={a_old[:6]} new={a_new[:6]} other={a_other[:6]}")

print("\n=== 1. 时间范围 ===")
TODAY_STR = TODAY.strftime("%Y-%m-%d")
FUTURE_STR = (TODAY + timedelta(days=3)).strftime("%Y-%m-%d")

st, lst = req(f"/assets?created_from={TODAY_STR}&limit=200", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("created_from 滤掉老素材", st == 200 and a_new in ids and a_old not in ids,
       f"返回 {len(lst)} 条")

st, lst = req(f"/assets?created_to={OLD.strftime('%Y-%m-%d')}&limit=200", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("created_to 滤掉新素材（纯日期含当天全天）", st == 200 and a_old in ids and a_new not in ids,
       f"返回 {len(lst)} 条")

st, lst = req(f"/assets?created_from={TODAY_STR}&created_to={TODAY_STR}&limit=200", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("同日范围只留今天的", st == 200 and a_new in ids and a_old not in ids, f"返回 {len(lst)} 条")

st, lst = req(f"/assets?created_from={FUTURE_STR}&limit=200", token=ADMIN_T)
report("未来起点全部滤掉", st == 200 and len(lst) == 0, f"返回 {len(lst)} 条")

print("\n=== 2. 创建人 ===")
st, lst = req(f"/assets?creator={MEMBER_ID}&limit=200", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("creator 只留该用户的", st == 200 and a_other in ids and a_new not in ids and a_old not in ids,
       f"返回 {len(lst)} 条")

st, lst = req(f"/assets?creator={ADMIN_ID}&limit=200", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("creator=admin 留 admin 的", st == 200 and a_new in ids and a_old in ids and a_other not in ids,
       f"返回 {len(lst)} 条")

st, lst = req(f"/assets?creator={MEMBER_ID}&created_from=2026-10-27&limit=200", token=ADMIN_T)
ids = {x["id"] for x in lst}
report("组合筛选（人 + 时间）", st == 200 and ids == {a_other}, f"返回 {len(lst)} 条")

# 隔离规则仍要生效：member 查别人的 creator 也只见共享的（默认全不共享 → 空）
st, lst = req(f"/assets?creator={ADMIN_ID}&limit=200", token=MEMBER_T)
ids = {x["id"] for x in lst}
report("member 借 creator 参数也穿不透隔离", st == 200 and a_new not in ids and a_old not in ids,
       f"返回 {len(lst)} 条")

print("\n=== 3. 回归守卫 ===")
import inspect
import app.api.assets as amod

src = inspect.getsource(amod.list_assets)
report("list_assets 必须处理三个筛选参数",
       "created_from" in src and "created_to" in src and "creator" in src)
report("纯日期的「止」必须含当天（end=True 走 _parse_day）",
       "_parse_day(created_to, end=True)" in src)
report("creator 筛选必须与隔离过滤并存（都挂在同一 query 上）",
       "Task.created_by == creator" in src and "Asset.is_shared" in src)

print("\n=== 清理 ===")
for tid in list(SEEDED_TASKS):
    st, _ = req(f"/tasks/{tid}", "DELETE", token=ADMIN_T)
    report(f"清理任务 {tid[:8]}", st == 200, f"status={st}")

st, _ = req(f"/templates/{TPL_ID}", "DELETE", token=ADMIN_T)
report("清理测试模板", st == 200, f"status={st}")

print()
print("全部通过 ✓" if ok_all else "存在失败 ✗")
sys.exit(0 if ok_all else 1)
