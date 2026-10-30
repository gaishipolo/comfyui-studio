"""Grsai 图像生成（GPT Image 2 / 2.5 系列）Provider 插件

对接 Grsai 图片生成 API（与 nano-banana 同一套端点，只是模型族不同）：
    POST {base_url}/v1/api/generate      Authorization: Bearer {api_key}
    GET  {base_url}/v1/api/result?id=..  （异步结果查询，同一套鉴权）
节点：全球 https://grsaiapi.com ｜ 国内 https://grsai.dakka.com.cn

文档要点（都已在代码里消化，改动时注意）：
- replyType 用 **async**：提交立即拿 id，get_status 走 /v1/api/result 轮询。
  进度实时、请求不挂住，**后端重启后也能接回继续轮询**（存的是远端 id）。
- 状态机 running / succeeded / failed / violation，progress 是 0~100 整数；
  失败也可能以 **HTTP 400** 返回，body 同样是 {id, status, error} —— 先看 status 再看状态码。
- **aspectRatio 的含义随模型变**（本插件的核心适配点）：
    gpt-image-2 / gpt-image-2.5  → 比例（"16:9"）或 1K 像素（"1024x1024"）
    gpt-image-2-vip / 2.5-flare / 2.5-sunburst → 只吃 1-4K 像素值，**不认比例**
  下拉框无法按模型联动，所以拆成两个字段：aspectRatio（比例）+ imageSize（像素），
  imageSize 填了就以它为准（后者覆盖前者）。
- quality 支持范围也随模型变：2/2.5 只支持 auto；vip 支持 medium；flare 支持 low/medium/high；
  sunburst 支持 low/medium/high/xhigh/max。选错由接口报错，description 里已标注。
- background=transparent 仅 vip / flare / sunburst 支持；mask 是公网图片 URL（局部重绘）。
- 没有取消端点，cancel 恒返回 False。

参考图（upload_input）不把 base64 塞进表单值：表单值会进任务列表每次轮询的响应体，
MB 级 base64 会拖垮前端。落 filestore 存短标记 wbinput:xxx.png，build_payload 时还原。

只 import 标准库 / httpx / app.*（插件加载器白名单内）。
"""
import base64
import mimetypes
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

_MODELS = [
    "gpt-image-2",
    "gpt-image-2-vip",
    "gpt-image-2.5",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
]
# 比例：仅 gpt-image-2 / gpt-image-2.5 认
_RATIOS = [
    "auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3",
    "5:4", "4:5", "21:9", "9:21", "1:2", "2:1",
]
# 像素：vip / flare / sunburst 认（取官方比例参考表里的代表值）。
# 首项留空 = 不指定 —— 下拉框若默认选中首项，空值不会误覆盖比例（build_payload 会跳过空值）
_PIXELS = [
    "", "1024x1024", "1536x1024", "1024x1536",
    "2048x2048", "2880x2880", "2048x1152", "1152x2048",
    "3840x2160", "2160x3840", "2304x1728", "1728x2304",
    "3504x2336", "2336x3504", "3840x1648", "1648x3840",
    "3840x1920", "1920x3840",
]
_QUALITIES = ["", "auto", "low", "medium", "high", "xhigh", "max"]

_RATIO_DESC = "仅 gpt-image-2 / gpt-image-2.5 认比例；vip / flare / sunburst 请改用「像素尺寸」"
_PIXEL_DESC = "vip / flare / sunburst 用；留空 = 不指定（用上面的比例），填了以它为准"
_QUALITY_DESC = ("留空 = 不指定；2/2.5 只支持 auto；vip 支持 medium；"
                 "flare 支持 low/medium/high；sunburst 支持 low/medium/high/xhigh/max")


class GrsaiGptImageProvider(GenerationProvider):
    # ── ① 类声明：管理页与模板配置读这些 ──────────────────
    name = "grsai_gpt_image"
    label = "Grsai 图像 (GPT Image 2 / 2.5)"

    capabilities = {
        "text_to_image": CapabilitySpec(
            kind="text_to_image",
            label="文生图",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True),
                FieldSpec(name="model", label="模型", type="enum", default="gpt-image-2",
                          options=_MODELS),
                FieldSpec(name="aspectRatio", label="比例", type="enum", default="auto",
                          options=_RATIOS, description=_RATIO_DESC),
                FieldSpec(name="imageSize", label="像素尺寸", type="enum", default="",
                          options=_PIXELS, description=_PIXEL_DESC),
                FieldSpec(name="quality", label="质量", type="enum", default="",
                          options=_QUALITIES, description=_QUALITY_DESC),
                FieldSpec(name="background", label="透明背景", type="string",
                          description="留空=不设置；填 transparent 需 vip / flare / sunburst 模型"),
            ],
        ),
        "image_to_image": CapabilitySpec(
            kind="image_to_image",
            label="图生图（单参考图）",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True),
                FieldSpec(name="image", label="参考图", type="image", required=True,
                          description="上传后自动转 base64 提交，表单里只存短标记"),
                FieldSpec(name="model", label="模型", type="enum", default="gpt-image-2",
                          options=_MODELS),
                FieldSpec(name="aspectRatio", label="比例", type="enum", default="auto",
                          options=_RATIOS, description=_RATIO_DESC),
                FieldSpec(name="imageSize", label="像素尺寸", type="enum", default="",
                          options=_PIXELS, description=_PIXEL_DESC),
                FieldSpec(name="quality", label="质量", type="enum", default="",
                          options=_QUALITIES, description=_QUALITY_DESC),
                FieldSpec(name="background", label="透明背景", type="string",
                          description="留空=不设置；填 transparent 需 vip / flare / sunburst 模型"),
                FieldSpec(name="mask", label="遮罩图 URL", type="string",
                          description="局部重绘用；须是公网可访问的图片 URL（不支持本地上传）"),
            ],
        ),
        "image_compose": CapabilitySpec(
            kind="image_compose",
            label="多图合成",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True,
                          description="说明每张参考图的角色与最终组合方式"),
                FieldSpec(name="image", label="参考图", type="image", required=True, multiple=True,
                          description="可一次选多张，按选择顺序提交；表单里只存短标记"),
                FieldSpec(name="model", label="模型", type="enum", default="gpt-image-2",
                          options=_MODELS),
                FieldSpec(name="aspectRatio", label="比例", type="enum", default="auto",
                          options=_RATIOS, description=_RATIO_DESC),
                FieldSpec(name="imageSize", label="像素尺寸", type="enum", default="",
                          options=_PIXELS, description=_PIXEL_DESC),
                FieldSpec(name="quality", label="质量", type="enum", default="",
                          options=_QUALITIES, description=_QUALITY_DESC),
            ],
        ),
    }

    config_schema = [
        FieldSpec(name="base_url", label="API 地址", type="string",
                  default="https://grsai.dakka.com.cn",
                  description="国内节点 https://grsai.dakka.com.cn ｜ 全球 https://grsaiapi.com（带不带 /v1 都行）"),
        FieldSpec(name="api_key", label="API 密钥", type="string", required=True,
                  description="sk- 开头，控制台 https://grsai.ai/zh/dashboard/api-keys 获取"),
        FieldSpec(name="image_format", label="参考图编码", type="enum", default="data_uri",
                  options=["data_uri", "raw_base64"],
                  description="data_uri = data:image/png;base64,...；接口报图片解析失败就切 raw_base64"),
    ]

    # ── ② 配置与认证 ────────────────────────────────────
    def _base(self) -> str:
        b = (self.config.get("base_url") or "").strip().rstrip("/")
        if b.endswith("/v1"):
            b = b[:-3].rstrip("/")
        return b

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.get('api_key', '')}",
        }

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            raise ValueError(f"Grsai 返回的不是 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")

    # ── ③ 参考图：短标记 ↔ base64 ───────────────────────
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

    def _encode_image(self, data: bytes, filename: str) -> str:
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        b64 = base64.b64encode(data).decode()
        if (self.config.get("image_format") or "data_uri") == "raw_base64":
            return b64
        return f"data:{mime};base64,{b64}"

    def _resolve_image(self, value) -> str:
        """表单里的参考图值 → API 能吃的 URL / base64。

        认三种形态：wbinput: 短标记（upload_input 产出）、手填公网 URL、手贴 base64/Data URI。
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
            return self._encode_image(data, key)
        if len(v) > 512:  # 手贴的裸 base64
            return v
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
        """参考图进表单的值形态：落 filestore 存短标记，build_payload 时还原成 base64。"""
        ext = Path(filename or "").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            ext = self._sniff_ext(data)  # .jfif 等猜不出 mime 的兜底
        key = f"grsai-gpt-input/{uuid.uuid4().hex}{ext}"
        save_asset_file(key, data, mimetypes.guess_type(f"x{ext}")[0] or "image/png")
        return f"wbinput:{key}"

    # ── ④ 表单值 → 请求体 ────────────────────────────────
    def build_payload(self, capability: str, params: dict) -> dict:
        images = self._resolve_images(params.get("image"))
        if capability in ("image_to_image", "image_compose") and not images:
            raise ValueError("图生图 / 多图合成需要至少一张参考图")

        payload = {
            "model": params.get("model") or "gpt-image-2",
            "prompt": params.get("prompt", ""),
            "images": images,  # 文生图传空数组（官方示例也是 images: []）
            "replyType": "async",  # 返回 id 后由 get_status 轮询 /v1/api/result
        }

        # 像素尺寸优先：vip 系列只认像素，2/2.5 两者都认
        size = str(params.get("imageSize") or "").strip()
        ratio = str(params.get("aspectRatio") or "").strip()
        if size:
            payload["aspectRatio"] = size
        elif ratio:
            payload["aspectRatio"] = ratio

        quality = str(params.get("quality") or "").strip()
        if quality:
            payload["quality"] = quality
        background = str(params.get("background") or "").strip()
        if background:
            payload["background"] = background
        mask = str(params.get("mask") or "").strip()
        if mask:
            payload["mask"] = mask
        return payload

    @staticmethod
    def _redact(payload: dict) -> dict:
        """请求快照去脂：base64 动辄几 MB，进任务库/轮询响应会拖垮一切。"""
        snap = dict(payload)
        snap["images"] = [i if i.startswith(("http://", "https://")) else f"<base64，{len(i)} 字符>"
                          for i in payload.get("images", [])]
        return snap

    # ── ⑤ Provider 接口实现 ──────────────────────────────
    async def submit(self, ctx: dict, params: dict) -> str:
        capability = str(ctx.get("_capability") or "text_to_image")
        payload = self.build_payload(capability, params)
        self.last_request = self._redact(payload)  # 快照去脂（审计/重跑依据），别删

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            resp = await client.post(f"{self._base()}/v1/api/generate",
                                     json=payload, headers=self._headers())
        data = self._json(resp)
        if resp.status_code >= 400:
            raise ValueError(f"Grsai API {resp.status_code}: {data.get('error') or str(data)[:200]}")

        task_id = data.get("id")
        if not task_id:
            raise ValueError(f"Grsai 响应缺少任务 ID：{str(data)[:200]}")
        return str(task_id)

    async def get_status(self, task_id: str) -> GenerationResult:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.get(f"{self._base()}/v1/api/result",
                                    params={"id": task_id}, headers=self._headers())
        data = self._json(resp)
        # 失败/违规也可能以 400 返回，body 同样是 {id, status, error} —— 先看 status 再看状态码
        status = str(data.get("status") or "").lower()

        if status in ("failed", "violation"):
            err = str(data.get("error") or ("内容违规" if status == "violation" else "任务失败"))
            return GenerationResult(status=TaskStatus.ERROR, error=err, raw={"status": status})

        if resp.status_code >= 400:
            return GenerationResult(status=TaskStatus.ERROR,
                                    error=f"Grsai API {resp.status_code}: {data.get('error') or str(data)[:200]}")

        if status == "succeeded":
            outputs = []
            for item in data.get("results") or []:
                url = (item or {}).get("url") or ""
                if not url:
                    continue
                # filename 存纯文件名，完整 URL 放 url 字段：任务链路会立即下载落我们自己的存储
                name = url.rsplit("/", 1)[-1].split("?")[0] or f"grsai_{task_id}.png"
                outputs.append({"url": url, "filename": name, "subfolder": "", "type": "output"})
            if not outputs:
                return GenerationResult(status=TaskStatus.ERROR,
                                        error="任务成功但没有返回结果图片", raw={"status": status})
            return GenerationResult(success=True, status=TaskStatus.DONE, progress=100.0,
                                    outputs=outputs, raw={"status": status})

        try:
            progress = float(data.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        return GenerationResult(status=TaskStatus.RUNNING, progress=progress,
                                raw={"status": status or "running"})

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        if filename.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(filename)
                resp.raise_for_status()
                return resp.content
        if filename.startswith("data:"):
            return base64.b64decode(filename.partition(",")[2])
        raise ValueError(f"下载不到产物：{filename[:80]}")

    async def cancel(self, provider_task_id: str) -> bool:
        """Grsai 文档没有取消端点，任务一旦提交只能等它结束。诚实返回 False（不代表出错）。"""
        return False

    async def health_check(self) -> bool:
        if not self._base() or not self.config.get("api_key"):
            return False
        try:
            # 没有专门的健康端点，拿结果查询接口探活（无效 id 会返回 400/404，但说明服务可达）：
            # 200/400/404/422 = 通；401/403 = 密钥无效；连不上 = 不健康
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{self._base()}/v1/api/result",
                                        params={"id": "health-probe"}, headers=self._headers())
            return resp.status_code in (200, 400, 404, 422)
        except Exception:  # noqa: BLE001
            return False
