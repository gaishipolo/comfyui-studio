"""媒体类型判定：扩展名优先，猜不出时看文件头

为什么需要：素材的 MIME 决定前端怎么渲染（`AuthMedia` 按 MIME 分流 img / video / audio）。
内置 ComfyUI 引擎的产物文件名规整（`xxx.mp4`），按扩展名猜没问题；但插件 provider 的产物
是远端 CDN 给的 URL，可能根本没有扩展名（`https://cdn/abc123?token=...`），
`mimetypes.guess_type` 会返回 None → 存成 application/octet-stream → 视频被当图片渲染，一片空白。

所以：扩展名猜得出就用扩展名（快、够用），猜不出再读前 16 字节嗅探。两者都不中才兜底
application/octet-stream。
"""
import mimetypes

# 文件头 → MIME。只覆盖生成平台会产出的常见类型，不做通用魔数库
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"ID3", "audio/mpeg"),
    (b"\x1aE\xdf\xa3", "video/webm"),   # EBML：webm / mkv
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
]

_FALLBACK = "application/octet-stream"


def _sniff(data: bytes) -> str | None:
    if not data:
        return None
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # RIFF 容器：WAVE 音频 / WEBP 图片
    if data[:4] == b"RIFF" and len(data) >= 12:
        if data[8:12] == b"WAVE":
            return "audio/wav"
        if data[8:12] == b"WEBP":
            return "image/webp"
    # ISO BMFF（mp4 / mov）：第 4~8 字节是 ftyp
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:10] == b"qt" else "video/mp4"
    # MPEG 音频裸流（无 ID3 头）
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return None


def guess_mime(filename: str, data: bytes | None = None) -> str:
    """文件名扩展名优先；猜不出时用 data 的文件头嗅探。

    先剥掉 URL 的 ?query / #fragment —— 插件把产物文件名从 URL 截下来时，
    有些供应商的 URL 带签名参数，留着会让扩展名判断失效。
    """
    name = (filename or "").split("?", 1)[0].split("#", 1)[0]
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    return _sniff(data or b"") or _FALLBACK
