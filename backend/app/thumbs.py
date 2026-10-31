"""Image/video thumbnail conversion and derived-file helpers."""
import asyncio
import io
import logging
import os
import shutil
import subprocess
import tempfile
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

from PIL import Image, ImageOps

from app.filestore import delete_asset_file

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

_thumbnail_lifecycle_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)

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


def task_lifecycle_key(task_id: str) -> str:
    """Coordination key for mutations that affect a task as a whole."""
    return f"task-lifecycle:{task_id}"


@asynccontextmanager
async def thumbnail_lifecycle(keys: Iterable[str]) -> AsyncIterator[None]:
    """Own thumbnail generation/cleanup for sorted storage keys."""
    locks: list[asyncio.Lock] = []
    for key in sorted(set(keys)):
        lock = _thumbnail_lifecycle_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _thumbnail_lifecycle_locks[key] = lock
        locks.append(lock)

    acquired: list[asyncio.Lock] = []
    try:
        for lock in locks:
            await lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


async def wait_for_task_completion(task: asyncio.Task[_T]) -> _T:
    """Delay caller cancellation until a shielded lifecycle task settles."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            break

    try:
        result = task.result()
    except BaseException as exc:
        if cancellation is not None:
            logger.error(
                "Lifecycle task failed after caller cancellation operation=%s: %s",
                task.get_name(),
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        raise
    if cancellation is not None:
        raise cancellation
    return result


def make_image_thumb(data: bytes, max_edge: int = 400) -> bytes | None:
    """Convert image bytes to an EXIF-aware, white-backed optimized JPEG."""
    if not data or max_edge <= 0:
        return None

    try:
        with io.BytesIO(data) as source_stream:
            with Image.open(source_stream) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                try:
                    has_alpha = (
                        "A" in oriented.getbands()
                        or "transparency" in oriented.info
                    )
                    if has_alpha:
                        with oriented.convert("RGBA") as rgba:
                            rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                            with rgba.getchannel("A") as alpha:
                                rgb.paste(rgba, mask=alpha)
                    else:
                        rgb = oriented.convert("RGB")

                    try:
                        rgb.thumbnail(
                            (max_edge, max_edge),
                            Image.Resampling.LANCZOS,
                        )
                        with io.BytesIO() as output:
                            rgb.save(
                                output,
                                format="JPEG",
                                quality=85,
                                optimize=True,
                            )
                            return output.getvalue()
                    finally:
                        rgb.close()
                finally:
                    if oriented is not source:
                        oriented.close()
    except Exception:  # noqa: BLE001 - thumbnail failure must not fail media requests
        return None


def cleanup_asset_files(
    file_path: str,
    task_id: str,
    file_name: str,
) -> tuple[tuple[bool, str], tuple[bool, str]]:
    """Best-effort cleanup for an asset source and its derived thumbnail."""

    def remove(key: str, kind: str) -> tuple[bool, str]:
        try:
            return delete_asset_file(key)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - DB deletion stays primary
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.warning("%s cleanup failed key=%s reason=%s", kind, key, reason)
            return False, reason

    source_result = remove(file_path, "Source file")
    derived_key = thumb_key(task_id, file_name)
    derived_result = remove(derived_key, "Thumbnail")
    if not derived_result[0] and derived_result[1] == "文件本就不存在":
        derived_result = False, ""
    elif derived_result[1]:
        logger.warning(
            "Thumbnail cleanup failed key=%s reason=%s",
            derived_key,
            derived_result[1],
        )
    return source_result, derived_result
