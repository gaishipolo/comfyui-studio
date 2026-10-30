"""用量统计验证（P2）—— 任务数/存储/耗时，仅 admin，可切时间段（2026-10-29）

产品拍板：**只统计不拦截**。统计维度是任务数（按状态分列）、存储占用、平均耗时；
仅 admin 看得到；时间段可切（纯日期含当天全天，与素材筛选同语义）。

测试数据全部自建（deltest- 前缀 + uuid），跑完自清理。
需要后端在 127.0.0.1:8000 运行。
"""
import asyncio
import json
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
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


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ── 账号 ─────────────────────────────────────────────────
_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
ADMIN_T = tok["access_token"]
ADMIN_ID = tok["user"]["id"]

MEMBER_NAME = f"deltest-usage-{uuid.uuid4().hex[:8]}"
st, _ = req("/auth/register", "POST",
            {"username": MEMBER_NAME, "password": "pw123456", "role": "member"}, token=ADMIN_T)
report("创建临时成员", st == 200, f"status={st}")
_, m = req("/auth/login", "POST", {"username": MEMBER_NAME, "password": "pw123456"})
MEMBER_T = m["access_token"]
MEMBER_ID = m["user"]["id"]

# ── 造模板 + 测试任务（确定性 created_at / status / 耗时）────
N = f"用量验证-{uuid.uuid4().hex[:8]}"
st, tpl = req("/templates", "POST", {"name": N, "is_active": True}, token=ADMIN_T)
report("创建模板", st == 200, f"status={st}")
TPL_ID = tpl["id"]

SEEDED_TASKS: list[str] = []

OLD = datetime(2026, 10, 1, 12, 0, 0)   # 老任务日
TODAY = datetime.now().replace(microsecond=0)  # 新任务日


def snapshot(token, q=""):
    """取一次用量快照：user_id -> 各指标。断言用「增量」对比，
    不假定库里是干净的 —— admin 名下可能有历史任务。"""
    st, d = req(f"/stats/usage{q}", token=token)
    assert st == 200, f"快照失败 {st}"
    return {u["user_id"]: u for u in d["by_user"]}, d["totals"]


def delta(after, before, uid, key):
    return after.get(uid, {}).get(key, 0) - before.get(uid, {}).get(key, 0)


async def seed(owner_id: str, status: str, created_at: datetime,
               duration_seconds: float | None, file_size: int = 1000):
    """造一个任务 + 1 个素材。duration_seconds=None 表示没有耗时（不算进平均）"""
    from app.database import async_session
    from app.models.models import Task, Asset
    from app.config import settings

    task_id = uuid.uuid4().hex
    aid = uuid.uuid4().hex
    started = created_at
    finished = (created_at + timedelta(seconds=duration_seconds)) if duration_seconds else None
    if not duration_seconds:
        started = None

    async with async_session() as db:
        db.add(Task(
            id=task_id, template_id=TPL_ID, created_by=owner_id,
            batch_id=uuid.uuid4().hex, status=status, progress=100.0,
            params={}, result={}, error_msg="",
            created_at=created_at, started_at=started, finished_at=finished,
        ))
        rel = f"deltest-{uuid.uuid4().hex[:8]}/u.png"
        abs_path = settings.STORAGE_DIR / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"\x89PNG-test-bytes")
        db.add(Asset(
            id=aid, task_id=task_id, file_path=rel,
            file_name="u.png", file_size=file_size,
            mime_type="image/png", tags=[], meta={},
            created_at=created_at,
        ))
        await db.commit()
    SEEDED_TASKS.append(task_id)
    return task_id


# admin 名下：老任务 1 个（done, 100s, 1000B）+ 新任务 3 个
#   done 200s、done 400s、error（无耗时）、cancelled（无耗时）→ 合计 4 个 done 耗时 [100,200,400]
# member 名下：新任务 2 个（done 60s、done 180s）→ 平均 120s
# 先取基线再播种 —— 断言全用增量，库里有历史数据也不怕
Q_OLD = f"?date_from={OLD.date()}&date_to={OLD.date()}"
Q_TODAY = f"?date_from={TODAY.strftime('%Y-%m-%d')}&date_to={TODAY.strftime('%Y-%m-%d')}"
base_by, base_t = snapshot(ADMIN_T)
base_old, _ = snapshot(ADMIN_T, Q_OLD)
base_today, _ = snapshot(ADMIN_T, Q_TODAY)

asyncio.run(seed(ADMIN_ID, "done", OLD, 100, 1000))
asyncio.run(seed(ADMIN_ID, "done", TODAY, 200, 2000))
asyncio.run(seed(ADMIN_ID, "done", TODAY, 400, 3000))
asyncio.run(seed(ADMIN_ID, "error", TODAY, None, 4000))
asyncio.run(seed(ADMIN_ID, "cancelled", TODAY, None, 5000))
asyncio.run(seed(MEMBER_ID, "done", TODAY, 60, 6000))
asyncio.run(seed(MEMBER_ID, "done", TODAY, 180, 7000))

print("\n=== 1. 权限 ===")
st, d = req("/stats/usage", token=ADMIN_T)
report("admin 能看用量统计", st == 200 and "by_user" in d, f"status={st}")

st, d = req("/stats/usage", token=MEMBER_T)
report("member 被拒 403", st == 403 and "仅管理员" in d.get("detail", ""),
       f"{st} {d.get('detail', '')}")

print("\n=== 2. 无区间：增量核对（不假定库里干净）===")
by, totals = snapshot(ADMIN_T)
report("admin 任务数 +5", delta(by, base_by, ADMIN_ID, "task_total") == 5,
       f"got +{delta(by, base_by, ADMIN_ID, 'task_total')}")
report("admin 成功 +3", delta(by, base_by, ADMIN_ID, "task_done") == 3,
       f"got +{delta(by, base_by, ADMIN_ID, 'task_done')}")
report("admin 失败 +1", delta(by, base_by, ADMIN_ID, "task_error") == 1,
       f"got +{delta(by, base_by, ADMIN_ID, 'task_error')}")
report("admin 取消 +1", delta(by, base_by, ADMIN_ID, "task_cancelled") == 1,
       f"got +{delta(by, base_by, ADMIN_ID, 'task_cancelled')}")
report("admin 存储 +15000B", delta(by, base_by, ADMIN_ID, "storage_bytes") == 15000,
       f"got +{delta(by, base_by, ADMIN_ID, 'storage_bytes')}")
report("member 任务数 +2", delta(by, base_by, MEMBER_ID, "task_total") == 2,
       f"got +{delta(by, base_by, MEMBER_ID, 'task_total')}")
report("member 存储 +13000B", delta(by, base_by, MEMBER_ID, "storage_bytes") == 13000,
       f"got +{delta(by, base_by, MEMBER_ID, 'storage_bytes')}")

# 平均耗时不能用增量（不是可加指标），member 是新建账号名下只有测试数据 → 精确断言
b = by[MEMBER_ID]
report("member 平均耗时 120s（[60,180]）",
       b["avg_duration_seconds"] is not None and abs(b["avg_duration_seconds"] - 120) < 0.01,
       f"got {b['avg_duration_seconds']}")

# 合计 = 各行之和（对任意库状态都成立）
report("合计任务数 = 各行之和",
       totals["task_total"] == sum(u["task_total"] for u in by.values()),
       f"got {totals['task_total']}")
report("合计存储 = 各行之和",
       totals["storage_bytes"] == sum(u["storage_bytes"] for u in by.values()),
       f"got {totals['storage_bytes']}")
report("合计成功 = 各行之和",
       totals["task_done"] == sum(u["task_done"] for u in by.values()),
       f"got {totals['task_done']}")

print("\n=== 3. 时间区间（同样用增量）===")
# 基线 base_old / base_today 是播种前取的（Q_OLD / Q_TODAY 同口径），
# 这里播种后用同样的查询参数再取一次 after —— 过滤条件一致 delta 才可比
after_old, _ = snapshot(ADMIN_T, Q_OLD)
report("区间=10-01：admin +1 个任务", delta(after_old, base_old, ADMIN_ID, "task_total") == 1,
       f"got +{delta(after_old, base_old, ADMIN_ID, 'task_total')}")

after_today, _ = snapshot(ADMIN_T, Q_TODAY)
report("区间=今天：admin +4 个任务", delta(after_today, base_today, ADMIN_ID, "task_total") == 4,
       f"got +{delta(after_today, base_today, ADMIN_ID, 'task_total')}")

# 起点在未来：比基线还少（历史数据里不可能有未来任务）—— 直接断言为空更稳
future = (TODAY + timedelta(days=3)).strftime("%Y-%m-%d")
st, d = req(f"/stats/usage?date_from={future}", token=ADMIN_T)
report("未来起点：by_user 为空", st == 200 and len(d["by_user"]) == 0,
       f"返回 {len(d['by_user'])} 条")

print("\n=== 4. 口径守卫 ===")
# 无完成任务的用户，平均耗时应为 null（不是 0）
# 用一个只有 error 任务的临时用户验证
ERR_NAME = f"deltest-usageerr-{uuid.uuid4().hex[:8]}"
req("/auth/register", "POST",
    {"username": ERR_NAME, "password": "pw123456", "role": "member"}, token=ADMIN_T)
_, em = req("/auth/login", "POST", {"username": ERR_NAME, "password": "pw123456"})
ERR_ID = em["user"]["id"]
asyncio.run(seed(ERR_ID, "error", TODAY, None, 999))
st, d = req("/stats/usage", token=ADMIN_T)
by = {u["user_id"]: u for u in d["by_user"]}
report("只有失败任务的用户，平均耗时为 null（不瞎填 0）",
       by.get(ERR_ID, {}).get("avg_duration_seconds") is None,
       f"got {by.get(ERR_ID, {}).get('avg_duration_seconds')}")

print("\n=== 5. 回归守卫 ===")
src = (DB_PATH.parent / "app/api/stats.py").read_text(encoding="utf-8")
report("统计端点只给 admin", 'raise HTTPException(status_code=403' in src and "is_admin" in src)
report("耗时只算 done（不把 error/cancelled 混进平均）",
       'if status == "done"' in src and '"durations"' in src)
report("时间解析走共享 parse_day（不复制第二份）",
       "from app.timeutils import parse_day" in src)
report("assets.py 也用共享 parse_day（不保留本地副本）",
       "from app.timeutils import parse_day" in (DB_PATH.parent / "app/api/assets.py").read_text(encoding="utf-8"))

# ── 清理 ─────────────────────────────────────────────────
print("\n=== 清理 ===")
for tid in SEEDED_TASKS:
    st, _ = req(f"/tasks/{tid}", "DELETE", token=ADMIN_T)
    report(f"清理任务 {tid[:8]}", st == 200, f"status={st}")
st, _ = req(f"/templates/{TPL_ID}", "DELETE", token=ADMIN_T)
report("清理模板", st == 200, f"status={st}")

conn = sqlite3.connect(DB_PATH)
conn.execute("DELETE FROM users WHERE username LIKE 'deltest-usage%'")
conn.commit()
conn.close()
report("清理临时成员", True, f"{MEMBER_NAME} / {ERR_NAME}")

print(f"\n{'全部通过 ✓' if ok_all else '存在失败 ✗'}")
sys.exit(0 if ok_all else 1)
