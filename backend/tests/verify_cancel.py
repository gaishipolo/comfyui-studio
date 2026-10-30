"""任务取消 + 超时中断验证。

背景：视频任务跑几分钟，用户点错/想改参数只能干等；超时写死 600 秒且超时后
不通知引擎，prompt 继续占显存、跑完的产物成孤儿。

本脚本覆盖四块：
  1. 引擎侧取消的三条路径（新版 jobs/cancel → 降级 /queue delete → /interrupt）
  2. 协作式取消（轮询间隙退出，不硬砍协程）
  3. 超时必须顺手停掉引擎
  4. 回归守卫（超时不得再写死、取消终态不得被覆盖、重试要清取消登记）

全部用假客户端/假 provider，不连真引擎，可重复跑。
"""
import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.task_service as ts
from app.engines.base import GenerationResult, TaskStatus
from app.engines.comfyui import ComfyUIProvider
from app.services.task_service import TaskService, clear_cancel_request

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


import httpx


def FakeResp(status_code=200, json_data=None):
    """造一个**真的** httpx.Response。

    别自己造假响应对象 —— raise_for_status() 抛什么、json() 什么时候抛
    ValueError，只有跟线上一致才测得准（第一版自造的假件抛 RuntimeError，
    生产代码 except httpx.HTTPError 根本接不住，测试就假绿了）。
    """
    return httpx.Response(
        status_code,
        json=json_data if json_data is not None else {},
        request=httpx.Request("POST", "http://fake"),
    )


class FakeHTTP:
    """记录每次请求，按 (method, path) 顺序吐出预设响应。"""

    is_closed = False  # 让 ComfyUIProvider._get_client() 认它是个可用客户端

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        for i, (m, p, resp) in enumerate(self.script):
            if m == method and p == path:
                self.script.pop(i)
                return resp
        return FakeResp(404)

    async def post(self, path, json=None):
        return self._next("POST", path, json=json)

    async def get(self, path):
        return self._next("GET", path)


def make_provider(http):
    p = ComfyUIProvider(base_url="http://fake")
    p._client = http
    return p


# ── 1. 首选路径：新版 POST /api/jobs/cancel ──────────────────────
print("=== 引擎取消 · 首选 /api/jobs/cancel ===")

h = FakeHTTP([("POST", "/api/jobs/cancel", FakeResp(200, {"cancelled": True}))])
report(
    "jobs/cancel 返回 true → 取消成功",
    asyncio.run(make_provider(h).cancel("p1")) is True,
)
report("只发了一次请求", len(h.calls) == 1, str(h.calls))
report("请求体是 job_ids 列表", h.calls[0][2] == {"job_ids": ["p1"]}, str(h.calls[0][2]))

h = FakeHTTP([("POST", "/api/jobs/cancel", FakeResp(200, {"cancelled": False}))])
report(
    "jobs/cancel 返回 false（任务早已跑完）→ False，且不算异常",
    asyncio.run(make_provider(h).cancel("p1")) is False,
)

h = FakeHTTP([("POST", "/api/jobs/cancel", FakeResp(400, {"error": "bad"}))])
report(
    "jobs/cancel 400（入参被拒）→ 直接 False，不再降级",
    asyncio.run(make_provider(h).cancel("p1")) is False,
)
report("400 后不再发别的请求", len(h.calls) == 1, str(h.calls))

h = FakeHTTP([])
report("空 provider_task_id → False 且一次请求都不发", asyncio.run(make_provider(h).cancel("")) is False and len(h.calls) == 0)

# ── 2. 降级路径：老版本没有 /api/jobs ──────────────────────────
print("=== 引擎取消 · 降级到 /queue + /interrupt ===")

QUEUE_BOTH = {"queue_running": [[1, "run-1", {}, {}, []]], "queue_pending": [[0, "pend-1", {}, {}, []]]}

h = FakeHTTP([
    ("POST", "/api/jobs/cancel", FakeResp(404)),
    ("GET", "/queue", FakeResp(200, QUEUE_BOTH)),
    ("POST", "/queue", FakeResp(200)),
])
report(
    'pending 任务 → POST /queue {"delete":[id]}',
    asyncio.run(make_provider(h).cancel("pend-1")) is True,
)
posted = [c for c in h.calls if c[0] == "POST" and c[1] == "/queue"]
report("delete 只撤自己那条，不是 clear 全清", posted and posted[0][2] == {"delete": ["pend-1"]}, str(posted))

h = FakeHTTP([
    ("POST", "/api/jobs/cancel", FakeResp(404)),
    ("GET", "/queue", FakeResp(200, QUEUE_BOTH)),
    ("POST", "/interrupt", FakeResp(200)),
])
report(
    "running 任务 → POST /interrupt",
    asyncio.run(make_provider(h).cancel("run-1")) is True,
)
inter = [c for c in h.calls if c[1] == "/interrupt"]
report("interrupt 带 prompt_id 定向，不是全局", inter and inter[0][2] == {"prompt_id": "run-1"}, str(inter))
report("running 时不误发 delete", not any(c[1] == "/queue" and c[0] == "POST" for c in h.calls))

h = FakeHTTP([
    ("POST", "/api/jobs/cancel", FakeResp(404)),
    ("GET", "/queue", FakeResp(200, QUEUE_BOTH)),
])
report(
    "既不在 pending 也不在 running（已跑完）→ False",
    asyncio.run(make_provider(h).cancel("gone")) is False,
)
report("那种情况下不乱发中断", not any(c[0] == "POST" and c[1] != "/api/jobs/cancel" for c in h.calls))

h = FakeHTTP([
    ("POST", "/api/jobs/cancel", FakeResp(404)),
    ("GET", "/queue", FakeResp(200, {"queue_running": [], "queue_pending": []})),
])
report("队列为空 → False", asyncio.run(make_provider(h).cancel("x")) is False)

h = FakeHTTP([("POST", "/api/jobs/cancel", FakeResp(404)), ("GET", "/queue", FakeResp(500))])
report("查队列就失败 → False，不抛异常", asyncio.run(make_provider(h).cancel("x")) is False)

# 队列项形状容错：字符串项/短元组不该让解析炸掉
h = FakeHTTP([
    ("POST", "/api/jobs/cancel", FakeResp(404)),
    ("GET", "/queue", FakeResp(200, {"queue_running": ["oops", [1]], "queue_pending": []})),
])
report("畸形队列项不炸 → False", asyncio.run(make_provider(h).cancel("x")) is False)

# ── 3. 协作式取消 ─────────────────────────────────────────────
print("=== 协作式取消（_poll_result） ===")


class FakeProvider:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [])
        self.cancel_calls = []

    async def get_status(self, prompt_id):
        if self.statuses:
            return self.statuses.pop(0)
        return GenerationResult(status=TaskStatus.RUNNING, progress=1.0)

    async def cancel(self, prompt_id):
        self.cancel_calls.append(prompt_id)
        return True


class FakeDB:
    def __init__(self, task=None, template=None):
        self.task = task
        self.template = template
        self.commits = 0

    async def get(self, model, pk):
        from app.models.models import Task as T, Template as Tp
        if model is T:
            return self.task
        if model is Tp:
            return self.template
        return None

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_task(task_id="t1"):
    return SimpleNamespace(
        id=task_id, status="running", progress=0.0, params={}, result={},
        error_msg="", provider_task_id="p1", template_id="tpl",
        created_at=None, started_at=None, finished_at=None,
    )


ts._cancel_requested.clear()
t = make_task()
ts._cancel_requested.add("t1")
prov = FakeProvider()
res = asyncio.run(TaskService._poll_result(FakeDB(task=t), t, prov, "p1", max_wait=5))
report("登记了取消 → 立刻返回 CANCELLED", res.status == TaskStatus.CANCELLED, res.status)
report("CANCELLED 时不请求引擎状态", prov.statuses == [])
report("CANCELLED 时不去打扰引擎（本地已停）", prov.cancel_calls == [])

ts._cancel_requested.clear()
t = make_task("t2")
prov = FakeProvider([GenerationResult(status=TaskStatus.RUNNING, progress=40.0)])
res = asyncio.run(TaskService._poll_result(FakeDB(task=t), t, prov, "p2", max_wait=100))
report("没取消时照常轮询，但 max_wait 内未完成 → 走超时", res.status == TaskStatus.ERROR)

ts._cancel_requested.clear()
t = make_task("t3")
prov = FakeProvider([GenerationResult(status=TaskStatus.DONE, progress=100.0, outputs=[])])
res = asyncio.run(TaskService._poll_result(FakeDB(task=t), t, prov, "p3"))
report("正常完成 → DONE，不受取消影响", res.status == TaskStatus.DONE)

clear_cancel_request("t1")
report("clear_cancel_request 能清掉登记", not ts._cancel_requested_for("t1"))

# ── 4. 超时必须停引擎 ─────────────────────────────────────────
print("=== 超时要顺手停掉引擎 ===")

ts._cancel_requested.clear()
t = make_task("t4")
prov = FakeProvider()
db = FakeDB(task=t)
res = asyncio.run(TaskService._poll_result(db, t, prov, "p4", max_wait=0))
report("超时 → status=error", res.status == TaskStatus.ERROR)
report("超时后向引擎发了 cancel", prov.cancel_calls == ["p4"], str(prov.cancel_calls))
report("超时文案说清了上限与已中断", "超时" in res.error and "中断" in res.error, res.error)

ts._cancel_requested.clear()
t = make_task("t5")
prov = FakeProvider()

class NoCancel(FakeProvider):
    async def cancel(self, prompt_id):
        raise NotImplementedError("这引擎不支持取消")

res = asyncio.run(TaskService._poll_result(FakeDB(task=t), t, NoCancel(), "p5", max_wait=0))
report("引擎不支持取消时超时仍然能收尾（不炸）", res.status == TaskStatus.ERROR, res.error)

# ── 5. cancel_task 状态机 ─────────────────────────────────────
print("=== cancel_task 状态机 ===")


def run_cancel(task_obj, template_obj=None, provider=None):
    ts._cancel_requested.clear()
    db = FakeDB(task=task_obj, template=template_obj)
    real = ts.get_provider
    ts.get_provider = lambda engine: provider or FakeProvider()
    try:
        return asyncio.run(TaskService.cancel_task(db, task_obj.id)), db
    finally:
        ts.get_provider = real


for terminal in ("done", "error", "cancelled"):
    t = make_task("t6")
    t.status = terminal
    t.provider_task_id = "p6"
    try:
        run_cancel(t)
        report(f"终态 {terminal} 不可取消", False)
    except ValueError as e:
        report(f"终态 {terminal} 不可取消", True, str(e))

t = make_task("t7")
t.status = "running"
t.provider_task_id = "p7"
prov = FakeProvider()
out, db = run_cancel(t, SimpleNamespace(engine="comfyui"), prov)
report("running 可取消", out.status == "cancelled")
report("取消写入了终态时间", t.finished_at is not None)
report("取消通知了引擎", prov.cancel_calls == ["p7"], str(prov.cancel_calls))
report("取消登记留下痕迹（执行协程能看到）", ts._cancel_requested_for("t7"))

t = make_task("t8")
t.status = "running"
t.provider_task_id = ""   # 还没提交成功，拿不到 prompt_id
prov = FakeProvider()
run_cancel(t, SimpleNamespace(engine="comfyui"), prov)
report("没有 provider_task_id 时不误调引擎", prov.cancel_calls == [])
report("但取消登记照样落下（由 execute_task 补刀）", ts._cancel_requested_for("t8"))

t = make_task("t9")
t.status = "pending"
prov = FakeProvider()

class NoCancelProvider(FakeProvider):
    async def cancel(self, prompt_id):
        raise NotImplementedError

out, _ = run_cancel(t, SimpleNamespace(engine="comfyui"), NoCancelProvider())
report("引擎不支持取消时，本地仍标记 cancelled", out.status == "cancelled")

# ── 6. 回归守卫 ───────────────────────────────────────────────
print("=== 回归守卫 ===")

sig = inspect.signature(TaskService._poll_result)
report("_poll_result 超时不得再写死 600", sig.parameters["max_wait"].default is None, str(sig))
src = inspect.getsource(TaskService._poll_result)
report("超时分支必须调用 provider.cancel", "provider.cancel" in src)
report("轮询循环必须检查取消登记", "_cancel_requested_for" in src)

esrc = inspect.getsource(TaskService.execute_task)
report("execute_task 必须处理 submit 后的取消竞态", "_cancel_requested_for" in esrc)

# 重构后 execute_task / resume_task 共用 _finish_task 收尾，「CANCELLED 不覆盖
# 终态」这条不变量落在 _finish_task 里 —— 守卫跟着搬家，别再盯着 execute_task 的
# 源码（那样重构一次就假红一次）。同时卡住两个入口都必须走 _finish_task，
# 免得将来有人另起一条收尾路径把不变量绕过去。
fsrc = inspect.getsource(TaskService._finish_task)
report("收尾必须吞掉 CANCELLED 不覆盖终态", "TaskStatus.CANCELLED" in fsrc)
rsrc = inspect.getsource(TaskService.resume_task)
report(
    "execute_task 与 resume_task 都要走 _finish_task",
    "_finish_task" in esrc and "_finish_task" in rsrc,
)

csrc = inspect.getsource(TaskService.cancel_task)
report("cancel_task 先登记再落库", csrc.index("_cancel_requested.add") < csrc.index('task.status = "cancelled"'))

from app.engines.base import GenerationProvider
report("GenerationProvider.cancel 是协程方法", inspect.iscoroutinefunction(GenerationProvider.cancel))


class _MinimalProvider(GenerationProvider):
    """只补完抽象方法，故意不实现 cancel —— 用来验证默认行为。"""

    async def submit(self, workflow, params):
        return "id"

    async def get_status(self, task_id):
        return GenerationResult()

    async def download_output(self, filename, subfolder="", output_type="output"):
        return b""

    async def health_check(self):
        return True


try:
    asyncio.run(_MinimalProvider().cancel("x"))
    report("老引擎没实现 cancel → 抛 NotImplementedError", False)
except NotImplementedError:
    report("老引擎没实现 cancel → 抛 NotImplementedError（调用方有 except 兜住）", True)

import app.engines.base as base_mod
report("TaskStatus 含 cancelled 终态", TaskStatus.CANCELLED.value == "cancelled")

print()
print("ALL PASS" if ok_all else "SOME FAILED")
sys.exit(0 if ok_all else 1)
