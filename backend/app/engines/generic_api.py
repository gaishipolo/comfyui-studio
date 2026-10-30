"""通用 HTTP API 引擎 — 可配置对接任意云端生成 API（如通义万相、可灵等）

通过配置适配不同的云端 API 模式：
  1. POST 提交生成请求 → 返回任务 ID
  2. GET 轮询任务状态 → 返回状态 + 结果 URL
  3. GET 下载结果文件

配置项（环境变量 / .env）：
  GENERIC_API_BASE_URL   — API 基础地址
  GENERIC_API_KEY        — API 密钥
  GENERIC_API_SUBMIT_PATH   — 提交路径，默认 /generate
  GENERIC_API_STATUS_PATH   — 状态查询路径模板，默认 /tasks/{task_id}
  GENERIC_API_KEY_HEADER    — 密钥请求头名，默认 Authorization
  GENERIC_API_KEY_PREFIX    — 密钥前缀，默认 Bearer
"""
import httpx

from app.config import settings
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus


class GenericAPIProvider(GenerationProvider):
    """通用 HTTP 生成引擎 Provider"""

    name = "generic_api"
    label = "通用 HTTP API"

    def __init__(
        self,
        config: dict | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        submit_path: str | None = None,
        status_path: str | None = None,
        key_header: str | None = None,
        key_prefix: str | None = None,
    ):
        cfg = config or {}
        self.base_url = (
            base_url or cfg.get("base_url")
            or getattr(settings, "GENERIC_API_BASE_URL", "") or "http://127.0.0.1:9000"
        ).rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or getattr(settings, "GENERIC_API_KEY", "")
        self.submit_path = submit_path or cfg.get("submit_path") or getattr(settings, "GENERIC_API_SUBMIT_PATH", "/generate")
        self.status_path = status_path or cfg.get("status_path") or getattr(settings, "GENERIC_API_STATUS_PATH", "/tasks/{task_id}")
        self.key_header = key_header or cfg.get("key_header") or getattr(settings, "GENERIC_API_KEY_HEADER", "Authorization")
        self.key_prefix = (
            key_prefix if key_prefix is not None
            else cfg.get("key_prefix", getattr(settings, "GENERIC_API_KEY_PREFIX", "Bearer"))
        )

        self._client: httpx.AsyncClient | None = None
        self._results: dict[str, GenerationResult] = {}  # 本地缓存结果（兼容无状态 API）

    def _cache_result(self, task_id: str, result: GenerationResult):
        """缓存结果并裁剪，防止长期运行内存无限增长"""
        if len(self._results) > 200:
            self._results.pop(next(iter(self._results)))
        self._results[task_id] = result

    def _get_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers[self.key_header] = f"{self.key_prefix} {self.api_key}".strip()
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._get_headers(),
                timeout=httpx.Timeout(settings.COMFYUI_TIMEOUT, connect=10.0),
            )
        return self._client

    # ── Provider 接口实现 ──────────────────────────────────

    async def submit(self, ctx: dict, params: dict) -> str:
        """
        提交生成请求。
        params 是表单值，经 build_payload 转成请求体（默认透传）。
        请求体格式：{"input": <payload>, ...}
        """
        client = await self._get_client()
        payload = {
            "input": self.build_payload(str(ctx.get("_capability") or ctx.get("_type") or "generate"), params),
            "workflow_type": ctx.get("_type", "text_to_image"),
        }
        self.last_request = payload  # 请求体快照（审计/重跑依据）
        resp = await client.post(self.submit_path, json=payload)
        resp.raise_for_status()
        data = resp.json()

        # 兼容常见响应格式：{"task_id": ...} / {"id": ...} / {"data": {"task_id": ...}}
        task_id = (
            data.get("task_id")
            or data.get("id")
            or (data.get("data") or {}).get("task_id")
        )
        if not task_id:
            # 没有任务 ID 就无法轮询状态，直接报错而不是伪造 ID 等到超时
            raise ValueError(f"API 响应缺少任务 ID，无法跟踪生成状态：{str(data)[:200]}")
        self._cache_result(task_id, GenerationResult(status=TaskStatus.RUNNING, progress=10.0))
        return str(task_id)

    async def get_status(self, task_id: str) -> GenerationResult:
        """查询任务状态"""
        client = await self._get_client()
        path = self.status_path.format(task_id=task_id)

        try:
            resp = await client.get(path)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return self._results.get(task_id, GenerationResult(status=TaskStatus.RUNNING, progress=50.0))
            raise

        data = resp.json()

        # 兼容常见状态字段：status / state / task_status
        raw_status = str(
            data.get("status")
            or data.get("state")
            or data.get("task_status")
            or "running"
        ).lower()

        if raw_status in ("done", "completed", "success", "succeeded", "finished"):
            outputs = self._extract_outputs(data)
            result = GenerationResult(
                success=True,
                status=TaskStatus.DONE,
                progress=100.0,
                outputs=outputs,
                raw=data,
            )
        elif raw_status in ("failed", "error", "cancelled"):
            result = GenerationResult(
                status=TaskStatus.ERROR,
                error=str(data.get("error") or data.get("message") or "Generation failed"),
                raw=data,
            )
        else:
            progress = float(data.get("progress", 50.0))
            result = GenerationResult(status=TaskStatus.RUNNING, progress=progress, raw=data)

        self._cache_result(task_id, result)
        return result

    def _extract_outputs(self, data: dict) -> list[dict]:
        """从响应中提取输出文件信息，兼容多种格式"""
        outputs = []

        # 格式1：{"outputs": [{"url": ...}]} / {"images": [...]}
        for key in ("outputs", "images", "results", "data"):
            items = data.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str):
                        outputs.append({"url": item, "filename": item.rsplit("/", 1)[-1], "subfolder": "", "type": "output"})
                    elif isinstance(item, dict):
                        url = item.get("url") or item.get("image_url") or item.get("output_url")
                        if url:
                            outputs.append({
                                "url": url,
                                "filename": item.get("filename") or url.rsplit("/", 1)[-1].split("?")[0],
                                "subfolder": item.get("subfolder", ""),
                                "type": "output",
                            })
            elif isinstance(items, dict):
                # 格式2：{"data": {"url": ...}}
                url = items.get("url") or items.get("image_url")
                if url:
                    outputs.append({"url": url, "filename": url.rsplit("/", 1)[-1].split("?")[0], "subfolder": "", "type": "output"})

        return outputs

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        """下载结果文件。filename 可以是完整 URL 或相对路径。"""
        client = await self._get_client()

        # 如果 filename 是完整 URL，直接下载
        if filename.startswith("http://") or filename.startswith("https://"):
            async with httpx.AsyncClient(timeout=60.0) as dl_client:
                resp = await dl_client.get(filename)
                resp.raise_for_status()
                return resp.content

        # 否则按相对路径下载
        path = f"/files/{subfolder}/{filename}".replace("//", "/")
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.content

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            # 通用 API 不一定有 /health，尝试根路径
            try:
                client = await self._get_client()
                resp = await client.get("/", timeout=5.0)
                return resp.status_code < 500
            except Exception:
                return False

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
