"""任务提交与查询 API"""
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.models.models import Task, Template, Asset, User
from app.schemas.schemas import TaskCreate, TaskResponse, AssetResponse
from app.services.task_service import (
    TaskService,
    clear_cancel_request,
    spawn_task,
    _running_tasks,
)
from app.auth import get_current_user
from app.filestore import open_asset, read_asset_bytes, save_asset_file
from app.media import guess_mime
from app.thumbs import (
    cleanup_asset_files,
    make_image_thumb,
    task_lifecycle_key,
    thumb_key,
    thumbnail_lifecycle,
    wait_for_task_completion,
)
from app.access import (
    can_modify_task,
    can_view_asset,
    can_view_task,
    creator_display_names,
    task_owner_map,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

_thumbnail_generation_tasks: dict[str, asyncio.Task[bool]] = {}


def _generate_image_thumbnail(source_key: str, derived_key: str) -> bool:
    data = read_asset_bytes(source_key)
    if not data:
        return False
    thumbnail = make_image_thumb(data)
    if thumbnail is None:
        return False
    save_asset_file(derived_key, thumbnail, content_type="image/jpeg")
    return True


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


def _shared_thumbnail_generation(
    source_key: str,
    derived_key: str,
) -> asyncio.Task[bool]:
    task = _thumbnail_generation_tasks.get(derived_key)
    if task is None:
        task = asyncio.create_task(
            asyncio.to_thread(
                _generate_image_thumbnail,
                source_key,
                derived_key,
            ),
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


@router.post("", response_model=list[TaskResponse])
async def submit_tasks(
    data: TaskCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提交生成任务（支持批量）"""
    template = await db.get(Template, data.template_id)
    if not template or not template.is_active:
        raise HTTPException(status_code=404, detail="Template not found or inactive")

    batch_id = uuid.uuid4().hex
    tasks = []
    for _ in range(data.batch_count):
        task = await TaskService.create_task(
            db, data.template_id, data.params, batch_id,
            created_by=user.id, commit=False,
        )
        tasks.append(task)
    await db.commit()  # 批量提交统一一次 commit

    # 异步执行任务（并发闸门在 task_service 里，超出上限的排队等）
    for task in tasks:
        await spawn_task(task.id)

    return tasks


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    status: str | None = None,
    batch_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询任务列表。用户隔离：普通用户只见自己提交的，admin 可见全部（带创建人）。"""
    query = select(Task).order_by(Task.created_at.desc()).offset(offset).limit(limit)
    if user.role != "admin":
        query = query.where(Task.created_by == user.id)
    if status:
        query = query.where(Task.status == status)
    if batch_id:
        query = query.where(Task.batch_id == batch_id)

    result = await db.execute(query)
    tasks = result.scalars().all()
    names = await creator_display_names(db, {t.created_by for t in tasks})
    return [
        TaskResponse.model_validate(t).model_copy(
            update={"creator_name": names.get(t.created_by, "")}
        )
        for t in tasks
    ]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    # 用户隔离：看不见的任务一律 404，不泄露「存在但无权」
    if not task or not can_view_task(user, task.created_by):
        raise HTTPException(status_code=404, detail="Task not found")
    names = await creator_display_names(db, {task.created_by})
    return TaskResponse.model_validate(task).model_copy(
        update={"creator_name": names.get(task.created_by, "")}
    )


@router.get("/{task_id}/assets", response_model=list[AssetResponse])
async def get_task_assets(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取任务生成的素材"""
    task = await db.get(Task, task_id)
    if not task or not can_view_task(user, task.created_by):
        raise HTTPException(status_code=404, detail="Task not found")
    result = await db.execute(select(Asset).where(Asset.task_id == task_id))
    assets = result.scalars().all()
    names = await creator_display_names(db, {task.created_by})
    creator = names.get(task.created_by, "")
    return [
        AssetResponse.model_validate(a).model_copy(
            update={"created_by": task.created_by, "creator_name": creator}
        )
        for a in assets
    ]


@router.get("/{task_id}/assets/{filename}")
async def download_asset(
    task_id: str,
    filename: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载/预览素材文件"""
    # 安全：只取纯文件名，杜绝 ../ 路径穿越
    safe_name = Path(filename).name
    safe_task_id = Path(task_id).name

    # 只允许下载数据库中登记的素材
    result = await db.execute(
        select(Asset).where(Asset.task_id == safe_task_id, Asset.file_name == safe_name)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset file not found")

    # 用户隔离：任务看不见时，看素材本身是否已共享 —— 共享素材的下载/预览
    # 走的就是这条路由，必须放行，否则素材库里的共享卡片打不开
    owners = await task_owner_map(db, {safe_task_id})
    if not can_view_asset(user, owners.get(safe_task_id, ""), asset.is_shared):
        raise HTTPException(status_code=404, detail="Asset file not found")

    handle = open_asset(asset.file_path)
    if handle is None:
        raise HTTPException(status_code=404, detail="Asset file not found")

    # 落盘时已嗅探过文件头（app/media.py），直接用库里的值；
    # 老记录可能没有 mime_type，再按文件名兜底
    media_type = asset.mime_type or guess_mime(safe_name)

    # 本地后端有真实路径 → FileResponse：支持 HTTP Range，视频拖进度条才不卡
    if handle.local_path is not None:
        return FileResponse(handle.local_path, media_type=media_type, filename=safe_name)

    # 对象存储后端没有本地路径 → 流式转发（前端取的是整段 blob，不影响现有交互）
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
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )


@router.get("/{task_id}/assets/{filename}/thumb")
async def download_asset_thumb(
    task_id: str,
    filename: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve persistent image/video thumbnails after asset authorization."""
    safe_name = Path(filename).name
    safe_task_id = Path(task_id).name
    derived_key = thumb_key(safe_task_id, safe_name)

    async with thumbnail_lifecycle([derived_key]):
        result = await db.execute(
            select(Asset).where(
                Asset.task_id == safe_task_id,
                Asset.file_name == safe_name,
            )
        )
        asset = result.scalar_one_or_none()
        if not asset:
            raise HTTPException(status_code=404, detail="Asset file not found")

        owners = await task_owner_map(db, {safe_task_id})
        if not can_view_asset(user, owners.get(safe_task_id, ""), asset.is_shared):
            raise HTTPException(status_code=404, detail="Asset file not found")

        mime_type = asset.mime_type or guess_mime(safe_name)
        is_image = mime_type.startswith("image/")
        is_video = mime_type.startswith("video/")
        if not (is_image or is_video):
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        etag = f'"{asset.id}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "private, no-cache",
        }
        if_none_match = request.headers.get("if-none-match", "")
        etag_matches = any(
            candidate.strip() in {"*", etag, f"W/{etag}"}
            for candidate in if_none_match.split(",")
        )
        if is_image and etag_matches:
            return Response(status_code=304, headers=headers)

        handle = await asyncio.to_thread(open_asset, derived_key)
        if handle is None:
            if is_video:
                # Never try to decode a missing video thumbnail as an image.
                raise HTTPException(status_code=404, detail="Thumbnail not found")

            # Recheck while owning the lifecycle before registering work. This
            # closes stale-miss races with a just-completed generation task.
            handle = await asyncio.to_thread(open_asset, derived_key)
            if handle is None:
                generation = _shared_thumbnail_generation(
                    asset.file_path,
                    derived_key,
                )
                try:
                    generated = await wait_for_task_completion(generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise HTTPException(status_code=404, detail="Thumbnail not found")
                if not generated:
                    raise HTTPException(status_code=404, detail="Thumbnail not found")
                handle = await asyncio.to_thread(open_asset, derived_key)
                if handle is None:
                    raise HTTPException(status_code=404, detail="Thumbnail not found")

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


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取消任务（排队中或执行中）。仅本人或管理员可取消。

    会一并通知引擎：排队中的 prompt 从队列撤掉，运行中的发中断，
    不让它继续占显存。已终态的任务返回 409。
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if not can_modify_task(user, task.created_by):
        raise HTTPException(
            status_code=403, detail="只能取消自己提交的任务（或联系管理员）"
        )

    try:
        return await TaskService.cancel_task(db, task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重试失败/已取消的任务。仅本人或管理员可重试。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    # 用户隔离：重试原来漏了归属校验 —— 谁都能拿别人失败的任务重跑，堵上
    if not can_modify_task(user, task.created_by):
        raise HTTPException(
            status_code=403, detail="只能重试自己提交的任务（或联系管理员）"
        )
    if task.status not in ("error", "cancelled"):
        raise HTTPException(status_code=400, detail="Only failed or cancelled tasks can be retried")

    task_key = task_lifecycle_key(task.id)

    async def retry_operation() -> Task:
        async with thumbnail_lifecycle([task_key]):
            await db.refresh(task)
            if task.id in _running_tasks:
                raise HTTPException(
                    status_code=409,
                    detail="Task runner is still finishing; retry again shortly",
                )
            if task.status not in ("error", "cancelled"):
                raise HTTPException(
                    status_code=409,
                    detail="Task is no longer in a retryable terminal state",
                )

            # Load outputs only after the old runner is confirmed finished.
            result = await db.execute(select(Asset).where(Asset.task_id == task.id))
            old_assets = result.scalars().all()
            derived_keys = [
                thumb_key(asset.task_id, asset.file_name)
                for asset in old_assets
            ]
            async with thumbnail_lifecycle(derived_keys):
                for old_asset in old_assets:
                    await asyncio.to_thread(
                        cleanup_asset_files,
                        old_asset.file_path,
                        old_asset.task_id,
                        old_asset.file_name,
                    )
                    await db.delete(old_asset)

                # 清掉上一轮的取消登记，否则执行协程一进轮询就当成「已取消」直接退出
                clear_cancel_request(task.id)
                task.status = "pending"
                task.error_msg = ""
                task.progress = 0.0
                task.result = {}
                task.finished_at = None
                await db.commit()
                await db.refresh(task)
                spawned = await spawn_task(task.id)
                if not spawned:
                    task.status = "error"
                    task.error_msg = "Retry could not start; please retry again"
                    task.finished_at = datetime.utcnow()
                    await db.commit()
                    await db.refresh(task)
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Retry could not start; task returned to a "
                            "recoverable error state"
                        ),
                    )
            return task

    operation = asyncio.create_task(
        retry_operation(),
        name=f"retry-task:{task.id}",
    )
    return await wait_for_task_completion(operation)


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除任务及其全部素材（记录 + 磁盘文件）。仅本人或管理员可删。

    级联顺序：先素材文件 → 素材记录 → 任务记录，不留孤儿。
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if not can_modify_task(user, task.created_by):
        raise HTTPException(
            status_code=403, detail="只能删除自己提交的任务（或联系管理员）"
        )

    # 正在跑的任务不能删：后台协程还会往这条记录写进度/素材，删了就是竞态
    if task.status == "running":
        raise HTTPException(
            status_code=400, detail="任务正在执行中，请等它完成或失败后再删除"
        )

    task_key = task_lifecycle_key(task.id)

    async def delete_operation() -> dict:
        files_removed = 0
        file_errors: list[str] = []
        thumbnails_removed = 0
        thumbnail_errors: list[str] = []
        async with thumbnail_lifecycle([task_key]):
            await db.refresh(task)
            if task.status in ("pending", "running") or task.id in _running_tasks:
                raise HTTPException(
                    status_code=409,
                    detail="Task runner is still finishing; delete again shortly",
                )

            result = await db.execute(select(Asset).where(Asset.task_id == task.id))
            assets = result.scalars().all()
            derived_keys = [
                thumb_key(asset.task_id, asset.file_name)
                for asset in assets
            ]
            async with thumbnail_lifecycle(derived_keys):
                for asset in assets:
                    source_result, thumbnail_result = await asyncio.to_thread(
                        cleanup_asset_files,
                        asset.file_path,
                        asset.task_id,
                        asset.file_name,
                    )
                    source_removed, source_error = source_result
                    thumbnail_removed, thumbnail_error = thumbnail_result
                    if source_removed:
                        files_removed += 1
                    elif source_error and source_error != "文件本就不存在":
                        file_errors.append(f"{asset.id}: {source_error}")
                    if thumbnail_removed:
                        thumbnails_removed += 1
                    elif thumbnail_error:
                        thumbnail_errors.append(f"{asset.id}: {thumbnail_error}")
                    await db.delete(asset)

                await db.delete(task)
                await db.commit()

        return {
            "message": "Task deleted",
            "id": task_id,
            "assets_deleted": len(assets),
            "files_removed": files_removed,
            "file_errors": file_errors,
            "thumbnails_removed": thumbnails_removed,
            "thumbnail_errors": thumbnail_errors,
        }

    operation = asyncio.create_task(
        delete_operation(),
        name=f"delete-task:{task.id}",
    )
    return await wait_for_task_completion(operation)
