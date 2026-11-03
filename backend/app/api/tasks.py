"""任务提交与查询 API

素材文件的下载/预览/缩略图已迁到 `app/asset_media.py` —— 素材独立化后
它们与任务无关，路由在 `api/assets.py` 里按 asset_id 注册。
"""
import asyncio
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.models import Task, Template, Asset, User
from app.quota import ensure_quota
from app.schemas.schemas import TaskCreate, TaskResponse, AssetResponse
from app.services.task_service import (
    TaskService,
    clear_cancel_request,
    spawn_task,
    _running_tasks,
)
from app.services.random_fields import resolve_random_fields
from app.auth import get_current_user
from app.thumbs import (
    cleanup_asset_files,
    task_lifecycle_key,
    thumb_key,
    thumbnail_lifecycle,
    wait_for_task_completion,
)
from app.timeutils import utcnow
from app.access import (
    can_modify_task,
    can_view_task,
    creator_display_names,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

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

    # 配额准入：产物多大要等引擎跑完才知道，这里只能判断「当前是否已超限」。
    # 不在落盘时按大小拦截 —— 用户白等几分钟换一个失败，比多占几百 MB 更糟。
    await ensure_quota(db, user, 0)

    batch_id = uuid.uuid4().hex
    tasks = []
    for _ in range(data.batch_count):
        # random_number 字段填 -1 = 「每次生成自动换一个」：逐任务展开，
        # 批量 N 张各用不同种子；展开后的真实值随 params 落库，便于复现
        task = await TaskService.create_task(
            db, data.template_id, resolve_random_fields(template.input_schema, data.params),
            batch_id, created_by=user.id, commit=False,
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
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
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
    result = await db.execute(
        select(Asset).where(Asset.task_id == task_id, Asset.deleted_at.is_(None))
    )
    assets = result.scalars().all()
    # 归属读素材自己的 owner_id（写入时已按 task.created_by 落好），
    # 不再假定「任务提交人 = 素材作者」
    names = await creator_display_names(db, {a.owner_id for a in assets})
    return [
        AssetResponse.model_validate(a).model_copy(
            update={
                "created_by": a.owner_id or "",
                "creator_name": names.get(a.owner_id, ""),
            }
        )
        for a in assets
    ]


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
    task_key = task_lifecycle_key(task_id)

    async def retry_operation(owned_task_id: str) -> Task:
        async with thumbnail_lifecycle([task_key]):
            task = await db.get(Task, owned_task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            if not can_modify_task(user, task.created_by):
                raise HTTPException(
                    status_code=403,
                    detail="只能重试自己提交的任务（或联系管理员）",
                )
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
                thumb_key(asset.file_path)
                for asset in old_assets
            ]
            async with thumbnail_lifecycle(derived_keys):
                for old_asset in old_assets:
                    await asyncio.to_thread(
                        cleanup_asset_files,
                        old_asset.file_path,
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
                    task.finished_at = utcnow()
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
        retry_operation(task_id),
        name=f"retry-task:{task_id}",
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
    task_key = task_lifecycle_key(task_id)

    async def delete_operation(owned_task_id: str) -> dict:
        files_removed = 0
        file_errors: list[str] = []
        thumbnails_removed = 0
        thumbnail_errors: list[str] = []
        async with thumbnail_lifecycle([task_key]):
            task = await db.get(Task, owned_task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            if not can_modify_task(user, task.created_by):
                raise HTTPException(
                    status_code=403,
                    detail="只能删除自己提交的任务（或联系管理员）",
                )
            if task.status in ("pending", "running") or task.id in _running_tasks:
                raise HTTPException(
                    status_code=409,
                    detail="Task runner is still finishing; delete again shortly",
                )

            result = await db.execute(select(Asset).where(Asset.task_id == task.id))
            assets = result.scalars().all()
            derived_keys = [
                thumb_key(asset.file_path)
                for asset in assets
            ]
            async with thumbnail_lifecycle(derived_keys):
                for asset in assets:
                    source_result, thumbnail_result = await asyncio.to_thread(
                        cleanup_asset_files,
                        asset.file_path,
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
        delete_operation(task_id),
        name=f"delete-task:{task_id}",
    )
    return await wait_for_task_completion(operation)
