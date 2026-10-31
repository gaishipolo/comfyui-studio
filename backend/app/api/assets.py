"""素材库 API — 搜索、标签、批量下载"""
import asyncio
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Sequence

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import String, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from starlette.background import BackgroundTask

from app.database import get_db
from app.models.models import Asset, Task, Template, User
from app.schemas.schemas import AssetResponse, AssetShareRequest
from app.auth import get_current_user
from app.filestore import open_asset
from app.thumbs import (
    cleanup_asset_files,
    task_lifecycle_key,
    thumb_key,
    thumbnail_lifecycle,
    wait_for_task_completion,
)
from app.timeutils import parse_day as _parse_day
from app.access import (
    can_modify_asset,
    can_view_asset,
    creator_display_names,
    is_admin,
    task_owner_map,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])

# 批量下载护城河：数量与总字节都封顶；真实字节数在打包循环里再兜一道
# （防 file_size 元数据失真）。ZIP 落临时文件，避免整包常驻内存。
BATCH_DOWNLOAD_MAX_FILES = 50
BATCH_DOWNLOAD_MAX_BYTES = 1024 * 1024 * 1024  # 1GB
_abandoned_archive_tasks: set[asyncio.Task[Path]] = set()


def _remove_temp_archive(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def _build_batch_archive(assets: Sequence[Asset]) -> Path:
    """Write a bounded ZIP to disk without buffering source files in memory."""
    archive_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="comfyui-assets-",
            suffix=".zip",
            delete=False,
        ) as temp_file:
            archive_path = Path(temp_file.name)

        used_names: set[str] = set()
        read_total = 0
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for asset in assets:
                handle = open_asset(asset.file_path)
                if handle is None:
                    continue
                try:
                    base_name = PurePosixPath(
                        str(asset.file_name or "").replace("\\", "/")
                    ).name.replace("\x00", "_")
                    if base_name in {"", ".", ".."}:
                        base_name = "asset"

                    archive_name = base_name
                    if archive_name in used_names:
                        raw_prefix = PurePosixPath(
                            str(asset.task_id or "").replace("\\", "/")
                        ).name[:8]
                        prefix = "".join(
                            char if char.isalnum() or char in "-_" else "_"
                            for char in raw_prefix
                        ).strip("._") or "asset"
                        archive_name = f"{prefix}_{base_name}"
                        suffix = PurePosixPath(base_name).suffix
                        stem = base_name[:-len(suffix)] if suffix else base_name
                        duplicate = 2
                        while archive_name in used_names:
                            archive_name = f"{prefix}_{stem}_{duplicate}{suffix}"
                            duplicate += 1
                    used_names.add(archive_name)

                    with archive.open(archive_name, "w") as target:
                        for chunk in handle.iter_bytes():
                            read_total += len(chunk)
                            if read_total > BATCH_DOWNLOAD_MAX_BYTES:
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        "素材实际总大小超过 "
                                        f"{BATCH_DOWNLOAD_MAX_BYTES // (1024 * 1024)}MB 上限，"
                                        "请分批下载"
                                    ),
                                )
                            target.write(chunk)
                finally:
                    handle.close()

        return archive_path
    except BaseException:
        if archive_path is not None:
            _remove_temp_archive(archive_path)
        raise


class _TemporaryArchiveResponse(FileResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette does not reach BackgroundTask when ASGI send raises.
            _remove_temp_archive(Path(self.path))


def _remove_abandoned_archive(task: asyncio.Task[Path]) -> None:
    try:
        archive_path = task.result()
    except BaseException:
        # Retrieving the result prevents a late worker failure from going unobserved.
        pass
    else:
        _remove_temp_archive(archive_path)
    finally:
        _abandoned_archive_tasks.discard(task)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，防止搜索词中的 % _ 造成误匹配"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _annotate(db: AsyncSession, assets: list[Asset]) -> list[AssetResponse]:
    """补上归属信息（created_by / creator_name）—— 素材本身没归属字段，走 task.created_by"""
    owners = await task_owner_map(db, {a.task_id for a in assets})
    names = await creator_display_names(db, set(owners.values()))
    out = []
    for a in assets:
        owner = owners.get(a.task_id, "")
        out.append(
            AssetResponse.model_validate(a).model_copy(
                update={"created_by": owner, "creator_name": names.get(owner, "")}
            )
        )
    return out


async def _owner_of(db: AsyncSession, asset: Asset) -> str:
    """单个素材的归属人 id（显式查，不能用 asset.task 懒加载）"""
    owners = await task_owner_map(db, {asset.task_id})
    return owners.get(asset.task_id, "")


# 标签精确匹配：用 SQLite json_each 展开 JSON 数组后逐个比对。
# 不能用 CAST + LIKE —— JSON 列默认把非 ASCII 转成 \uXXXX，中文标签永远匹配不上；
# 且 LIKE 是子串语义，"图" 会误命中 "图片"。json_each 让 SQLite 自己解码，语义精确。
_TAG_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM json_each(assets.tags) je "
    "WHERE lower(je.value) = lower(:_tag))"
)

# 文件大类。素材现在不只有图片（视频/音频也会落库），列表要能按类分栏。
# 用 mime 前缀匹配，不新增数据库列 —— 已有数据天然带 mime_type，不需要迁移。
ASSET_KINDS = ("image", "video", "audio")


@router.get("", response_model=list[AssetResponse])
async def list_assets(
    keyword: str | None = Query(None, description="搜索关键词（文件名/提示词）"),
    tag: str | None = Query(None, description="按标签筛选"),
    kind: str | None = Query(None, description="按文件类型筛选：image / video / audio"),
    template_id: str | None = Query(None, description="按场景筛选"),
    task_id: str | None = Query(None, description="按任务筛选"),
    creator: str | None = Query(None, description="按创建人 user_id 筛选"),
    created_from: str | None = Query(None, description="创建时间起（2026-10-01 或完整 ISO）"),
    created_to: str | None = Query(None, description="创建时间止（纯日期含当天全天）"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """素材列表，支持搜索和筛选。

    用户隔离：普通用户只见「自己提交的 + 已共享给团队的」，admin 可见全部。
    """
    # 归属过滤、按创建人筛选、按场景筛选都要过任务表 —— join 只做一次，别重复
    need_join = bool(template_id) or bool(creator) or not is_admin(user)
    query = select(Asset)
    if need_join:
        query = query.join(Task, Asset.task_id == Task.id)
    query = query.order_by(Asset.created_at.desc())

    if not is_admin(user):
        query = query.where(
            or_(Task.created_by == user.id, Asset.is_shared.is_(True))
        )

    if creator:
        query = query.where(Task.created_by == creator)

    if created_from:
        query = query.where(Asset.created_at >= _parse_day(created_from))
    if created_to:
        query = query.where(Asset.created_at <= _parse_day(created_to, end=True))

    if keyword:
        like = f"%{_escape_like(keyword)}%"
        query = query.where(
            or_(
                Asset.file_name.ilike(like, escape="\\"),
                # meta 存的是普通文本（JSONStore 用 ensure_ascii=False），可直接 LIKE
                Asset.meta.cast(String).ilike(like, escape="\\"),
            )
        )

    if tag:
        query = query.where(
            text(_TAG_EXISTS_SQL).bindparams(bindparam("_tag", value=tag))
        )

    if task_id:
        query = query.where(Asset.task_id == task_id)

    if kind:
        k = kind.strip().lower()
        if k not in ASSET_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"kind 只能是 {'/'.join(ASSET_KINDS)}，收到：{kind}",
            )
        # mime 可能为空（历史数据/嗅探失败）→ 这些素材在分类视图里不出现，
        # 「全部」视图仍能看到，不至于被藏掉
        query = query.where(Asset.mime_type.ilike(f"{k}/%"))

    if template_id:
        query = query.where(Task.template_id == template_id)

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    return await _annotate(db, result.scalars().all())


@router.get("/tags")
async def list_tags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取所有标签及计数。用户隔离：普通用户只统计自己可见素材（自己的 + 共享的）"""
    query = select(Asset.tags)
    if not is_admin(user):
        query = query.join(Task, Asset.task_id == Task.id).where(
            or_(Task.created_by == user.id, Asset.is_shared.is_(True))
        )
    result = await db.execute(query)
    all_tags = result.scalars().all()

    counter: dict[str, int] = {}
    for tags in all_tags:
        for tag in (tags or []):
            counter[tag] = counter.get(tag, 0) + 1

    return [{"name": k, "count": v} for k, v in sorted(counter.items(), key=lambda x: -x[1])]


@router.get("/{asset_id}", response_model=AssetResponse)
async def get_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    asset = await db.get(Asset, asset_id)
    # 用户隔离：看不见的一律 404，不泄露「存在但无权」
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    owner = await _owner_of(db, asset)
    if not can_view_asset(user, owner, asset.is_shared):
        raise HTTPException(status_code=404, detail="Asset not found")
    return (await _annotate(db, [asset]))[0]


@router.post("/{asset_id}/tags")
async def add_tag(
    asset_id: str,
    tag: str = Query(..., description="要添加的标签"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """给素材添加标签。仅本人或管理员（共享出去的素材别人只能看，不能改）"""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, await _owner_of(db, asset)):
        raise HTTPException(
            status_code=403, detail="只能修改自己提交的素材（或联系管理员）"
        )

    # 注意：JSON 列必须整体赋新列表，原地 append 后赋回同一对象不会被 SQLAlchemy 检测到变更
    tags = list(asset.tags or [])
    if tag not in tags:
        tags.append(tag)
        asset.tags = tags
        flag_modified(asset, "tags")
        await db.commit()

    return {"tags": asset.tags}


@router.delete("/{asset_id}/tags/{tag}")
async def remove_tag(
    asset_id: str,
    tag: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """移除素材标签。仅本人或管理员"""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, await _owner_of(db, asset)):
        raise HTTPException(
            status_code=403, detail="只能修改自己提交的素材（或联系管理员）"
        )

    tags = list(asset.tags or [])
    if tag in tags:
        tags.remove(tag)
        asset.tags = tags
        flag_modified(asset, "tags")
        await db.commit()

    return {"tags": asset.tags}


@router.post("/{asset_id}/share", response_model=AssetResponse)
async def share_asset(
    asset_id: str,
    body: AssetShareRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """共享开关：true = 共享给团队（全员可见可下载），false = 收回。
    仅本人或管理员可操作。共享的是「看」的权限 —— 别人拿不到编辑/删除权。"""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, await _owner_of(db, asset)):
        raise HTTPException(
            status_code=403, detail="只能共享自己提交的素材（或联系管理员）"
        )

    asset.is_shared = bool(body.shared)
    await db.commit()
    await db.refresh(asset)
    return (await _annotate(db, [asset]))[0]


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除单个素材（记录 + 磁盘文件）。仅本人或管理员可删。

    素材本身没有归属字段，归属走 task.created_by —— 显式查一次，
    不能用 asset.task 懒加载（async 会话里抛 MissingGreenlet）。
    """
    async def delete_operation() -> tuple[tuple[bool, str], tuple[bool, str]]:
        # The route only carries asset_id, so first read the immutable task-id
        # hint, end that transaction, then reload all authoritative state while
        # owning the same task lock used by retry and task deletion.
        task_result = await db.execute(
            select(Asset.task_id).where(Asset.id == asset_id)
        )
        owned_task_id = task_result.scalar_one_or_none()
        await db.rollback()
        if not owned_task_id:
            raise HTTPException(status_code=404, detail="Asset not found")

        async with thumbnail_lifecycle([task_lifecycle_key(owned_task_id)]):
            asset = await db.get(Asset, asset_id)
            if asset is None or asset.task_id != owned_task_id:
                raise HTTPException(status_code=404, detail="Asset not found")
            task = await db.get(Task, owned_task_id)
            owner_id = task.created_by if task is not None else ""
            if not can_modify_asset(user, owner_id):
                raise HTTPException(
                    status_code=403,
                    detail="只能删除自己提交的素材（或联系管理员）",
                )

            derived_key = thumb_key(asset.task_id, asset.file_name)
            async with thumbnail_lifecycle([derived_key]):
                # 记录是主体，文件清理是附带 —— 文件删不掉也要把记录删掉，如实上报原因
                cleanup_result = await asyncio.to_thread(
                    cleanup_asset_files,
                    asset.file_path,
                    asset.task_id,
                    asset.file_name,
                )
                await db.delete(asset)
                await db.commit()
                return cleanup_result

    operation = asyncio.create_task(
        delete_operation(),
        name=f"delete-asset:{asset_id}",
    )
    cleanup_result = await wait_for_task_completion(operation)
    (file_removed, file_error), (thumbnail_removed, thumbnail_error) = cleanup_result

    return {
        "message": "Asset deleted",
        "id": asset_id,
        "file_removed": file_removed,
        "file_error": file_error,
        "thumbnail_removed": thumbnail_removed,
        "thumbnail_error": thumbnail_error,
    }


@router.post("/batch-download")
async def batch_download(
    asset_ids: list[str],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量下载素材（打包 ZIP）。用户隔离：无权看的 id 静默剔除（批量语义：拿到自己能拿的）"""
    if not asset_ids:
        raise HTTPException(status_code=400, detail="No assets selected")

    # 数量与总字节都封顶，超了让用户分批下。
    if len(asset_ids) > BATCH_DOWNLOAD_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多打包 {BATCH_DOWNLOAD_MAX_FILES} 个素材，请分批下载",
        )

    result = await db.execute(select(Asset).where(Asset.id.in_(asset_ids)))
    assets = result.scalars().all()

    owners = await task_owner_map(db, {a.task_id for a in assets})
    assets = [
        a for a in assets
        if can_view_asset(user, owners.get(a.task_id, ""), a.is_shared)
    ]

    if not assets:
        raise HTTPException(status_code=404, detail="No matching assets")

    # file_size 是落库时的元数据，作为总量预估；真实字节数在写 ZIP 时再兜一道
    total = sum(a.file_size or 0 for a in assets)
    if total > BATCH_DOWNLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"选中素材共约 {total // (1024 * 1024)}MB，"
                f"超过单次 {BATCH_DOWNLOAD_MAX_BYTES // (1024 * 1024)}MB 上限，请分批下载"
            ),
        )

    archive_task = asyncio.create_task(
        asyncio.to_thread(_build_batch_archive, assets)
    )
    try:
        archive_path = await asyncio.shield(archive_task)
    except BaseException:
        # Executor threads cannot be cancelled. Keep ownership of a late result.
        _abandoned_archive_tasks.add(archive_task)
        archive_task.add_done_callback(_remove_abandoned_archive)
        raise

    try:
        return _TemporaryArchiveResponse(
            archive_path,
            filename="assets.zip",
            media_type="application/zip",
            background=BackgroundTask(_remove_temp_archive, archive_path),
        )
    except BaseException:
        _remove_temp_archive(archive_path)
        raise
