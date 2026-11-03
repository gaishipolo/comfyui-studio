"""Image/video thumbnail conversion and derived-file helpers."""
import asyncio
import io
import logging
import shutil
import subprocess
import tempfile
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

from PIL import Image, ImageOps

from app.config import settings
from app.filestore import delete_asset_file, prune_empty_asset_dir, safe_key

# 显式像素上限：超大图解码会把整张 RGB 读进内存（5888×10496 ≈ 185MB，
# 加上 exif/convert 副本峰值约 500MB）。PIL 默认阈值（约 178M 像素）太宽松。
# 设为 100M 像素：覆盖实测的 61.8Mpx「99MB 图」，又能挡住 >10000×10000 的
# 病态大图。超过此值 Image.open 抛 DecompressionBombError → make_image_thumb
# 返回 None → 路由回退原图，功能不丢，只是不做缩略图。
Image.MAX_IMAGE_PIXELS = 100_000_000  # 约 10000×10000

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

    exe = settings.FFMPEG_PATH.strip() or None
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


def thumb_key(file_path: str) -> str:
    """缩略图在存储里的键：与源文件同目录，加 thumb_ 前缀 + .jpg 后缀。

    从 **file_path** 推导（而不是 (task_id, file_name)）—— 素材不再必须属于
    某个任务，上传素材的路径是 `uploads/{asset_id}/x.png`，同样能算出键。
    历史素材的 file_path 是 `{task_id}/{name}`，推导结果与原公式逐字节一致，
    老缩略图无需迁移即可命中。
    """
    key = safe_key(file_path) or file_path.replace("\\", "/")
    directory, _, name = key.rpartition("/")
    return f"{directory}/thumb_{name}.jpg" if directory else f"thumb_{name}.jpg"


def task_lifecycle_key(task_id: str) -> str:
    """Coordination key for mutations that affect a task as a whole."""
    return f"task-lifecycle:{task_id}"


def asset_lifecycle_key(asset_id: str) -> str:
    """Coordination key for a single asset（上传素材没有任务，只能按素材加锁）"""
    return f"asset-lifecycle:{asset_id}"


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


async def wait_for_task_completion(
    task: asyncio.Task[_T],
    *,
    timeout: float | None = None,
) -> _T:
    """Delay caller cancellation until a shielded lifecycle task settles.

    `timeout` 到点抛 asyncio.TimeoutError，但**不会取消** task —— 它继续在
    后台跑完，下次请求即可命中产物（缩略图首访用这个做「等一下，超时先给
    原图」）。调用方被取消时取消仍然延迟到 task 结束，且取消优先于超时。
    """
    cancellation: asyncio.CancelledError | None = None
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while not task.done():
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                if cancellation is not None:
                    raise cancellation
                raise asyncio.TimeoutError
        try:
            # asyncio.wait 只监听、不把取消传染给 task（等价于 shield），
            # 同时支持超时。
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            cancellation = exc

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
    derived_key = thumb_key(file_path)
    derived_result = remove(derived_key, "Thumbnail")
    if not derived_result[0] and derived_result[1] == "文件本就不存在":
        derived_result = False, ""
    elif derived_result[1]:
        logger.warning(
            "Thumbnail cleanup failed key=%s reason=%s",
            derived_key,
            derived_result[1],
        )
    # 源文件和缩略图都处理完再收目录：只删掉其中一个时目录还不空，rmdir 会失败跳过
    prune_empty_asset_dir(file_path)
    return source_result, derived_result
