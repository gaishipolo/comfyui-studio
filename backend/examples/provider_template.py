"""示例 Provider 模板 —— 对接一家假想的「示例云」REST 生成 API

照着这份改就是一家新供应商。约定见 docs/provider-dev-guide.md，要点：
- 一个文件一个 GenerationProvider 子类，上传后自动注册（注册名 = name 属性）
- 机密（api_key 等）写进 config_schema 由界面配置，**不要写死在代码里**
- 只允许 import 标准库 / httpx / app.engines.base（加载器有白名单）
- 值形态差异优先用 FieldSpec.options 枚举对齐（表单选出的值就是 API 原生值），
  复杂转换写进 build_payload

本示例假想的 API 形态（多数云厂商都是这个套路）：
  POST {base_url}/v1/generate      {"model": ..., "prompt": ..., "size": ...}  -> {"task_id": "..."}
  GET  {base_url}/v1/tasks/{id}    -> {"status": "queued|running|succeeded|failed",
                                       "progress": 0.6, "output": {"url": ...}, "error": ...}
"""
import httpx

from app.config import settings  # noqa: F401 —— 用到 settings 时放开；纯 config 驱动则删掉
from app.engines.base import (
    CapabilitySpec,
    FieldSpec,
    GenerationProvider,
    GenerationResult,
    TaskStatus,
)


class SampleCloudProvider(GenerationProvider):
    # ── ① 类声明：管理页与模板配置读这些 ──────────────────
    name = "sample_cloud"                      # 注册名（唯一；小写字母/数字/下划线/连字符）
    label = "示例云"                            # 展示名

    capabilities = {
        "text_to_image": CapabilitySpec(
            kind="text_to_image",
            label="文生图",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True),
                # 值形态差异用枚举消化：size 的选项值就是 API 原生值
                FieldSpec(name="size", label="尺寸", type="enum", default="1024*1024",
                          options=["512*512", "768*768", "1024*1024"]),
                FieldSpec(name="n", label="张数", type="int", default=1),
            ],
        ),
        "image_to_image": CapabilitySpec(
            kind="image_to_image",
            label="图生图",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True),
                FieldSpec(name="image", label="参考图", type="image", required=True),
                FieldSpec(name="strength", label="重绘幅度", type="float", default=0.7),
            ],
        ),
    }

    # 运行配置声明 —— 上传后界面渲染成配置表单，值存 DB 注入 self.config
    config_schema = [
        FieldSpec(name="base_url", label="API 地址", type="string", required=True,
                  description="如 https://api.sample-cloud.com"),
        FieldSpec(name="api_key", label="API 密钥", type="string", required=True),
        FieldSpec(name="model", label="模型名", type="string", default="sample-diffusion-v2"),
    ]

    # ── ② 认证：Bearer / JWT 签发 / HMAC 签名都是这里几行代码 ──
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.get('api_key', '')}",
        }

    def _base(self) -> str:
        return (self.config.get("base_url") or "").rstrip("/")

    # ── ③ 表单值 → 请求体（改名/拼接/嵌套都在这）────────────
    def build_payload(self, capability: str, params: dict) -> dict:
        payload = {
            "model": self.config.get("model", "sample-diffusion-v2"),
            "prompt": params.get("prompt", ""),
            "size": params.get("size", "1024*1024"),
        }
        if capability == "image_to_image":
            # 假设该 API 的图生图参数嵌在 input 下，还要求不同的 model 名
            return {
                "model": "sample-diffusion-v2-img",
                "input": {
                    "prompt": payload["prompt"],
                    "image_url": params.get("image", ""),   # 图片传法=url（值已是可访问 URL）
                    "strength": float(params.get("strength", 0.7)),
                },
            }
        payload["n"] = int(params.get("n", 1))
        return payload

    # ── ④ Provider 接口实现 ──────────────────────────────
    async def submit(self, ctx: dict, params: dict) -> str:
        capability = str(ctx.get("_capability") or "text_to_image")
        payload = self.build_payload(capability, params)
        self.last_request = payload          # 请求体快照（审计/重跑依据），别删
        async with httpx.AsyncClient(base_url=self._base(), timeout=60.0) as client:
            resp = await client.post("/v1/generate", json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise ValueError(f"API 响应缺少任务 ID: {str(data)[:200]}")
        return str(task_id)

    async def get_status(self, task_id: str) -> GenerationResult:
        async with httpx.AsyncClient(base_url=self._base(), timeout=30.0) as client:
            resp = await client.get(f"/v1/tasks/{task_id}", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        raw = str(data.get("status", "running")).lower()
        if raw in ("succeeded", "success", "done", "completed"):
            url = (data.get("output") or {}).get("url", "")
            outputs = []
            if url:
                # filename 存纯文件名，完整 URL 放 url 字段 ——
                # 任务链路下载产物时优先用 url（临时签名 URL 不落盘会过期，会自动下载落 storage）
                outputs.append({"url": url, "filename": url.rsplit("/", 1)[-1].split("?")[0],
                                "subfolder": "", "type": "output"})
            return GenerationResult(success=True, status=TaskStatus.DONE,
                                    progress=100.0, outputs=outputs, raw=data)
        if raw in ("failed", "error", "cancelled"):
            return GenerationResult(status=TaskStatus.ERROR,
                                    error=str(data.get("error") or data.get("message") or "生成失败"),
                                    raw=data)
        return GenerationResult(status=TaskStatus.RUNNING,
                                progress=float(data.get("progress", 0.5)) * 100, raw=data)

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        # filename 可能是完整 URL（任务链路优先传 url 字段）或相对路径
        if filename.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(filename)
                resp.raise_for_status()
                return resp.content
        async with httpx.AsyncClient(base_url=self._base(), timeout=120.0) as client:
            resp = await client.get(f"/v1/files/{filename}", headers=self._headers())
            resp.raise_for_status()
            return resp.content

    async def upload_input(self, data: bytes, filename: str) -> str:
        """图生图的图片传法：本示例 API 收 URL，就先传图换 URL。
        收 base64 的 API 更简单 —— 直接在 build_payload 里编码。"""
        async with httpx.AsyncClient(base_url=self._base(), timeout=60.0) as client:
            resp = await client.post(
                "/v1/files",
                files={"file": (filename, data)},
                headers={"Authorization": self._headers()["Authorization"]},
            )
            resp.raise_for_status()
            return str(resp.json().get("url", ""))

    async def cancel(self, provider_task_id: str) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self._base(), timeout=15.0) as client:
                resp = await client.post(f"/v1/tasks/{provider_task_id}/cancel",
                                         headers=self._headers())
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self._base(), timeout=5.0) as client:
                resp = await client.get("/v1/health", headers=self._headers())
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False
