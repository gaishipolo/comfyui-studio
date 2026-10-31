"""素材列表筛选验证 —— 时间范围 + 创建人（P1-3，2026-10-29）

覆盖：
1. created_from / created_to 时间范围（纯日期的「止」要含当天全天）
2. creator 按创建人筛（admin 视角挑人）
3. 组合筛选（时间 + 创建人 + 隔离规则叠加）
4. 回归守卫：筛选实现别被将来改没

测试数据全部自建（deltest- 前缀 + uuid），跑完自清理，不碰库里真实数据。
需要 COMFYUI_STUDIO_TEST_BASE_URL 指向正在运行的隔离后端。
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_env import API_BASE_URL  # noqa: E402

BASE = API_BASE_URL
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


async def seed(owner_id: str, created_at: datetime, name: str,
               mime: str = "image/png", ext: str = "png"):
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
        rel = f"deltest-{uuid.uuid4().hex[:8]}/{name}.{ext}"
        abs_path = settings.STORAGE_DIR / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"\x89PNG-test-bytes")
        db.add(Asset(
            id=aid, task_id=task_id, file_path=rel,
            file_name=f"{name}.{ext}", file_size=14,
            mime_type=mime, tags=[], meta={},
            created_at=created_at,
        ))
        await db.commit()
    SEEDED_TASKS.append(task_id)
    return aid


async def delete_member(username: str) -> bool:
    from sqlalchemy import select

    from app.database import async_session
    from app.models.models import User

    async with async_session() as database:
        user = (
            await database.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            return True
        await database.delete(user)
        await database.commit()
        return True


@contextmanager
def temporary_member():
    name = f"deltest-{uuid.uuid4().hex[:8]}"
    try:
        status, _ = req(
            "/auth/register",
            "POST",
            {"username": name, "password": "pw123456", "role": "member"},
            token=ADMIN_T,
        )
        report("创建临时成员", status == 200, f"status={status}")
        status, login = req(
            "/auth/login",
            "POST",
            {"username": name, "password": "pw123456"},
        )
        if status != 200:
            raise RuntimeError(f"临时成员登录失败: HTTP {status}")
        yield name, login["access_token"], login["user"]["id"]
    finally:
        print("\n=== 清理 ===")
        try:
            for task_id in list(SEEDED_TASKS):
                status, _ = req(f"/tasks/{task_id}", "DELETE", token=ADMIN_T)
                report(f"清理任务 {task_id[:8]}", status in {200, 404}, f"status={status}")
            status, _ = req(f"/templates/{TPL_ID}", "DELETE", token=ADMIN_T)
            report("清理测试模板", status in {200, 404}, f"status={status}")
        finally:
            report("清理临时成员", asyncio.run(delete_member(name)), name)


# 老素材（2026-10-01）归 admin，新素材（今天）也归 admin，再加一个今天的「他人」素材
OLD = datetime(2026, 10, 1, 12, 0, 0)
TODAY = datetime.now().replace(microsecond=0)

a_old = asyncio.run(seed(ADMIN_ID, OLD, "old-asset"))
a_new = asyncio.run(seed(ADMIN_ID, TODAY, "new-asset"))

with temporary_member() as (MEMBER_NAME, MEMBER_T, MEMBER_ID):
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

    st, lst = req(f"/assets?creator={ADMIN_ID}&limit=200", token=MEMBER_T)
    ids = {x["id"] for x in lst}
    report("member 借 creator 参数也穿不透隔离", st == 200 and a_new not in ids and a_old not in ids,
           f"返回 {len(lst)} 条")

    print("\n=== kind 类型筛选 ===")
    a_audio = asyncio.run(seed(ADMIN_ID, TODAY, "deltest-audio", mime="audio/mpeg", ext="mp3"))

    st, rows = req("/assets?limit=200&kind=audio", token=ADMIN_T)
    report("kind=audio 只返回音频",
           st == 200 and all((a.get("mime_type") or "").startswith("audio/") for a in rows),
           f"status={st} 共 {len(rows) if isinstance(rows, list) else '?'} 个")
    report("kind=audio 能查到刚造的音频",
           isinstance(rows, list) and any(a["id"] == a_audio for a in rows))

    st, rows = req("/assets?limit=200&kind=image", token=ADMIN_T)
    report("kind=image 不含音频",
           st == 200 and all(not (a.get("mime_type") or "").startswith("audio/") for a in rows))

    st, rows = req("/assets?limit=200&kind=video", token=ADMIN_T)
    report("kind=video 不含图片/音频",
           st == 200 and all((a.get("mime_type") or "").startswith("video/") for a in rows))

    st, _ = req("/assets?kind=xxx", token=ADMIN_T)
    report("非法 kind → 400（别静默当成全部）", st == 400, f"status={st}")

print()
print("全部通过 ✓" if ok_all else "存在失败 ✗")
sys.exit(0 if ok_all else 1)
