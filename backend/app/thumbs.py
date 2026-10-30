"""视频缩略图：抽一帧存 JPEG

为什么：素材库卡片和任务列表里，视频原来是把**整个 mp4** 拉下来当封面（`<video>` 首帧），
一个 2MB 视频就是 2MB 流量，一屏十几个卡片直接卡住。缩略图几十 KB，够用。

ffmpeg 从哪来（按顺序找，找到就用，找不到整个功能静默跳过，绝不影响生成主流程）：
  1. 环境变量 FFMPEG_PATH（自己指定）
  2. PATH 里的 ffmpeg
  3. imageio-ffmpeg 自带的二进制（requirements.txt 里已列，pip 装完就有）

失败一律返回 None —— 缩略图是锦上添花，不能让「存素材」这一步挂掉。
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# None = 还没找过；False = 找过了没有
_FFMPEG: str | bool | None = None

# 抽帧位置：1 秒处（避开很多视频开头的一帧黑场）；不够长就退回 0
_SEEK_SECONDS = 1.0
_MAX_EDGE = 480
_TIMEOUT = 30


def ffmpeg_exe() -> str | None:
    global _FFMPEG
    if _FFMPEG is not None:
        return _FFMPEG or None

    exe = (os.environ.get("FFMPEG_PATH") or "").strip() or None
    if not exe:
        exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg  # 可选依赖

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 —— 没装 / 拿不到二进制都算没有
            exe = None

    _FFMPEG = exe or False
    return exe or None


def make_video_thumb(data: bytes, suffix: str = ".mp4") -> bytes | None:
    """从视频字节里抽一帧，返回 JPEG 字节；任何一步失败都返回 None。"""
    if not data:
        return None
    exe = ffmpeg_exe()
    if not exe:
        return None

    ext = suffix if suffix and len(suffix) <= 6 else ".mp4"
    with tempfile.TemporaryDirectory(prefix="wbthumb-") as td:
        src = Path(td) / f"in{ext}"
        dst = Path(td) / "thumb.jpg"
        src.write_bytes(data)
        for seek in (_SEEK_SECONDS, 0.0):
            cmd = [
                exe, "-y", "-loglevel", "error",
                "-ss", str(seek), "-i", str(src),
                "-frames:v", "1", "-an",
                # min(480, iw)：小视频不放大，大视频等比缩到 480 宽
                "-vf", "scale=min(480\\,iw):-2",
                "-q:v", "4",
                str(dst),
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, check=False)
            except (subprocess.TimeoutExpired, OSError):
                return None
            if dst.exists() and dst.stat().st_size > 0:
                return dst.read_bytes()
    return None


def thumb_key(task_id: str, file_name: str) -> str:
    """缩略图在存储里的键：与视频同目录，加 thumb_ 前缀 + .jpg 后缀。

    约定成路径（而不是数据库字段）是为了让老素材天然「没有缩略图」——
    请求 404 → 前端退回原逻辑，不需要迁移，也不会多出素材记录。
    """
    return f"{task_id}/thumb_{file_name}.jpg"
