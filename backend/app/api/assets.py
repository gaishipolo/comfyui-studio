"""素材库 API — 搜索、标签、批量下载"""
import io
import zipfile
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import String, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models.models import Asset, Task, Template, User
from app.schemas.schemas import AssetResponse, AssetShareRequest
from app.auth import get_current_user
from app.filestore import delete_asset_file, read_asset_bytes
from app.timeutils import parse_day as _parse_day
from app.access import (
    can_modify_asset,
    can_view_asset,
    creator_display_names,
    is_admin,
    task_owner_map,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])


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


@router.get("", response_model=list[AssetResponse])
async def list_assets(
    keyword: str | None = Query(None, description="搜索关键词（文件名/提示词）"),
    tag: str | None = Query(None, description="按标签筛选"),
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
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    owner_result = await db.execute(
        select(Task.created_by).where(Task.id == asset.task_id)
    )
    owner_id = owner_result.scalar_one_or_none() or ""

    if not can_modify_asset(user, owner_id):
        raise HTTPException(
            status_code=403, detail="只能删除自己提交的素材（或联系管理员）"
        )

    # 记录是主体，文件清理是附带 —— 文件删不掉也要把记录删掉，如实上报原因
    file_removed, file_error = delete_asset_file(asset.file_path)
    await db.delete(asset)
    await db.commit()

    return {
        "message": "Asset deleted",
        "id": asset_id,
        "file_removed": file_removed,
        "file_error": file_error,
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

    result = await db.execute(select(Asset).where(Asset.id.in_(asset_ids)))
    assets = result.scalars().all()

    owners = await task_owner_map(db, {a.task_id for a in assets})
    assets = [
        a for a in assets
        if can_view_asset(user, owners.get(a.task_id, ""), a.is_shared)
    ]

    if not assets:
        raise HTTPException(status_code=404, detail="No matching assets")

    # 在内存中构建 ZIP
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names: set[str] = set()
        for asset in assets:
            data = read_asset_bytes(asset.file_path)
            if data is None:
                continue
            # 同名文件加任务前缀，避免 ZIP 内互相覆盖
            # arcname 只取纯文件名：写入端（task_service）已用 Path(...).name 清洗过，
            # 但读出端不能假设库里数据永远干净 —— 带 ../ 的 arcname 会让解压方中 zip-slip
            arcname = Path(asset.file_name).name
            if arcname in used_names:
                arcname = f"{asset.task_id[:8]}_{asset.file_name}"
            used_names.add(arcname)
            zf.writestr(arcname, data)

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=assets.zip"},
    )
