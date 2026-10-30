"""ComfyUI Provider — 对接本地/远程 ComfyUI API"""
import json
import uuid
import httpx

from app.config import settings
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus
from app.engines.comfyui_progress import ProgressTracker, reachable_node_count


class ComfyUIProvider(GenerationProvider):

    name = "comfyui"
    label = "ComfyUI"

    def __init__(self, config: dict | None = None, base_url: str | None = None):
        cfg = config or {}
        self.base_url = (base_url or cfg.get("base_url") or settings.COMFYUI_BASE_URL).rstrip("/")
        self.client_id = uuid.uuid4().hex
        self._client: httpx.AsyncClient | None = None
        # 实时进度：靠 /ws 推送，见 comfyui_progress 模块注释
        self._progress = ProgressTracker(self.base_url, self.client_id)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(settings.COMFYUI_TIMEOUT, connect=10.0),
            )
        return self._client

    # ── 工具：把用户参数注入 ComfyUI workflow JSON ──────────

    @staticmethod
    def _coerce_like(reference, value):
        """把用户传入的值转成工作流里原有值的类型。

        ComfyUI 的 /prompt 校验本身也会按节点声明的类型强转
        （execution.py validate_inputs：INT→int()、FLOAT→float()、
        STRING→str()、BOOLEAN→bool()），所以不做转换也不会让 kernel 报错——
        这点已核实过，别再把 adaln 之类的 CUDA 报错往参数类型上归因。

        但 Python 的 bool("false") 是 True、bool("") 是 False，
        字符串布尔透传过去会静默得到**反义值**，而且很难查。
        这里以工作流原值的类型为准对齐一次，顺带让落库/日志里的
        参数类型与真正提交的保持一致。

        转不动时原样返回，交给 ComfyUI 报 invalid_input_type——
        宁可入队时就 400 报清楚，也不要静默改成错的值。
        """
        if isinstance(reference, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() not in ("", "0", "false", "no", "off", "none", "null")
            return bool(value)
        if isinstance(reference, int):  # bool 是 int 子类，须排在上面
            try:
                return int(value)
            except (TypeError, ValueError):
                try:
                    return int(float(value))   # "480.0" 这种
                except (TypeError, ValueError):
                    return value
        if isinstance(reference, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if isinstance(reference, str):
            return value if isinstance(value, str) else str(value)
        return value

    @staticmethod
    def inject_params(workflow: dict, param_mapping: dict, params: dict) -> dict:
        """
        按 param_mapping 把用户输入写入 workflow 节点。

        param_mapping 格式：{"prompt": "76.inputs.value", "width": "103.inputs.value"}
        含义：用户填的 prompt 字段 -> 写入节点 76 的 inputs.value

        映射配置错误（节点不存在 / 路径非法）会抛出 ValueError，
        避免"任务成功但参数没生效"的静默失败。

        值会按工作流里原有值的类型对齐（见 _coerce_like）——
        前端 select/number 控件给的是字符串，直接写进去类型就漂了。
        """
        wf = json.loads(json.dumps(workflow))  # deep copy
        errors: list[str] = []
        for field_name, node_path in param_mapping.items():
            if field_name not in params:
                continue
            if not node_path or "." not in node_path:
                errors.append(f"字段「{field_name}」的映射路径非法: {node_path!r}（应为 节点ID.inputs.参数名）")
                continue
            value = params[field_name]
            parts = node_path.split(".")
            # 例如 "76.inputs.value" -> node_id="76", keys=["inputs", "value"]
            node_id = parts[0]
            keys = parts[1:]
            target = wf.get(node_id)
            if target is None:
                errors.append(f"字段「{field_name}」映射的节点 {node_id} 在工作流中不存在")
                continue
            for k in keys[:-1]:
                target = target.setdefault(k, {})
            # 以工作流里原有值的类型为准对齐，避免字符串数值/布尔漂移
            target[keys[-1]] = ComfyUIProvider._coerce_like(target.get(keys[-1]), value)
        if errors:
            raise ValueError("参数映射配置错误：" + "；".join(errors))
        return wf

    # ── Provider 接口实现 ──────────────────────────────────

    async def submit(self, ctx: dict, params: dict) -> str:
        """提交工作流到 ComfyUI /prompt，返回 prompt_id

        ctx._workflow 是工作流 JSON，ctx._param_mapping 是表单字段→节点路径映射；
        参数注入在这一步做（原先在 task_service 里 isinstance 特判，现收进类内）。
        """
        workflow = ctx.get("_workflow") or {}
        if not workflow:
            raise ValueError(
                f"ComfyUI engine requires a workflow file (template: {ctx.get('_template_name', '?')})"
            )
        workflow = self.inject_params(workflow, ctx.get("_param_mapping") or {}, params)
        client = await self._get_client()
        # 先把 WS 监听拉起来，否则跑得快的任务一条进度事件都收不到
        await self._progress.ensure_running()
        payload = {
            "prompt": workflow,
            "client_id": self.client_id,
        }
        resp = await client.post("/prompt", json=payload)
        resp.raise_for_status()
        data = resp.json()
        prompt_id = data["prompt_id"]
        # 登记执行图规模，进度条的分母。要算「可达节点数」而不是 len(workflow)：
        # 工作流里常有孤儿节点，拿总数当分母进度会永远爬不满、卡在某个百分比。
        self._progress.record_plan(prompt_id, reachable_node_count(workflow))
        return prompt_id

    async def cancel(self, provider_task_id: str) -> bool:
        """取消 ComfyUI 侧的 prompt，覆盖「排队中」和「运行中」两种情况。

        首选 `POST /api/jobs/cancel`（ComfyUI 较新版）：一个请求同时管住
        pending 和 running，且带 UUID 校验。实测 0.40.0 返回 `{"cancelled": bool}`。

        老版本没有 /api/jobs，退回两条腿：
          - 还在 `queue_pending` → `POST /queue {"delete": [prompt_id]}` 只撤这一条，
            不碰别人排队的任务（`{"clear": true}` 会清空整个队列，绝不能用）
          - 已在 `queue_running` → `POST /interrupt`。老版本的 /interrupt 不看 body、
            是**全局**中断，所以这里必须先用 /queue 确认「跑着的确实是我们这条」
            再发，否则可能误杀别的任务。

        任务早已跑完时两边都不会命中，返回 False —— 这是正常结果，不是错误。
        """
        if not provider_task_id:
            return False
        client = await self._get_client()

        # ── 首选：新版批量取消接口 ────────────────────────
        try:
            resp = await client.post("/api/jobs/cancel", json={"job_ids": [provider_task_id]})
        except httpx.HTTPError:
            resp = None
        if resp is not None:
            if resp.status_code == 200:
                try:
                    return bool(resp.json().get("cancelled"))
                except ValueError:  # 200 但不是 JSON，别让解析异常冒泡到任务上
                    return False
            if resp.status_code not in (404, 405):
                # 400 之类说明服务端认得这个接口但不接受我们的入参，没必要再降级
                return False
            # 404/405 = 这版没有 /api/jobs，走下面的降级路径

        # ── 降级：/queue 定向删除 + 定向中断 ────────────────
        try:
            q = await client.get("/queue")
            q.raise_for_status()
            data = q.json() or {}
        except (httpx.HTTPError, ValueError):
            return False

        def _ids(key: str) -> list[str]:
            items = data.get(key) or []
            # 队列项元组形如 (number, prompt_id, prompt, extra_data, outputs)
            return [str(it[1]) for it in items if isinstance(it, (list, tuple)) and len(it) > 1]

        cancelled = False
        if provider_task_id in _ids("queue_pending"):
            try:
                r = await client.post("/queue", json={"delete": [provider_task_id]})
                cancelled = r.status_code == 200 or cancelled
            except httpx.HTTPError:
                pass
        if provider_task_id in _ids("queue_running"):
            try:
                # 带 prompt_id 是新版的定向中断；老版忽略 body 做全局中断，
                # 但上面已经确认它就是正在跑的那条，全局中断也只会打断它。
                r = await client.post("/interrupt", json={"prompt_id": provider_task_id})
                cancelled = r.status_code == 200 or cancelled
            except httpx.HTTPError:
                pass
        return cancelled

    async def get_status(self, task_id: str) -> GenerationResult:
        """查询 ComfyUI /history/{prompt_id} 获取执行结果"""
        client = await self._get_client()
        resp = await client.get(f"/history/{task_id}")
        resp.raise_for_status()
        history = resp.json()

        if task_id not in history:
            # 还没跑完 —— 给真实进度，不是写死的常数
            return GenerationResult(
                status=TaskStatus.RUNNING,
                progress=await self._running_progress(client, task_id),
            )

        entry = history[task_id]
        status_info = entry.get("status", {})
        status_str = status_info.get("status_str", "")

        if status_str == "error":
            messages = status_info.get("messages", [])
            error_parts = []
            for msg in messages:
                if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
                    error_parts.append(msg[1].get("exception_message", "Unknown error"))
            self._progress.forget(task_id)   # 终态，进度数据不再需要
            return GenerationResult(
                status=TaskStatus.ERROR,
                error="; ".join(error_parts) or "ComfyUI execution error",
                raw=entry,
            )

        # 收集输出文件
        outputs = self._collect_outputs(entry)

        self._progress.forget(task_id)   # 终态，进度数据不再需要
        return GenerationResult(
            success=True,
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=outputs,
            raw=entry,
        )

    @staticmethod
    def _collect_outputs(entry: dict) -> list[dict]:
        """从 /history 条目里收集产出文件。

        ComfyUI 各节点把产出挂在**不同的 ui 键**上，没有统一字段：
          SaveImage / PreviewImage → "images"
          VHS_VideoCombine        → "gifs"（内容其实是 mp4/webm，不是 gif）
          其它节点                → "video" / "audio" / "files" …
        之前只读 "images"，视频任务跑完后 outputs 恒为空 —— 前端拿不到视频。
        这里不写死键名，而是扫所有「文件列表」：列表项是 dict 且带 filename 的
        就算一个产出文件。text 之类的列表项（纯字符串）自然被跳过。
        """
        outputs = []
        seen = set()
        for node_id, node_output in (entry.get("outputs") or {}).items():
            if not isinstance(node_output, dict):
                continue
            for items in node_output.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("filename")
                    if not filename:
                        continue
                    subfolder = item.get("subfolder", "")
                    out_type = item.get("type", "output")
                    # 同一个文件可能被多个 ui 键重复报出来
                    key = (str(filename), str(subfolder), str(out_type))
                    if key in seen:
                        continue
                    seen.add(key)
                    outputs.append({
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": out_type,
                        "node_id": node_id,
                    })
        return outputs

    async def _running_progress(self, client: httpx.AsyncClient, prompt_id: str) -> float:
        """任务还没跑完时的进度百分比（0-100）。

        优先用 /ws 推上来的真实进度；WS 没数据时退回 /queue 区分
        「还在排队」和「已经开跑」。

        这里**绝不能写死一个百分比**：之前写死 50.0，导致进度条第一次刷新
        就显示 50%、之后纹丝不动、跑完直接跳 100%。

        输出区间压在 3..97 —— 100% 只在 /history 确认完成后才出现，
        免得「看着跑完了其实还在存文件」。
        """
        await self._progress.ensure_running()
        ratio = self._progress.ratio(prompt_id)
        if ratio is None:
            ratio = 0.35 if await self._is_running(client, prompt_id) else 0.02
        return round(3.0 + 94.0 * ratio, 1)

    @staticmethod
    async def _is_running(client: httpx.AsyncClient, prompt_id: str) -> bool:
        """查 /queue：prompt 是否已经离开队列、正在执行。"""
        try:
            resp = await client.get("/queue")
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception:
            return False
        for item in data.get("queue_running") or []:
            # 队列项形如 [number, prompt_id, prompt, extra_data]
            if len(item) > 1 and str(item[1]) == prompt_id:
                return True
        return False

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        """下载生成的图片 / 文件"""
        client = await self._get_client()
        resp = await client.get("/view", params={
            "filename": filename,
            "subfolder": subfolder,
            "type": output_type,
        })
        resp.raise_for_status()
        return resp.content

    async def upload_input(self, data: bytes, filename: str) -> str:
        """上传输入图片到 ComfyUI（/upload/image），返回服务器上的文件名"""
        client = await self._get_client()
        resp = await client.post(
            "/upload/image",
            files={"image": (filename, data, "application/octet-stream")},
            data={"overwrite": "true"},
        )
        resp.raise_for_status()
        info = resp.json()
        return info.get("name") or filename

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get("/system_stats", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self):
        await self._progress.close()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
