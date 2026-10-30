"""ComfyUI 执行进度追踪 —— 通过 /ws 拿真实进度

**为什么不能只靠 REST**（这是之前进度卡在 50% 的根因，别再退回去）：
  - `/history/{prompt_id}`  只有跑完才有内容，跑完前恒为空
  - `/api/jobs/{id}`        只给 pending / in_progress / completed / failed，**没有步数**
  - `/queue`                只能区分「排队中」和「运行中」

真实的步数进度只在 WebSocket 上推（ComfyUI `main.py` 的 `hijack_progress`）：

    {"type": "progress", "data": {"value": v, "max": m, "prompt_id": p, "node": n}}

节点推进靠 `executing` 事件：{"type": "executing", "data": {"node": n, "prompt_id": p}}

**关键约束**：这些事件是按提交 `/prompt` 时带的 `client_id` 定向下发的
（`server.send_sync(..., server_instance.client_id)` → `self.sockets[sid]`）。
所以必须用**提交时同一个 client_id** 连 `/ws?clientId=<client_id>` 才收得到，
换一个 id 连上去是收不到自己任务的进度的。

`ComfyUIProvider` 是单例（factory.py 的 `_providers` 缓存）且 `client_id` 在
`__init__` 里定死，所以一条 WS 连接能服务所有任务。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

log = logging.getLogger("comfyui.progress")

# 内存里最多留多少个 prompt 的进度，超出按插入顺序淘汰，防止长跑泄漏
_MAX_TRACKED = 256


def ws_uri(base_url: str, client_id: str) -> str:
    """http(s)://host:port -> ws(s)://host:port/ws?clientId=..."""
    root = base_url.rstrip("/")
    if root.startswith("https://"):
        root = "wss://" + root[len("https://"):]
    elif root.startswith("http://"):
        root = "ws://" + root[len("http://"):]
    else:
        root = "ws://" + root
    return f"{root}/ws?clientId={urllib.parse.quote(client_id)}"


def reachable_node_count(workflow: dict) -> int:
    """算出真正会被执行的节点数（作为进度分母）。

    ComfyUI 只执行「出图节点的上游」，工作流里常有孤儿节点（注释、没接线的
    备用分支、SetNode 残留）。拿 `len(workflow)` 当分母会让进度条永远爬不满，
    只能停在 60% 然后一下跳到 100%。

    做法：先圈出「参与过连线」的节点，孤立节点（注释、没接线的备用分支）
    直接排除；再从中找出没被任何节点消费的「汇点」（通常是 SaveImage /
    VHS_VideoCombine 这类输出节点），沿连线反向遍历，得到的就是执行图。

    注意不能直接拿「没被消费的节点」当汇点——**孤立节点同样没被消费**，
    会被当成输出节点捞回来，等于白算。
    """
    if not isinstance(workflow, dict) or not workflow:
        return 0

    linked: set[str] = set()        # 参与过任意一条连线的节点（任一端都算）
    referenced: set[str] = set()    # 被下游消费的节点
    parents: dict[str, set[str]] = {}
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        for value in (node.get("inputs") or {}).values():
            # 连线写法是 [上游节点ID, 输出槽位]
            if isinstance(value, list) and len(value) == 2:
                src = str(value[0])
                if src in workflow:
                    linked.add(src)
                    linked.add(nid)
                    referenced.add(src)
                    parents.setdefault(nid, set()).add(src)

    if not linked:
        # 一条连线都没有（单节点工作流 / 全孤立）：没有拓扑可分析，用总数
        return len(workflow)

    sinks = [nid for nid in linked if nid not in referenced]
    if not sinks:  # 兜底：出现环，别猜，直接用连通部分的大小
        return len(linked)

    seen: set[str] = set()
    stack = list(sinks)
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(parents.get(nid, ()))

    return len(seen)


class ProgressTracker:
    """按 prompt_id 累计执行进度，进度单调不回退。"""

    def __init__(self, base_url: str, client_id: str):
        self._uri = ws_uri(base_url, client_id)
        self._task: asyncio.Task | None = None
        self._closed = False

        # prompt_id -> 已开始执行过的节点 id（只增，保证「节点推进」单调）
        self._nodes: dict[str, set[str]] = {}
        # prompt_id -> 当前节点的 (value, max)
        self._steps: dict[str, tuple[float, float]] = {}
        # prompt_id -> 执行图节点总数（分母）
        self._totals: dict[str, int] = {}
        # prompt_id -> 历史最高进度（硬保证单调，挡住事件乱序）
        self._best: dict[str, float] = {}
        # 已收到终态事件（execution_success / execution_error / node=null）的 prompt。
        # 单列一个集合而不是只看 _best==1.0：终态可能在任何节点观测之前就到，
        # 此时 _nodes 是空的，得靠它才不会被 ratio() 当成「没数据」。
        self._done: set[str] = set()

    # ── 对外 ────────────────────────────────────────────

    def record_plan(self, prompt_id: str, total_nodes: int) -> None:
        """提交成功后登记该 prompt 的执行图规模，作为进度分母。"""
        if not prompt_id:
            return
        self._trim()
        self._totals[prompt_id] = max(1, int(total_nodes or 0))
        self._best.setdefault(prompt_id, 0.0)

    def ratio(self, prompt_id: str) -> float | None:
        """返回 0..1 的执行进度；没有任何观测数据时返回 None（让调用方去降级）。

        单调性：`_best` 只升不降。哪怕事件乱序（上一个节点发了 max 的步数，
        新节点又从 value=0 开始），也不会看到进度条倒退。
        """
        if prompt_id in self._done:
            return 1.0

        nodes = self._nodes.get(prompt_id)
        if not nodes:
            return None

        seen = len(nodes)
        total = max(self._totals.get(prompt_id, 0), seen)
        if total <= 0:
            return None

        # 最后一个节点还在跑，所以是 seen-1 个「已完成」+ 当前节点内的部分
        done = max(0, seen - 1)
        value, mx = self._steps.get(prompt_id, (0.0, 1.0))
        frac = min(1.0, max(0.0, value / mx)) if mx > 0 else 0.0

        raw = (done + frac) / total
        raw = min(1.0, max(0.0, raw))

        best = max(self._best.get(prompt_id, 0.0), raw)
        self._best[prompt_id] = best
        return best

    def forget(self, prompt_id: str) -> None:
        """任务结束（无论成败）后清掉该 prompt 的进度数据。"""
        for store in (self._nodes, self._steps, self._totals, self._best):
            store.pop(prompt_id, None)
        self._done.discard(prompt_id)

    def tracked(self) -> int:
        return len(self._best)

    async def ensure_running(self) -> None:
        """惰性拉起后台 WS 监听。失败不影响主流程（get_status 会降级）。"""
        if self._closed or self._task is not None:
            return
        try:
            import websockets  # noqa: F401
        except ImportError:
            log.warning("websockets 未安装，无法获取实时进度，只能给粗粒度进度")
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ── WS 监听 ─────────────────────────────────────────

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        backoff = 1.0
        while not self._closed:
            try:
                async with connect(self._uri, max_size=8 * 1024 * 1024, open_timeout=10) as ws:
                    backoff = 1.0
                    log.info("ComfyUI 进度 WS 已连接 %s", self._uri)
                    async for raw in ws:
                        self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    return
                log.debug("ComfyUI 进度 WS 断开（%s），%.0fs 后重连", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _on_message(self, raw) -> None:
        # 二进制帧是预览图，直接丢，别 json.loads 浪费
        if isinstance(raw, (bytes, bytearray)):
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return

        data = msg.get("data")
        if not isinstance(data, dict):
            return
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return

        kind = msg.get("type")
        if kind == "executing":
            node = data.get("node")
            if node is None:
                # node 为 null 表示这个 prompt 的执行流结束
                self._done.add(prompt_id)
                self._best[prompt_id] = 1.0
                return
            self._trim()
            self._best.setdefault(prompt_id, 0.0)   # 以 _best 为主键集合，_trim 才管得住
            self._nodes.setdefault(prompt_id, set()).add(str(node))
            # 换节点了，节点内的步数从头算
            self._steps.pop(prompt_id, None)
        elif kind == "progress":
            value, mx = data.get("value"), data.get("max")
            if isinstance(value, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
                self._trim()
                self._best.setdefault(prompt_id, 0.0)
                self._steps[prompt_id] = (float(value), float(mx))
                self._nodes.setdefault(prompt_id, set()).add(str(data.get("node")))
        elif kind in ("execution_success", "execution_error"):
            self._done.add(prompt_id)
            self._best[prompt_id] = 1.0

    def _trim(self) -> None:
        """超量时淘汰最早登记的 prompt，避免长跑内存无限涨。

        用 `>=` 而不是 `>`：`record_plan` / `_on_message` 是「先 trim 再插入」，
        写 `>` 的话稳定容量会是 _MAX_TRACKED+1。
        """
        while len(self._best) >= _MAX_TRACKED:
            oldest = next(iter(self._best), None)
            if oldest is None:
                return
            self.forget(oldest)
