"""Agnes Image 2.5 Flash —— 文生图 / 图生图 / 多图合成 Provider 插件

对接 Agnes 图像 API（官方文档 https://www.agnes-ai.cn/zh-Hans/docs/agnes-image-25-flash）：
    POST {base_url}/v1/images/generations    Authorization: Bearer {api_key}
    -> {"data": [{"url": ..., "b64_json": ..., "revised_prompt": ...}]}

官方文档的坑都已在代码里消化掉，改动时别动歪这几处：
- image（参考图数组）和 response_format **必须**嵌在 extra_body 里 ——
  放请求体顶层会被静默忽略（图生图悄悄退化成文生图）或直接 400
- 要 Base64 输出：文生图用顶层 return_base64: true，图生图/多图合成用
  extra_body.response_format: "b64_json"。本插件统一要 Base64 ——
  成品立即落进我们自己的存储，不依赖 Agnes 临时 URL 过不过期
- size 用档位（1K/2K/3K/4K）+ ratio（1:1/16:9/...）；ratio 在顶层，与 size 并列
- 图生图**不需要** tags: ["img2img"]，判断依据就是 extra_body.image 有没有
- 同步接口：一次 POST 直接返回成品，没有任务 ID / 轮询 / 取消端点 ——
  所以 submit 内同步等结果，返回本地任务号（agnes-xxx），get_status 首轮即 DONE

参考图的表单值不放 base64：表单值（task.params）会进任务列表每次轮询的响应体，
MB 级 base64 会拖垮前端。upload_input 落 filestore 存短标记 wbinput:xxx.png，
build_payload 时再还原成 Data URI —— 这是 ComfyUI「表单只存引擎侧短文件名」同款套路。

只 import 标准库 / httpx / app.*（插件加载器白名单内）。
"""
import base64
import mimetypes
import time
import uuid
from pathlib import Path

import httpx

from app.engines.base import (
    CapabilitySpec,
    FieldSpec,
    GenerationProvider,
    GenerationResult,
    TaskStatus,
)
from app.filestore import read_asset_bytes, save_asset_file

_MODEL = "agnes-image-2.5-flash"
_SIZES = ["1K", "2K", "3K", "4K"]
_RATIOS = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]


class AgnesProvider(GenerationProvider):
    # ── ① 类声明：管理页与模板配置读这些 ──────────────────
    name = "agnes"
    label = "Agnes 图像 (Image 2.5 Flash)"

    capabilities = {
        "text_to_image": CapabilitySpec(
            kind="text_to_image",
            label="文生图",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True,
                          description="推荐结构：主体 + 场景/环境 + 风格 + 光照 + 构图 + 质量要求"),
                FieldSpec(name="size", label="尺寸档位", type="enum", default="2K", options=_SIZES,
                          description="2.5 Flash 用档位式尺寸；1024x768 这类精确写法会被服务标准化"),
                FieldSpec(name="ratio", label="宽高比", type="enum", default="1:1", options=_RATIOS),
            ],
        ),
        "image_to_image": CapabilitySpec(
            kind="image_to_image",
            label="图生图（单参考图）",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True,
                          description="推荐结构：改变要求 + 新风格/场景 + 增删元素 + 需要保留的元素"),
                FieldSpec(name="image", label="参考图", type="image", required=True,
                          description="上传后自动转 Data URI 提交，表单里只存短标记"),
                FieldSpec(name="size", label="尺寸档位", type="enum", default="2K", options=_SIZES),
                FieldSpec(name="ratio", label="宽高比", type="enum", default="1:1", options=_RATIOS),
            ],
        ),
        "image_compose": CapabilitySpec(
            kind="image_compose",
            label="多图合成",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True,
                          description="说明每张参考图的角色和最终组合方式，如：第一张作主要角色，第二张作产品参考……"),
                FieldSpec(name="image", label="参考图", type="image", required=True, multiple=True,
                          description="可一次选多张，按选择顺序提交；表单里只存短标记"),
                FieldSpec(name="size", label="尺寸档位", type="enum", default="2K", options=_SIZES),
                FieldSpec(name="ratio", label="宽高比", type="enum", default="1:1", options=_RATIOS),
            ],
        ),
    }

    # 运行配置 —— 上传后界面渲染成配置表单，值存 DB 注入 self.config（机密不进代码）
    config_schema = [
        FieldSpec(name="base_url", label="API 地址", type="string",
                  default="https://api.agnes-ai.cn",
                  description="国内节点 https://api.agnes-ai.cn，带不带 /v1 都行（自动补）"),
        FieldSpec(name="api_key", label="API 密钥", type="string", required=True),
        FieldSpec(name="model", label="模型名", type="string", default=_MODEL),
    ]

    # 同步 API 的结果暂存。放**类**上而不是实例上：改配置/重传插件会重建实例，
    # 实例属性跟着蒸发，正在轮询的任务会平白报「结果丢失」。
    _results: dict = {}
    _output_files: dict = {}

    # ── ② 配置与认证 ────────────────────────────────────
    def _base(self) -> str:
        b = (self.config.get("base_url") or "").strip().rstrip("/")
        if b.endswith("/v1"):
            b = b[:-3].rstrip("/")
        return b

    def _endpoint(self) -> str:
        return f"{self._base()}/v1/images/generations"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.get('api_key', '')}",
        }

    # ── ③ 参考图：短标记 ↔ Data URI ─────────────────────
    @staticmethod
    def _sniff_ext(data: bytes) -> str:
        if data.startswith(b"\x89PNG"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"GIF8"):
            return ".gif"
        return ".png"

    @classmethod
    def _to_data_uri(cls, data: bytes, filename: str) -> str:
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    def _resolve_image(self, value) -> str:
        """表单里的参考图值 → API 能吃的 URL / Data URI。

        值的三种形态都认：wbinput: 短标记（upload_input 产出）、
        手填的公网 URL、手贴的 Data URI。
        """
        v = str(value or "").strip()
        if not v:
            return ""
        if v.startswith(("data:", "http://", "https://")):
            return v
        if v.startswith("wbinput:"):
            key = v[len("wbinput:"):]
            data = read_asset_bytes(key)
            if data is None:
                raise ValueError(f"参考图临时文件已丢失：{key}（重新上传一次即可）")
            return self._to_data_uri(data, key)
        raise ValueError(f"无法识别的参考图值：{v[:60]}")

    def _resolve_images(self, value) -> list[str]:
        """参考图字段可能是单个值，也可能是一组值（multiple 字段），统一成列表。"""
        raw = value if isinstance(value, (list, tuple)) else [value]
        out = []
        for v in raw:
            r = self._resolve_image(v)
            if r:
                out.append(r)
        return out

    async def upload_input(self, data: bytes, filename: str) -> str:
        """参考图进表单的值形态：落 filestore 存短标记，build_payload 时还原。

        不直接返回 Data URI —— 表单值会进任务列表每次轮询的响应体，
        MB 级 base64 会拖垮前端轮询。
        """
        ext = Path(filename or "").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            ext = self._sniff_ext(data)  # .jfif 等猜不出 mime 的兜底
        key = f"agnes-input/{uuid.uuid4().hex}{ext}"
        save_asset_file(key, data, mimetypes.guess_type(f"x{ext}")[0] or "image/png")
        return f"wbinput:{key}"

    # ── ④ 表单值 → 请求体（层级照官方文档钉死）──────────
    def build_payload(self, capability: str, params: dict) -> dict:
        payload = {
            "model": self.config.get("model") or _MODEL,
            "prompt": params.get("prompt", ""),
            "size": str(params.get("size") or "2K"),
        }
        ratio = str(params.get("ratio") or "").strip()
        if ratio:
            payload["ratio"] = ratio  # 顶层，与 size 并列（官方 2K/16:9 示例）

        images = self._resolve_images(params.get("image"))
        if capability in ("image_to_image", "image_compose") or images:
            if not images:
                raise ValueError("图生图 / 多图合成需要至少一张参考图")
            # image 和 response_format 必须嵌 extra_body —— 放顶层会被忽略或 400
            payload["extra_body"] = {"image": images, "response_format": "b64_json"}
        else:
            # 文生图要 Base64 用顶层 return_base64（不是 extra_body.response_format）
            payload["return_base64"] = True
        return payload

    @staticmethod
    def _redact(payload: dict) -> dict:
        """请求快照去脂：Data URI 动辄几 MB，进任务库/轮询响应会拖垮一切。"""
        snap = {k: v for k, v in payload.items() if k != "extra_body"}
        if "extra_body" in payload:
            extra = payload.get("extra_body") or {}
            snap["extra_body"] = {
                **{k: v for k, v in extra.items() if k != "image"},
                "image": [f"<data uri，{len(i)} 字符>" if i.startswith("data:") else i
                          for i in extra.get("image", [])],
            }
        return snap

    # ── ⑤ Provider 接口实现 ──────────────────────────────
    async def submit(self, ctx: dict, params: dict) -> str:
        """同步接口：请求发出去就直接拿到成品，没有引擎侧任务号可返回。

        所以在这里等完结果暂存内存，返回本地任务号（agnes- 前缀），
        get_status 首轮即 DONE —— 对上层任务链路完全透明。
        平台的超时计时从轮询才起算，这里的阻塞等待不占超时预算；
        等待期间用户取消，平台会在 submit 返回后补一刀 cancel（本接口无法中断，返回 False）。
        """
        capability = str(ctx.get("_capability") or "text_to_image")
        payload = self.build_payload(capability, params)
        self.last_request = self._redact(payload)  # 快照去脂（审计/重跑依据），别删

        async with httpx.AsyncClient(timeout=httpx.Timeout(360.0, connect=15.0)) as client:
            resp = await client.post(self._endpoint(), json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise ValueError(f"Agnes API {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError:
            raise ValueError(f"Agnes 返回的不是 JSON：{resp.text[:200]}")
        if not data.get("data"):
            raise ValueError(f"Agnes 响应缺少 data 字段：{str(data)[:200]}")

        task_id = f"agnes-{uuid.uuid4().hex[:12]}"
        self._results[task_id] = data
        return task_id

    async def get_status(self, task_id: str) -> GenerationResult:
        if not task_id.startswith("agnes-"):
            return GenerationResult(
                status=TaskStatus.ERROR,
                error=f"{task_id} 不是本插件的任务 ID（Agnes 是同步接口，没有引擎侧任务号）",
            )
        data = self._results.pop(task_id, None)
        if data is None:
            return GenerationResult(
                status=TaskStatus.ERROR,
                error="同步结果已丢失（后端重启后无法接回），请重新发起生成",
            )

        outputs = []
        for item in data.get("data") or []:
            b64 = item.get("b64_json")
            url = item.get("url")
            if b64:
                raw = base64.b64decode(b64)
                # 纯文件名 + 无 url：任务链路落产物时会按文件名找我们要字节（download_output）
                name = f"agnes_{int(time.time())}_{uuid.uuid4().hex[:6]}{self._sniff_ext(raw)}"
                self._output_files[name] = raw
                outputs.append({"filename": name, "subfolder": "", "type": "output"})
            elif url:
                # 万一 API 返回了 URL：filename 存纯文件名，完整 URL 放 url 字段，
                # 任务链路下载产物时优先用 url（临时 URL 不落盘会过期，会立即下载落 storage）
                name = url.rsplit("/", 1)[-1].split("?")[0] or f"agnes_{int(time.time())}.png"
                outputs.append({"url": url, "filename": name, "subfolder": "", "type": "output"})

        if not outputs:
            return GenerationResult(
                status=TaskStatus.ERROR,
                error="响应里既没有 url 也没有 b64_json",
                raw={"created": data.get("created")},
            )
        return GenerationResult(
            success=True,
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=outputs,
            # raw 只留小字段 —— 整包响应里可能带几 MB base64，不进任务库
            raw={"created": data.get("created"),
                 "revised_prompt": [it.get("revised_prompt") for it in data.get("data") or []]},
        )

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        raw = self._output_files.pop(filename, None)  # 一次性取走，不常驻内存
        if raw is not None:
            return raw
        if filename.startswith("data:"):
            return base64.b64decode(filename.partition(",")[2])
        if filename.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(filename)
                resp.raise_for_status()
                return resp.content
        raise ValueError(f"下载不到产物：{filename[:80]}（同步结果只暂存内存，重启后失效）")

    async def cancel(self, provider_task_id: str) -> bool:
        """Agnes 是一次 POST 直接出图的同步接口，没有任务号也没有取消端点 ——
        请求发出去只能等它返回。诚实返回 False（不代表出错）。"""
        return False

    async def health_check(self) -> bool:
        if not self._base() or not self.config.get("api_key"):
            return False
        try:
            # 没有专门的健康端点，借 GET /v1/models 探活：
            # 200 = 通；404/405 = 端点未公开但服务可达；401/超时/连不上 = 不健康
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{self._base()}/v1/models", headers=self._headers())
            return resp.status_code in (200, 404, 405)
        except Exception:  # noqa: BLE001
            return False
