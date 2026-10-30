"""任务进度验证 —— 之前进度条第一次刷新就是 50%、之后不动、跑完直接 100%。

根因是 get_status 里写死 `progress=50.0`。ComfyUI 的 REST 面压根没有进度接口：
  /history/{id}  跑完才有内容
  /api/jobs/{id} 只有 pending/in_progress/completed/failed，没有步数
  /queue         只能区分排队 / 运行
真实进度只在 WebSocket 上推（见 app/engines/comfyui_progress.py 模块注释）。

本脚本用**伪造的 WS 消息序列**驱动 ProgressTracker（不连真引擎，可重复跑），
验证三件事：进度单调不回退、没数据时能降级、终态归一到 1。
"""
import asyncio
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.comfyui import ComfyUIProvider
from app.engines.comfyui_progress import ProgressTracker, reachable_node_count, ws_uri

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def msg(kind, **data):
    return json.dumps({"type": kind, "data": data})


# ── 1. 进度分母：可达节点数，不是 len(workflow) ────────────────────
print("=== 进度分母 reachable_node_count ===")

graph = {
    "1": {"class_type": "LoadImage", "inputs": {}},
    "2": {"class_type": "VAEEncode", "inputs": {"pixels": ["1", 0]}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
    "9": {"class_type": "MarkdownNote", "inputs": {"text": "孤立节点"}},   # 没接线
}
report("孤立节点不计入分母（4 -> 3）", reachable_node_count(graph) == 3,
       f"got {reachable_node_count(graph)}")

chain = {
    "1": {"inputs": {}},
    "2": {"inputs": {"a": ["1", 0]}},
    "3": {"inputs": {"a": ["2", 0]}},
}
report("纯链路全部可达（3）", reachable_node_count(chain) == 3)

report("空工作流 -> 0", reachable_node_count({}) == 0)
report("非 dict -> 0", reachable_node_count(None) == 0 and reachable_node_count("x") == 0)

for name in ("LTX-2.3_-_I2V_T2V_Basic_GGUF_147b75.json",
             "image_flux2_klein_text_to_image_ref_face.json"):
    p = Path(__file__).resolve().parents[1] / "workflows" / name
    if p.exists():
        wf = json.loads(p.read_text(encoding="utf-8"))
        got = reachable_node_count(wf)
        report(f"{name[:26]}… 分母 ≤ 总数", 0 < got <= len(wf),
               f"{got}/{len(wf)}")

# ── 2. WS URI ─────────────────────────────────────────────────────
print("\n=== WS URI ===")
report("http -> ws", ws_uri("http://h:8888", "abc") == "ws://h:8888/ws?clientId=abc")
report("https -> wss", ws_uri("https://h/", "abc") == "wss://h/ws?clientId=abc")
report("client_id 需 URL 编码", "a%20b" in ws_uri("http://h", "a b"))

# ── 3. 进度单调性（核心回归点）──────────────────────────────────
print("\n=== 进度单调性 ===")

t = ProgressTracker("http://h:8888", "cid")
t.record_plan("p1", 3)

report("还没收到任何事件 -> None（触发降级，不是瞎给数字）",
       t.ratio("p1") is None)

ratios = []
t._on_message(msg("executing", node="A", prompt_id="p1"))
ratios.append(t.ratio("p1"))
t._on_message(msg("progress", value=5, max=10, prompt_id="p1", node="A"))
ratios.append(t.ratio("p1"))
t._on_message(msg("progress", value=10, max=10, prompt_id="p1", node="A"))
ratios.append(t.ratio("p1"))
t._on_message(msg("executing", node="B", prompt_id="p1"))   # 换节点，步数清零
ratios.append(t.ratio("p1"))
# 乱序：上一个节点的收尾进度晚到，随后新节点从 0 开始
t._on_message(msg("progress", value=10, max=10, prompt_id="p1", node="A"))
ratios.append(t.ratio("p1"))
t._on_message(msg("progress", value=0, max=20, prompt_id="p1", node="B"))
ratios.append(t.ratio("p1"))
t._on_message(msg("progress", value=20, max=20, prompt_id="p1", node="B"))
ratios.append(t.ratio("p1"))

report("单调不回退（事件乱序也不倒退）",
       all(b >= a for a, b in zip(ratios, ratios[1:])),
       " -> ".join(f"{r:.3f}" for r in ratios))
report("确实在动（不全是同一个值）", len(set(ratios)) >= 4, f"不同取值 {len(set(ratios))} 个")
report("未完成时 < 1.0", ratios[-1] < 1.0, f"last={ratios[-1]:.3f}")

# ── 4. 终态归一到 1 ──────────────────────────────────────────────
print("\n=== 终态 ===")
for kind in ("execution_success", "execution_error"):
    t2 = ProgressTracker("http://h", "c")
    t2.record_plan("p", 5)
    t2._on_message(msg("executing", node="A", prompt_id="p"))
    t2._on_message(msg(kind, prompt_id="p"))
    report(f"{kind} -> 1.0", t2.ratio("p") == 1.0)

t3 = ProgressTracker("http://h", "c")
t3.record_plan("p", 5)
t3._on_message(msg("executing", node=None, prompt_id="p"))
report("executing(node=null) 视为结束 -> 1.0", t3.ratio("p") == 1.0)

# ── 5. 其它 prompt 互不串台 / 清理 / 上限 ─────────────────────────
print("\n=== 隔离与清理 ===")
t4 = ProgressTracker("http://h", "c")
t4.record_plan("a", 4)
t4.record_plan("b", 4)
t4._on_message(msg("executing", node="N1", prompt_id="a"))
report("不同 prompt 进度互不影响", t4.ratio("b") is None and t4.ratio("a") is not None)

t4.forget("a")
report("forget 后清空", t4.ratio("a") is None and t4.tracked() == 1)

t5 = ProgressTracker("http://h", "c")
for i in range(300):
    t5.record_plan(f"p{i}", 3)
report("超出上限自动淘汰（≤256）", t5.tracked() <= 256, f"tracked={t5.tracked()}")

# ── 6. 进度映射区间 + 降级 ───────────────────────────────────────
print("\n=== 进度映射与降级 ===")


class FakeResp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class FakeClient:
    def __init__(self, data):
        self._d = data

    async def get(self, path):
        return FakeResp(self._d)


async def _checks():
    running = FakeClient({"queue_running": [[1, "p1", {}, {}]], "queue_pending": []})
    pending = FakeClient({"queue_running": [], "queue_pending": [[1, "p1", {}, {}]]})

    report("_is_running：执行中 -> True",
           await ComfyUIProvider._is_running(running, "p1") is True)
    report("_is_running：排队中 -> False",
           await ComfyUIProvider._is_running(pending, "p1") is False)
    report("_is_running：不是自己的 prompt -> False",
           await ComfyUIProvider._is_running(running, "other") is False)
    report("_is_running：/queue 挂了也不炸 -> False",
           await ComfyUIProvider._is_running(FakeClient(None), "p1") is False)

    # 进度映射：3 + 94*ratio，压在 [3, 97]
    prov = ComfyUIProvider.__new__(ComfyUIProvider)   # 不跑 __init__，避免建 WS
    prov._progress = ProgressTracker("http://h", "c")

    async def _noop():
        return None

    prov._progress.ensure_running = _noop   # 别真去连 http://h

    got = await prov._running_progress(pending, "nobody")
    report("无任何数据且在排队 -> 低位（不是 50）",
           got == round(3.0 + 94 * 0.02, 1), f"{got}%")

    got = await prov._running_progress(running, "p1")
    report("无 WS 数据但已在跑 -> 中低位（不是 50）",
           got == round(3.0 + 94 * 0.35, 1), f"{got}%")

    prov._progress.record_plan("p1", 4)
    prov._progress._on_message(msg("executing", node="A", prompt_id="p1"))
    got = await prov._running_progress(running, "p1")
    report("有真实进度时用真实值", 3.0 <= got <= 97.0, f"{got}%")

    prov._progress._on_message(msg("execution_success", prompt_id="p1"))
    got = await prov._running_progress(running, "p1")
    report("WS 说完了也不给 100（100% 留给 /history 确认）", got == 97.0, f"{got}%")


asyncio.run(_checks())

# ── 7. 回归守卫：get_status 不许再写死进度 ─────────────────────────
print("\n=== 回归守卫 ===")
src = inspect.getsource(ComfyUIProvider.get_status)
report("get_status 不再返回写死的 progress=50.0", "progress=50.0" not in src)
report("get_status 走 _running_progress", "_running_progress" in src)

all_src = inspect.getsource(ComfyUIProvider)
report("整个 provider 里没有写死的 RUNNING 进度常数",
       "TaskStatus.RUNNING, progress=" not in all_src)

print("\n" + ("ALL OK" if ok_all else "HAS FAILURE"))
sys.exit(0 if ok_all else 1)
