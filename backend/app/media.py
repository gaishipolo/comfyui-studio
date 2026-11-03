"""媒体类型判定：扩展名优先，猜不出时看文件头

为什么需要：素材的 MIME 决定前端怎么渲染（`AuthMedia` 按 MIME 分流 img / video / audio）。
内置 ComfyUI 引擎的产物文件名规整（`xxx.mp4`），按扩展名猜没问题；但插件 provider 的产物
是远端 CDN 给的 URL，可能根本没有扩展名（`https://cdn/abc123?token=...`），
`mimetypes.guess_type` 会返回 None → 存成 application/octet-stream → 视频被当图片渲染，一片空白。

所以：扩展名猜得出就用扩展名（快、够用），猜不出再读前 16 字节嗅探。两者都不中才兜底
application/octet-stream。
"""
import mimetypes

# 扩展名 → MIME 显式表。
#
# 为什么不能只靠 mimetypes.guess_type：它读的是**平台自己的** MIME 数据库
# （Windows 读注册表、Linux 读 /etc/mime.types），同一个扩展名两个平台结论不同：
#     wav    Windows audio/wav        Linux audio/x-wav
#     m4a    Windows audio/mp4        Linux 猜不出来（None）
#     flac   Windows audio/x-flac     Linux None
#     ogg    Windows audio/ogg        Linux None
#     mkv    Windows video/x-matroska Linux None
#     svg    Windows image/svg        Linux image/svg+xml
# 猜不出来的那几个会掉到文件头嗅探，而 m4a 的容器是 ISO BMFF，嗅探只能判出
# video/mp4 —— 音频被当视频渲染，前端按 mime.split('/')[0] 分流成 <video>，
# 声音就播不出来了。
# 这个差异只在 Linux（也就是 Docker 部署）上出现：本地 Windows 全绿，
# 部署到容器才炸 —— CI 里跑 Linux 才抓得到。
#
# 覆盖范围就是 upload_rules.FILE_KINDS 里那些扩展名：生成平台的产物类型是
# 已知且有限的，自己钉住最稳，也不用再依赖各平台那份 mime 数据库。
_EXT_MIME: dict[str, str] = {
    # 图片
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "jfif": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "tiff": "image/tiff",
    "heic": "image/heic",
    # 音频
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    # 视频
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
}

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
    # ISO BMFF（mp4 / mov / m4a）：第 4~8 字节是 ftyp
    # major brand 能区分音频容器：M4A 是音频、qt 是 QuickTime、其余按视频算
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand == b"M4A ":
            return "audio/mp4"
        return "video/mp4"
    # MPEG 音频裸流（无 ID3 头）
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return None


def _ext_of(name: str) -> str:
    """取小写扩展名（不含点）。没有扩展名返回空串。

    只看最后一段路径 —— 插件给的 URL 常带 query，已经在调用处剥掉了。
    """
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def guess_mime(
    filename: str,
    data: bytes | None = None,
    declared: str | None = None,
) -> str:
    """文件名扩展名优先；猜不出时用 data 的文件头嗅探。

    先剥掉 URL 的 ?query / #fragment —— 插件把产物文件名从 URL 截下来时，
    有些供应商的 URL 带签名参数，留着会让扩展名判断失效。

    declared 是客户端上传时声明的 MIME（例如浏览器传 wav 会给 audio/wav），
    只在扩展名和文件头都指不出方向时才用：很多客户端对未知类型一律报
    application/octet-stream，那是没有信息量的，不如不要。
    """
    name = (filename or "").split("?", 1)[0].split("#", 1)[0]
    ext = _ext_of(name)
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    sniffed = _sniff(data or b"")
    if sniffed:
        return sniffed
    normalized = (declared or "").split(";")[0].strip().lower()
    if normalized and normalized != "application/octet-stream":
        return normalized
    return _FALLBACK
