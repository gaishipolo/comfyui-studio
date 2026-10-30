"""Grsai 视频生成（MiniMax H3）Provider 插件

对接 Grsai 视频生成 API（与 nano-banana / gpt-image 同一套端点，只是产物是视频）：
    POST {base_url}/v1/api/generate      Authorization: Bearer {api_key}
    GET  {base_url}/v1/api/result?id=..  （异步结果查询，同一套鉴权）
节点：全球 https://grsaiapi.com ｜ 国内 https://grsai.dakka.com.cn

文档要点（都已在代码里消化，改动时注意）：
- replyType 用 **async**：提交立即拿 id，get_status 走 /v1/api/result 轮询。
  视频动辄几分钟，真轮询比同步阻塞稳得多，**后端重启后也能接回继续轮询**。
- 状态机 running / succeeded / failed / violation，progress 0~100；
  失败也可能以 **HTTP 400** 返回，body 同样是 {id, status, error} —— 先看 status 再看状态码。
- 必填项比图片接口多：prompt / aspectRatio / resolution / model / duration / replyType。
- aspectRatio 只有两个值：portrait（竖屏）/ landscape（横屏），**不是** 16:9 这种写法。
- resolution 480p / 768p / 1080p；duration 1~15 秒，**1080p 最多 10 秒**（超了直接报错，不静默改）。
- images 最多 9 张、audios 最多 3 段，都支持 base64 与公网 URL。
  参考图和参考音频都走平台上传（/api/upload/file?kind=image|audio），表单只存短标记；
  也允许手填公网 URL / base64 直连。
- promptSkill 默认 true（服务端会先优化提示词）；模型对提示词要求较高，建议保留。
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

_MODEL = "minimax-h3"
_RESOLUTIONS = ["480p", "768p", "1080p"]
_RATIOS = ["landscape", "portrait"]
_MAX_DURATION = 15
_MAX_DURATION_1080P = 10
_MAX_IMAGES = 9
_MAX_AUDIOS = 3
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class GrsaiMinimaxH3Provider(GenerationProvider):
    # ── ① 类声明：管理页与模板配置读这些 ──────────────────
    name = "grsai_minimax_h3"
    label = "Grsai 视频 (MiniMax H3)"

    capabilities = {
        "text_to_video": CapabilitySpec(
            kind="text_to_video",
            label="文生视频",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True,
                          description="该模型对提示词要求较高：建议写清镜头、时长分段、对白、声音设计、视觉要求"),
                FieldSpec(name="aspectRatio", label="画幅", type="enum", default="landscape",
                          options=_RATIOS, description="landscape 横屏 / portrait 竖屏"),
                FieldSpec(name="resolution", label="分辨率", type="enum", default="480p",
                          options=_RESOLUTIONS, description="1080p 时视频时长最多 10 秒"),
                FieldSpec(name="duration", label="时长（秒）", type="int", default=5,
                          description="1~15 秒；resolution=1080p 时最多 10 秒"),
                FieldSpec(name="seed", label="随机种子", type="int",
                          description="可选；固定种子便于复现同一结果"),
                FieldSpec(name="promptSkill", label="提示词优化", type="bool", default=True,
                          description="服务端先优化提示词；关闭则完全按原文生成"),
                FieldSpec(name="audios", label="参考音频", type="audio", multiple=True,
                          description=f"可一次选多个（接口上限 {_MAX_AUDIOS} 段）；也可填公网 URL / base64"),
            ],
        ),
        "image_to_video": CapabilitySpec(
            kind="image_to_video",
            label="图生视频",
            fields=[
                FieldSpec(name="prompt", label="提示词", type="string", required=True,
                          description="描述画面运动、镜头与声音；参考图最多 9 张"),
                FieldSpec(name="image", label="参考图", type="image", required=True, multiple=True,
                          description="可一次选多张（接口上限 9 张），按选择顺序提交；表单里只存短标记"),
                FieldSpec(name="aspectRatio", label="画幅", type="enum", default="landscape",
                          options=_RATIOS, description="landscape 横屏 / portrait 竖屏"),
                FieldSpec(name="resolution", label="分辨率", type="enum", default="480p",
                          options=_RESOLUTIONS, description="1080p 时视频时长最多 10 秒"),
                FieldSpec(name="duration", label="时长（秒）", type="int", default=5,
                          description="1~15 秒；resolution=1080p 时最多 10 秒"),
                FieldSpec(name="seed", label="随机种子", type="int",
                          description="可选；固定种子便于复现同一结果"),
                FieldSpec(name="promptSkill", label="提示词优化", type="bool", default=True,
                          description="服务端先优化提示词；关闭则完全按原文生成"),
                FieldSpec(name="audios", label="参考音频", type="audio", multiple=True,
                          description=f"可一次选多个（接口上限 {_MAX_AUDIOS} 段）；也可填公网 URL / base64"),
            ],
        ),
    }

    config_schema = [
        FieldSpec(name="base_url", label="API 地址", type="string",
                  default="https://grsai.dakka.com.cn",
                  description="国内节点 https://grsai.dakka.com.cn ｜ 全球 https://grsaiapi.com（带不带 /v1 都行）"),
        FieldSpec(name="api_key", label="API 密钥", type="string", required=True,
                  description="sk- 开头，控制台 https://grsai.ai/zh/dashboard/api-keys 获取"),
        FieldSpec(name="image_format", label="参考文件编码", type="enum", default="data_uri",
                  options=["data_uri", "raw_base64"],
                  description="参考图/音频的内联方式：data_uri = data:image/png;base64,...；接口报解析失败就切 raw_base64"),
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

    # ── ③ 参考图 / 参考音频：短标记 ↔ base64 ─────────────
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

    def _encode_audio(self, data: bytes, key: str) -> str:
        """音频内联：与参考图同一套编码开关（data_uri / raw_base64）。"""
        mime = mimetypes.guess_type(key)[0] or "audio/mpeg"
        b64 = base64.b64encode(data).decode()
        if (self.config.get("image_format") or "data_uri") == "raw_base64":
            return b64
        return f"data:{mime};base64,{b64}"

    def _resolve_audio(self, value) -> str:
        """单个参考音频 → API 能吃的 URL / base64 / data URI。

        认三种形态：wbinput: 短标记（本平台上传产出）、手填公网 URL、手贴 base64。
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
                raise ValueError(f"参考音频临时文件已丢失：{key}（重新上传一次即可）")
            return self._encode_audio(data, key)
        if len(v) > 512:  # 手贴的裸 base64
            return v
        raise ValueError(f"无法识别的参考音频值：{v[:60]}（上传音频，或填公网 URL / base64）")

    def _resolve_audios(self, value) -> list[str]:
        """参考音频字段可能是单个值，也可能是一组值（multiple 字段），统一成列表。"""
        raw = value if isinstance(value, (list, tuple)) else [value]
        out = []
        for v in raw:
            r = self._resolve_audio(v)
            if r:
                out.append(r)
        return out

    @staticmethod
    def _to_bool(value) -> bool:
        """表单值可能是 bool、字符串 "true"/"false"、或 1/0 —— 统一成 bool。

        不能直接 bool(value)：bool("false") 是 True，会把「关闭」悄悄变成「开启」。
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on", "是"):
            return True
        if s in ("0", "false", "no", "off", "否"):
            return False
        raise ValueError(f"提示词优化只接受 true/false，收到：{value}")

    async def upload_input(self, data: bytes, filename: str) -> str:
        """参考文件进表单的值形态：落 filestore 存短标记，build_payload 时还原成 base64。

        图片和音频共用这条路（平台上传接口按 kind 放行），按扩展名分目录便于排查。
        """
        ext = Path(filename or "").suffix.lower()
        if ext in _AUDIO_EXT:
            folder, fallback_mime = "grsai-audio-input", "audio/mpeg"
        else:
            if ext not in _IMAGE_EXT:
                ext = self._sniff_ext(data)  # .jfif 等猜不出 mime 的兜底
            folder, fallback_mime = "grsai-video-input", "image/png"
        key = f"{folder}/{uuid.uuid4().hex}{ext}"
        save_asset_file(key, data, mimetypes.guess_type(f"x{ext}")[0] or fallback_mime)
        return f"wbinput:{key}"

    # ── ④ 表单值 → 请求体 ────────────────────────────────
    def build_payload(self, capability: str, params: dict) -> dict:
        images = self._resolve_images(params.get("image"))
        if capability == "image_to_video" and not images:
            raise ValueError("图生视频需要至少一张参考图")
        # 新字段 audios 是列表；老模板里还存着 audio/audio2/audio3 三个槽位，
        # 这里一并收下 —— 存量模板不报错，管理员点「对齐到当前能力」后自然只剩 audios
        audios = self._resolve_audios(params.get("audios"))
        audios += self._resolve_audios([params.get(k) for k in ("audio", "audio2", "audio3")])
        if len(audios) > _MAX_AUDIOS:
            raise ValueError(f"参考音频最多 {_MAX_AUDIOS} 段，收到 {len(audios)} 段")
        if len(images) > _MAX_IMAGES:
            raise ValueError(f"参考图最多 {_MAX_IMAGES} 张，收到 {len(images)} 张")

        resolution = str(params.get("resolution") or "480p")
        # 注意不能用 `params.get("duration") or 5` —— 0 是假值，会被悄悄改成 5 秒。
        # 只有真正没填（None / 空串）才走默认，填了 0 就该报错
        raw_duration = params.get("duration")
        try:
            duration = 5 if raw_duration in (None, "") else int(raw_duration)
        except (TypeError, ValueError):
            raise ValueError(f"时长必须是整数秒：{raw_duration}")
        if not 1 <= duration <= _MAX_DURATION:
            raise ValueError(f"时长需在 1~{_MAX_DURATION} 秒之间，收到 {duration}")
        if resolution == "1080p" and duration > _MAX_DURATION_1080P:
            raise ValueError(f"1080p 最多支持 {_MAX_DURATION_1080P} 秒，收到 {duration} 秒（调低时长或换分辨率）")

        payload = {
            "model": _MODEL,
            "prompt": params.get("prompt", ""),
            "aspectRatio": str(params.get("aspectRatio") or "landscape"),
            "resolution": resolution,
            "duration": duration,
            "replyType": "async",  # 返回 id 后由 get_status 轮询 /v1/api/result
        }
        if images:
            payload["images"] = images
        if audios:
            payload["audios"] = audios

        seed = params.get("seed")
        if seed not in (None, ""):
            try:
                payload["seed"] = int(seed)
            except (TypeError, ValueError):
                raise ValueError(f"随机种子必须是整数：{seed}")

        skill = params.get("promptSkill")
        if skill is not None and skill != "":
            payload["promptSkill"] = self._to_bool(skill)
        return payload

    @staticmethod
    def _redact(payload: dict) -> dict:
        """请求快照去脂：base64 动辄几 MB，进任务库/轮询响应会拖垮一切。"""
        snap = dict(payload)
        for key in ("images", "audios"):
            snap[key] = [i if i.startswith(("http://", "https://")) else f"<base64，{len(i)} 字符>"
                         for i in payload.get(key, [])]
        return snap

    # ── ⑤ Provider 接口实现 ──────────────────────────────
    async def submit(self, ctx: dict, params: dict) -> str:
        capability = str(ctx.get("_capability") or "text_to_video")
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
                name = url.rsplit("/", 1)[-1].split("?")[0] or f"grsai_{task_id}.mp4"
                outputs.append({"url": url, "filename": name, "subfolder": "", "type": "output"})
            if not outputs:
                return GenerationResult(status=TaskStatus.ERROR,
                                        error="任务成功但没有返回结果视频", raw={"status": status})
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
            async with httpx.AsyncClient(timeout=300.0) as client:  # 视频文件大，给足超时
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
