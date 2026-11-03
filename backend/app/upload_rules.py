"""上传文件的种类规则 —— 图片 / 音频 / 视频各自的扩展名、MIME 前缀、大小上限。

两处消费：
- `api/uploads.py`：把参考文件推进生成引擎（临时用，传完即丢）
- `api/assets.py`：把文件直接存进素材库（长期保存）

规则必须一致，否则「生成表单能传 20MB、素材库只让传 5MB」这种割裂会立刻
被用户发现。加一种新种类只需在 FILE_KINDS 里加一项。
"""
from pathlib import Path

from fastapi import HTTPException, UploadFile

MB = 1024 * 1024

# 文件种类 → 校验规则。ext 用来挡明显不对的文件名，prefixes 用来挡「从素材库选材」时的 MIME
FILE_KINDS: dict[str, dict] = {
    "image": {
        "label": "图片",
        "ext": {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp"},
        "prefixes": ("image/",),
        "max": 20 * MB,
    },
    "audio": {
        "label": "音频",
        "ext": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"},
        "prefixes": ("audio/",),
        "max": 50 * MB,
    },
    "video": {
        "label": "视频",
        "ext": {".mp4", ".webm", ".mov", ".mkv"},
        "prefixes": ("video/",),
        "max": 200 * MB,
    },
}

# 从素材库选材时放行的 mime 前缀（三类都放行，具体能不能用由插件决定）
ALLOWED_MIME_PREFIXES = tuple(p for k in FILE_KINDS.values() for p in k["prefixes"])


def kind_rules(kind: str) -> dict:
    rules = FILE_KINDS.get((kind or "image").strip().lower())
    if rules is None:
        raise HTTPException(status_code=400, detail=f"不支持的文件种类：{kind}")
    return rules


def kind_for_filename(name: str) -> str | None:
    """按扩展名反推种类。认不出返回 None（素材库上传时用来拒绝未知格式）。"""
    suffix = Path(name).suffix.lower()
    for kind, rules in FILE_KINDS.items():
        if suffix in rules["ext"]:
            return kind
    return None


def fallback_filename(kind: str) -> str:
    """缺失 filename 时使用该种类允许的保守扩展名，避免兜底路径自拒。"""
    return {
        "image": "input.png",
        "audio": "input.mp3",
        "video": "input.mp4",
    }.get((kind or "image").strip().lower(), "input.bin")


async def read_upload_bounded(file: UploadFile, rules: dict) -> bytes:
    """分块读、即时封顶。

    若先 read() 再查大小，恶意客户端传多大服务器就先吃多少进内存，
    200MB 上限对内存 DoS 形同虚设。超限在读到第 (max+1) 个字节时就断掉。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > rules["max"]:
            raise HTTPException(
                status_code=400,
                detail=f"{rules['label']}超过 {rules['max'] // MB}MB 限制",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    return data
