"""任务提交与查询 API"""
import uuid
import mimetypes
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
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
from app.filestore import delete_asset_file, open_asset
from app.access import (
    can_modify_task,
    can_view_asset,
    can_view_task,
    creator_display_names,
    task_owner_map,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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

    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

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

    # 清理上次运行遗留的素材（记录 + 文件），避免重试后素材翻倍
    result = await db.execute(select(Asset).where(Asset.task_id == task.id))
    for old_asset in result.scalars().all():
        delete_asset_file(old_asset.file_path)
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

    await spawn_task(task.id)

    return task


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
    if task.status == "running" or task.id in _running_tasks:
        raise HTTPException(
            status_code=400, detail="任务正在执行中，请等它完成或失败后再删除"
        )

    result = await db.execute(select(Asset).where(Asset.task_id == task.id))
    assets = result.scalars().all()
    files_removed = 0
    file_errors: list[str] = []
    for asset in assets:
        ok, reason = delete_asset_file(asset.file_path)
        if ok:
            files_removed += 1
        elif reason and reason != "文件本就不存在":
            file_errors.append(f"{asset.id}: {reason}")
        await db.delete(asset)

    await db.delete(task)
    await db.commit()

    return {
        "message": "Task deleted",
        "id": task_id,
        "assets_deleted": len(assets),
        "files_removed": files_removed,
        "file_errors": file_errors,
    }
