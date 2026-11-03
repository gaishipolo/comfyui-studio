"""素材文件服务 —— 原文件下载/预览 与 缩略图。

历史上这些逻辑长在 `api/tasks.py` 里，因为素材当时只能是任务产物、路由也按
(task_id, filename) 定位。2026-11-02 素材独立化后，文件服务跟任务再无关系，
抽成独立模块：**路由层只负责鉴权，取字节/生成缩略图/缓存协商都在这里**。

缩略图双轨：
- 视频：落盘时（task_service）就用 ffmpeg 抽好帧，这里只负责读
- 图片：首访懒生成，生成后落盘，之后直接命中
"""
import asyncio
import logging

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from app.disposition import attachment_disposition
from app.filestore import (
    FILESTORE_UNAVAILABLE_DETAIL,
    FileStoreUnavailableError,
    open_asset,
    read_asset_bytes,
    save_asset_file,
)
from app.media import guess_mime
from app.models.models import Asset
from app.thumbs import (
    make_image_thumb,
    thumb_key,
    thumbnail_lifecycle,
    wait_for_task_completion,
)

logger = logging.getLogger(__name__)

_thumbnail_generation_tasks: dict[str, asyncio.Task[bool]] = {}

# 缩略图解码是 CPU + 内存密集（61.8Mpx 大图解码峰值约 500MB）。全局串行生成，
# 避免「打开一屏大图 → N 张并发解码 → 内存尖峰」。缩略图是后台产物，串行几乎
# 不损失体验，却把内存峰值钉死在「一张大图」的量级。
_thumbnail_generation_slots = asyncio.Semaphore(1)

# 首访（缩略图还没生成）时最多等这么久再给结果。等到了直接返回缩略图；
# 超时就回退原图、生成任务继续在后台跑，下次访问命中缩略图。
#
# 用「超时」而不是「按文件大小判断」：文件大小和解码耗时相关性很弱——实测
# 99.6MB / 61.8Mpx 的 PNG 生成缩略图只要 1.07s，而 20MB 的高压缩 JPEG 也可能
# 更慢。按 size 一刀切会让大图首访直接下载原图（99MB），反而比等 1 秒差得多。
_THUMB_EAGER_TIMEOUT_SECONDS = 3.0


async def open_asset_handle(file_path: str):
    try:
        return await asyncio.to_thread(open_asset, file_path)
    except FileStoreUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=FILESTORE_UNAVAILABLE_DETAIL,
        ) from exc


def _generate_image_thumbnail(source_key: str, derived_key: str) -> bool:
    data = read_asset_bytes(source_key)
    if not data:
        return False
    thumbnail = make_image_thumb(data)
    if thumbnail is None:
        return False
    save_asset_file(derived_key, thumbnail, content_type="image/jpeg")
    return True


async def _generate_image_thumbnail_async(source_key: str, derived_key: str) -> bool:
    """在全局并发闸门内生成缩略图（解码 + 写盘放线程池，不冻事件循环）。"""
    async with _thumbnail_generation_slots:
        return await asyncio.to_thread(
            _generate_image_thumbnail,
            source_key,
            derived_key,
        )


def _thumbnail_generation_done(
    derived_key: str,
    task: asyncio.Task[bool],
) -> None:
    try:
        generated = task.result()
        if not generated:
            logger.warning("Thumbnail generation failed key=%s", derived_key)
    except asyncio.CancelledError:
        logger.warning("Thumbnail generation cancelled key=%s", derived_key)
    except Exception:
        logger.exception("Thumbnail generation failed key=%s", derived_key)
    finally:
        if _thumbnail_generation_tasks.get(derived_key) is task:
            _thumbnail_generation_tasks.pop(derived_key, None)


def shared_thumbnail_generation(
    source_key: str,
    derived_key: str,
) -> asyncio.Task[bool]:
    """按派生键去重：多个并发请求共享同一次生成，不会各跑一遍。"""
    task = _thumbnail_generation_tasks.get(derived_key)
    if task is None:
        task = asyncio.create_task(
            _generate_image_thumbnail_async(source_key, derived_key),
            name=f"thumbnail:{derived_key}",
        )
        _thumbnail_generation_tasks[derived_key] = task
        task.add_done_callback(
            lambda completed, key=derived_key: _thumbnail_generation_done(
                key,
                completed,
            )
        )
    return task


async def _serve_original_image(asset: Asset, media_type: str) -> Response:
    """缩略图暂不可用时回退原图（大图首访 / 生成失败），保证页面能显示。

    用 `no-store`：不把原图钉死在浏览器缓存里，下次请求（此时缩略图多半已生成）
    才能拿到小图。代价是极端情况下（缩略图还没好就狂刷新）会重复下载原图。
    """
    handle = await open_asset_handle(asset.file_path)
    if handle is None:
        raise HTTPException(status_code=404, detail="Asset file not found")

    headers = {"Cache-Control": "no-store"}
    if handle.local_path is not None:
        local_path = handle.local_path
        handle.close()
        return FileResponse(local_path, media_type=media_type, headers=headers)

    def stream_original():
        try:
            yield from handle.iter_bytes()
        finally:
            handle.close()

    return StreamingResponse(
        stream_original(),
        media_type=media_type,
        headers={**headers, "Content-Length": str(handle.size)},
    )


async def serve_asset_file(asset: Asset) -> Response:
    """素材原文件（下载/预览）。鉴权由调用方负责。"""
    handle = await open_asset_handle(asset.file_path)
    if handle is None:
        raise HTTPException(status_code=404, detail="Asset file not found")

    # 落盘时已嗅探过文件头（app/media.py），直接用库里的值；
    # 老记录可能没有 mime_type，再按文件名兜底
    media_type = asset.mime_type or guess_mime(asset.file_name)

    # 本地后端有真实路径 → FileResponse：支持 HTTP Range，视频拖进度条才不卡
    if handle.local_path is not None:
        local_path = handle.local_path
        handle.close()
        return FileResponse(local_path, media_type=media_type, filename=asset.file_name)

    # 对象存储后端没有本地路径 → 流式转发
    def _stream():
        try:
            yield from handle.iter_bytes()
        finally:
            handle.close()

    return StreamingResponse(
        _stream(),
        media_type=media_type,
        headers={
            "Content-Length": str(handle.size),
            # 文件名用户可控（中文前缀常见）：必须走 RFC 5987 编码，
            # 裸拼会让非 latin-1 字符在 header 编码时抛错 → 500
            "Content-Disposition": attachment_disposition(asset.file_name),
        },
    )


async def serve_asset_thumb(asset: Asset, request: Request) -> Response:
    """素材缩略图（图片懒生成 / 视频落盘时已抽帧）。鉴权由调用方负责。"""
    derived_key = thumb_key(asset.file_path)

    async with thumbnail_lifecycle([derived_key]):
        mime_type = asset.mime_type or guess_mime(asset.file_name)
        is_image = mime_type.startswith("image/")
        is_video = mime_type.startswith("video/")
        if not (is_image or is_video):
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        etag = f'"{asset.id}"'
        # 缩略图按 asset.id 不可变：允许浏览器缓存一小时，省掉每次回源校验。
        headers = {
            "ETag": etag,
            "Cache-Control": "private, max-age=3600",
        }
        if_none_match = request.headers.get("if-none-match", "")
        etag_matches = any(
            candidate.strip() in {"*", etag, f"W/{etag}"}
            for candidate in if_none_match.split(",")
        )
        if is_image and etag_matches:
            return Response(status_code=304, headers=headers)

        handle = await open_asset_handle(derived_key)
        if handle is None:
            if is_video:
                # Never try to decode a missing video thumbnail as an image.
                raise HTTPException(status_code=404, detail="Thumbnail not found")

            # Recheck while owning the lifecycle before registering work. This
            # closes stale-miss races with a just-completed generation task.
            handle = await open_asset_handle(derived_key)
            if handle is None:
                generation = shared_thumbnail_generation(
                    asset.file_path,
                    derived_key,
                )
                # 首访等一下生成结果：等到了直接给缩略图（省掉原图下载）；
                # 超过 _THUMB_EAGER_TIMEOUT_SECONDS 就回退原图，生成任务在
                # 后台继续跑，下次访问即命中缩略图。
                try:
                    generated = await wait_for_task_completion(
                        generation,
                        timeout=_THUMB_EAGER_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    generated = False
                except asyncio.CancelledError:
                    raise
                except FileStoreUnavailableError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=FILESTORE_UNAVAILABLE_DETAIL,
                    ) from exc
                except Exception:
                    generated = False
                if generated:
                    handle = await open_asset_handle(derived_key)
                if handle is None:
                    # 生成超时 / 失败 → 回退原图（不 404），保证页面能显示
                    return await _serve_original_image(asset, mime_type)

        if etag_matches:
            handle.close()
            return Response(status_code=304, headers=headers)

        if handle.local_path is not None:
            local_path = handle.local_path
            handle.close()
            return FileResponse(
                local_path,
                media_type="image/jpeg",
                headers=headers,
            )

        def stream_thumbnail():
            try:
                yield from handle.iter_bytes()
            finally:
                handle.close()

        try:
            return StreamingResponse(
                stream_thumbnail(),
                media_type="image/jpeg",
                headers={**headers, "Content-Length": str(handle.size)},
            )
        except BaseException:
            handle.close()
            raise
