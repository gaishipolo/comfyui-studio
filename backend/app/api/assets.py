"""素材库 API — 上传、搜索、标签、文件服务、批量下载

素材不再必须属于某个任务（2026-11-02）：用户可以直接把文件传进素材库，
这类素材的 `task_id` 为 NULL、归属写在 `owner_id`。因此文件/缩略图路由
按 **asset_id** 定位，而不是 (task_id, filename)。
"""
import asyncio
import logging
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Sequence

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import String, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from starlette.background import BackgroundTask

from app.asset_media import serve_asset_file, serve_asset_thumb
from app.asset_names import unique_asset_name
from app.asset_retention import purge_matching
from app.database import get_db
from app.download_tickets import registry as download_ticket_registry
from app.media import guess_mime
from app.models.models import Asset, Task, User
from app.quota import ensure_quota, usage_summary
from app.schemas.schemas import AssetResponse, AssetShareRequest
from app.auth import get_current_user
from app.filestore import (
    FILESTORE_UNAVAILABLE_DETAIL,
    FileStoreUnavailableError,
    open_asset,
    save_asset_file,
)
from app.upload_rules import FILE_KINDS, kind_for_filename, read_upload_bounded
from app.thumbs import cleanup_asset_files, make_image_thumb, thumb_key
from app.timeutils import parse_day_or_400 as _parse_day, utcnow
from app.access import (
    can_modify_asset,
    can_view_asset,
    creator_display_names,
    is_admin,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])
logger = logging.getLogger(__name__)

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


def _build_batch_archive(entries: Sequence[tuple[str, str, str]]) -> Path:
    """把素材打包成 ZIP 落临时文件（不把源文件读进内存）。

    入参是**纯数据** `(file_path, file_name, task_id)`，不是 ORM 对象 ——
    这个函数跑在 `asyncio.to_thread` 的工作线程里，而 async session 的 ORM
    实例属于事件循环的 greenlet 上下文，在线程里碰属性会触发懒加载并卡死。
    """
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
            for file_path, file_name, task_id in entries:
                handle = open_asset(file_path)
                if handle is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Selected asset file is missing",
                    )
                try:
                    base_name = PurePosixPath(
                        str(file_name or "").replace("\\", "/")
                    ).name.replace("\x00", "_")
                    if base_name in {"", ".", ".."}:
                        base_name = "asset"

                    archive_name = base_name
                    if archive_name in used_names:
                        raw_prefix = PurePosixPath(
                            str(task_id or "").replace("\\", "/")
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
    """补上归属显示名。归属直接读 assets.owner_id —— 不再回表查任务。

    素材脱离任务独立存在后，「归属 = 产出任务的提交人」这个恒等式不成立了，
    owner_id 才是唯一来源。这里只查一次用户名，不做 N+1。
    """
    names = await creator_display_names(db, {a.owner_id for a in assets})
    out = []
    for a in assets:
        owner = a.owner_id or ""
        out.append(
            AssetResponse.model_validate(a).model_copy(
                update={"created_by": owner, "creator_name": names.get(owner, "")}
            )
        )
    return out


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
    tag: str | None = Query(None, max_length=64, description="按标签筛选"),
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
    # 回收站里的不进列表（deleted_at 非空）。归属过滤和按创建人筛选都读
    # assets.owner_id —— 素材自带归属，不用再 join 任务表。
    # 只有「按模板筛选」必须过任务表，且用 outerjoin：上传素材没有来源任务，
    # 筛模板时自然落选（inner join 结果相同，但 outer 语义更准确）。
    query = (
        select(Asset)
        .where(Asset.deleted_at.is_(None))
        .order_by(Asset.created_at.desc())
    )

    if not is_admin(user):
        query = query.where(
            or_(Asset.owner_id == user.id, Asset.is_shared.is_(True))
        )

    if creator:
        query = query.where(Asset.owner_id == creator)

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
        query = query.outerjoin(Task, Asset.task_id == Task.id).where(
            Task.template_id == template_id
        )

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    return await _annotate(db, result.scalars().all())


# 一次最多传多少个文件。单文件上限由 FILE_KINDS 管，这里防的是「一次几百个」
MAX_UPLOAD_FILES = 20

# 用户上传素材的存储前缀。按 asset_id 分目录：一个素材一个目录，天然不重名，
# 也不像生成素材那样依赖 task_id。
UPLOAD_DIR_PREFIX = "uploads"


@router.post("/upload", response_model=list[AssetResponse])
async def upload_assets(
    files: list[UploadFile] = File(..., description="一个或多个文件"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把文件直接存进素材库（不经过生成引擎）。

    与 `POST /api/upload/file` 的区别：那个是「给这次生成提供参考文件」，
    传完即丢；这里落的是素材库里的正式条目，之后能在「从素材库选择」里复用。

    逐个处理（读一个 → 落盘 → 登记一个），不把所有文件先读进内存 ——
    10 个 200MB 的视频全缓冲就是 2GB。任一文件失败则整体回滚，不留半成品。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多上传 {MAX_UPLOAD_FILES} 个文件",
        )

    # 同一归属人名下文件名去重：卡片重名在界面上分不清谁是谁。
    # 物理路径按 asset_id 分目录，重名本来不冲突，去重只是为了显示清晰。
    existing = await db.execute(
        select(Asset.file_name).where(Asset.owner_id == user.id)
    )
    used_names = set(existing.scalars().all())

    # 先做「文件名 + 种类」预检（不读内容）：混合上传里只要有一个不合规就整体
    # 拒绝，此时还没落盘，不会留下空目录和半个批次。
    staged: list[tuple[UploadFile, str, dict]] = []
    for upload in files:
        raw_name = Path(upload.filename or "").name
        if not raw_name:
            raise HTTPException(status_code=400, detail="文件缺少文件名")
        kind = kind_for_filename(raw_name)
        if kind is None:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的格式：{raw_name}（只收图片 / 音频 / 视频）",
            )
        staged.append((upload, raw_name, FILE_KINDS[kind]))

    created_ids: list[str] = []
    saved: list[tuple[str, str]] = []  # (存储键, 文件名) —— 失败时回滚磁盘
    # 本批次累计新增字节。一次传多个文件要按总量判断配额，不能逐个放行
    # （否则 10 个各 500MB 的文件在「还剩 600MB」时全能通过）。
    pending_bytes = 0
    try:
        for upload, raw_name, rules in staged:
            data = await read_upload_bounded(upload, rules)
            # 配额检查在落盘之前：超限时当前文件还没写，之前写过的由 finally 回滚
            pending_bytes += len(data)
            await ensure_quota(db, user, pending_bytes)
            mime = guess_mime(raw_name, data, declared=upload.content_type)
            safe_name = unique_asset_name(raw_name, used_names)
            used_names.add(safe_name)

            asset_id = uuid.uuid4().hex
            rel_path = f"{UPLOAD_DIR_PREFIX}/{asset_id}/{safe_name}"
            # to_thread：视频可达 200MB，同步写盘会冻住事件循环
            await asyncio.to_thread(save_asset_file, rel_path, data, content_type=mime)
            saved.append((rel_path, safe_name))

            # 图片顺手生成缩略图（视频的抽帧在任务落盘那边做，上传路径不处理）。
            # 不预生成的话，卡片首次访问要现场解码 2MB 原图，而 /thumb 只等 3 秒
            # 就回退原图 —— 人多时等于一直拉原图。失败就跳过，不影响素材本身；
            # 回滚时 cleanup_asset_files 会连缩略图一起删，不留孤儿文件。
            if mime.startswith("image/"):
                try:
                    thumb = await asyncio.to_thread(make_image_thumb, data)
                    if thumb:
                        await asyncio.to_thread(
                            save_asset_file,
                            thumb_key(rel_path),
                            thumb,
                            content_type="image/jpeg",
                        )
                except Exception:  # noqa: BLE001 —— 缩略图是锦上添花
                    logger.warning(
                        "上传图片缩略图生成失败，跳过：%s", safe_name, exc_info=True
                    )

            db.add(
                Asset(
                    id=asset_id,
                    owner_id=user.id,
                    task_id=None,  # 没有来源任务 —— 这正是素材独立化的意义
                    file_path=rel_path,
                    file_name=safe_name,
                    file_size=len(data),
                    mime_type=mime,
                    tags=[],
                    meta={"source": "upload", "original_name": raw_name},
                )
            )
            created_ids.append(asset_id)

        await db.commit()
    except BaseException:
        await db.rollback()
        for rel_path, safe_name in saved:
            await asyncio.to_thread(cleanup_asset_files, rel_path, safe_name)
        raise

    # commit 后 ORM 实例已过期，在 async 会话里碰属性会触发懒加载。
    # 重新查一遍拿干净快照（数量 ≤ 20，成本可忽略）。
    result = await db.execute(select(Asset).where(Asset.id.in_(created_ids)))
    return await _annotate(db, list(result.scalars().all()))


@router.get("/tags")
async def list_tags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取所有标签及计数。用户隔离：普通用户只统计自己可见素材（自己的 + 共享的）

    归属读 `assets.owner_id`（上传素材没有任务，join 任务表会漏掉它们的标签），
    回收站里的素材不计入 —— 列表里看不到的标签不该出现在筛选器上。
    """
    query = select(Asset.tags).where(Asset.deleted_at.is_(None))
    if not is_admin(user):
        query = query.where(or_(Asset.owner_id == user.id, Asset.is_shared.is_(True)))
    result = await db.execute(query)
    all_tags = result.scalars().all()

    counter: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for tags in all_tags:
        for tag in (tags or []):
            normalized = str(tag).strip()
            if not normalized:
                continue
            key = normalized.casefold()
            display_names.setdefault(key, normalized)
            counter[key] = counter.get(key, 0) + 1

    return [
        {"name": display_names[key], "count": count}
        for key, count in sorted(
            counter.items(),
            key=lambda item: (-item[1], display_names[item[0]].casefold()),
        )
    ]


# ── 回收站 ──────────────────────────────────────────────
# 路由顺序有讲究：/trash 必须注册在 /{asset_id} 之前，否则 "trash" 会被当成
# asset_id 匹配走（FastAPI 按注册顺序匹配，先注册先命中）。


@router.get("/quota")
async def get_quota(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的存储配额概览。`used_bytes` **含回收站** —— 文件还在磁盘上。

    前端在素材库顶部展示；超限时上传按钮应置灰。
    """
    return await usage_summary(db, user)


@router.get("/trash", response_model=list[AssetResponse])
async def list_trash(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回收站列表。普通用户只看自己删的，管理员看全部。

    共享素材进回收站后只有作者和管理员看得到 —— 共享的是「看正常素材」的
    权限，不是「看别人删掉的东西」。
    """
    query = (
        select(Asset)
        .where(Asset.deleted_at.is_not(None))
        .order_by(Asset.deleted_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if not is_admin(user):
        query = query.where(Asset.owner_id == user.id)
    result = await db.execute(query)
    return await _annotate(db, list(result.scalars().all()))


@router.delete("/trash")
async def empty_trash(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清空回收站：记录 + 磁盘文件一起删，不可恢复。

    普通用户只清自己的，管理员清全部。走 `purge_matching` 分批（每批 50 个），
    与启动时的保留期清理同一套逻辑 —— 一次删几千个文件会撞上本机 unlink 的
    批量守卫（SystemExit），也会让请求长时间不返回。批次用尽还没清完时，
    `remaining` 会告诉前端"还剩多少，请再执行一次"。
    """
    conditions: list = [Asset.deleted_at.is_not(None)]
    if not is_admin(user):
        conditions.append(Asset.owner_id == user.id)

    stats = await purge_matching(db, conditions)
    return {
        "deleted": stats["purged"],
        "files_removed": stats["files_removed"],
        "file_errors": stats["errors"],
        "remaining": stats["remaining"],
    }


@router.post("/{asset_id}/restore", response_model=AssetResponse)
async def restore_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """从回收站恢复。仅本人或管理员。"""
    asset = await db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is None:
        raise HTTPException(status_code=404, detail="回收站里没有这个素材")
    if not can_modify_asset(user, asset.owner_id):
        raise HTTPException(status_code=403, detail="只能恢复自己的素材（或联系管理员）")

    asset.deleted_at = None
    await db.commit()
    return (await _annotate(db, [asset]))[0]


@router.delete("/{asset_id}/purge")
async def purge_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """彻底删除：记录 + 磁盘文件，不可恢复。只对回收站里的素材生效。

    要求「先在回收站」是刻意的：正常素材必须走一次软删除，避免误点直接把
    文件删掉。想一步到位就调两次接口，代价是一次多余的点击。
    """
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.deleted_at is None:
        raise HTTPException(status_code=409, detail="素材不在回收站里，请先删除")
    if not can_modify_asset(user, asset.owner_id):
        raise HTTPException(status_code=403, detail="只能删除自己的素材（或联系管理员）")

    file_path = asset.file_path
    file_name = asset.file_name
    await db.delete(asset)
    await db.commit()

    try:
        (file_removed, file_error), (thumbnail_removed, thumbnail_error) = (
            await asyncio.to_thread(cleanup_asset_files, file_path, file_name)
        )
    except (OSError, RuntimeError, SystemExit) as exc:
        file_removed, thumbnail_removed = False, False
        file_error = thumbnail_error = f"{type(exc).__name__}: {exc}"

    return {
        "message": "已彻底删除",
        "id": asset_id,
        "file_removed": file_removed,
        "file_error": file_error,
        "thumbnail_removed": thumbnail_removed,
        "thumbnail_error": thumbnail_error,
    }


@router.get("/{asset_id}", response_model=AssetResponse)
async def get_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    asset = await db.get(Asset, asset_id)
    # 用户隔离：看不见的一律 404，不泄露「存在但无权」。
    # 回收站里的也按 404 处理 —— 详情页/预览是「正常素材」的入口，
    # 回收站有自己的列表接口。
    if not asset or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_view_asset(user, asset.owner_id, asset.is_shared):
        raise HTTPException(status_code=404, detail="Asset not found")
    return (await _annotate(db, [asset]))[0]


@router.get("/{asset_id}/file")
async def download_asset_file(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """素材原文件（下载 / 预览）。用户隔离：看不见的一律 404。

    按 asset_id 定位 —— 素材可以没有来源任务，旧的
    `/api/tasks/{task_id}/assets/{filename}` 已废弃。
    """
    asset = await db.get(Asset, asset_id)
    # 回收站里的不给取文件 —— 否则「删除」只是从列表消失，链接仍能下载
    if not asset or asset.deleted_at is not None or not can_view_asset(user, asset.owner_id, asset.is_shared):
        raise HTTPException(status_code=404, detail="Asset file not found")
    return await serve_asset_file(asset)


@router.get("/{asset_id}/thumb")
async def download_asset_thumb(
    asset_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """素材缩略图。图片首访懒生成后落盘，视频在落盘时就已抽帧。"""
    asset = await db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or not can_view_asset(user, asset.owner_id, asset.is_shared):
        raise HTTPException(status_code=404, detail="Asset file not found")
    return await serve_asset_thumb(asset, request)


@router.post("/{asset_id}/tags")
async def add_tag(
    asset_id: str,
    tag: str = Query(..., max_length=64, description="要添加的标签"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """给素材添加标签。仅本人或管理员（共享出去的素材别人只能看，不能改）

    回收站里的素材一律 404 —— 已经"删掉"的东西还能被改标签，语义上是错的。
    """
    asset = await db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, asset.owner_id):
        raise HTTPException(
            status_code=403, detail="只能修改自己提交的素材（或联系管理员）"
        )

    tag = tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="标签不能为空")

    # 注意：JSON 列必须整体赋新列表，原地 append 后赋回同一对象不会被 SQLAlchemy 检测到变更
    tags = list(asset.tags or [])
    if not any(str(existing).strip().casefold() == tag.casefold() for existing in tags):
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
    if not asset or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, asset.owner_id):
        raise HTTPException(
            status_code=403, detail="只能修改自己提交的素材（或联系管理员）"
        )

    tag = tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="标签不能为空")
    if len(tag) > 64:
        raise HTTPException(status_code=400, detail="标签不能超过 64 个字符")

    tags = list(asset.tags or [])
    key = tag.casefold()
    remaining = [
        existing
        for existing in tags
        if str(existing).strip().casefold() != key
    ]
    if len(remaining) != len(tags):
        asset.tags = remaining
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
    仅本人或管理员可操作。共享的是「看」的权限 —— 别人拿不到编辑/删除权。

    回收站里的素材不能共享：否则会变成「删掉了但对全员可见」的幽灵条目。
    """
    asset = await db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, asset.owner_id):
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
    """把素材移入回收站（软删除）。仅本人或管理员可删。

    软删除只打 `deleted_at` 时间戳，**不动磁盘文件** —— 误删能恢复，但配额
    不会因此释放（文件还在盘上）。要真腾空间走 `DELETE /{id}/purge`，或等
    超过保留期被自动清理。

    这里不加任务级联锁：不碰文件系统，纯 DB 事务；同一素材重复删除会在第二次
    发现 `deleted_at` 非空而返回 404，天然幂等。
    """
    asset = await db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not can_modify_asset(user, asset.owner_id):
        raise HTTPException(
            status_code=403,
            detail="只能删除自己提交的素材（或联系管理员）",
        )

    now = utcnow()
    asset.deleted_at = now
    await db.commit()
    return {
        "message": "已移入回收站",
        "id": asset_id,
        "deleted_at": now.isoformat(),
    }


@router.post("/batch-download")
async def batch_download(
    asset_ids: list[str],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量下载素材（打包 ZIP）。用户隔离：无权看的 id 静默剔除（批量语义：拿到自己能拿的）"""
    archive_path = await _prepare_archive(asset_ids, user, db)
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


async def _prepare_archive(asset_ids: list[str], user: User, db: AsyncSession) -> Path:
    """共享的归档构建逻辑：校验、权限过滤、落临时 ZIP。"""
    if not asset_ids:
        raise HTTPException(status_code=400, detail="No assets selected")

    if len(asset_ids) > BATCH_DOWNLOAD_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多打包 {BATCH_DOWNLOAD_MAX_FILES} 个素材，请分批下载",
        )

    result = await db.execute(
        select(Asset).where(Asset.id.in_(asset_ids), Asset.deleted_at.is_(None))
    )
    assets = result.scalars().all()

    assets = [a for a in assets if can_view_asset(user, a.owner_id, a.is_shared)]

    if not assets:
        raise HTTPException(status_code=404, detail="No matching assets")

    total = sum(a.file_size or 0 for a in assets)
    if total > BATCH_DOWNLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"选中素材共约 {total // (1024 * 1024)}MB，"
                f"超过单次 {BATCH_DOWNLOAD_MAX_BYTES // (1024 * 1024)}MB 上限，请分批下载"
            ),
        )

    # 在线程启动前把字段摘成纯数据：工作线程里不能碰 ORM 实例
    entries = [(a.file_path, a.file_name, a.task_id or "") for a in assets]
    archive_task = asyncio.create_task(
        asyncio.to_thread(_build_batch_archive, entries)
    )
    try:
        archive_path = await asyncio.shield(archive_task)
    except FileStoreUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=FILESTORE_UNAVAILABLE_DETAIL,
        ) from exc
    except BaseException:
        _abandoned_archive_tasks.add(archive_task)
        archive_task.add_done_callback(_remove_abandoned_archive)
        raise
    return archive_path


@router.post("/batch-download/prepare")
async def prepare_batch_download(
    asset_ids: list[str],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """为浏览器原生下载准备 ZIP：登记一次性票据并返回下载地址。

    浏览器 `<a>` 直链无法带 Authorization 头，因此把安全凭证放进按票据
    隔离的 HttpOnly Cookie，下载接口单次消费。
    """
    archive_path = await _prepare_archive(asset_ids, user, db)
    ticket = download_ticket_registry.create(archive_path, user.id)
    secure = request.url.scheme == "https"
    headers = {
        "Set-Cookie": download_ticket_registry.cookie_attrs(ticket, secure=secure),
    }
    return JSONResponse(
        content={
            "url": f"/api/assets/batch-download/file/{ticket.ticket_id}",
        },
        headers=headers,
    )


@router.get("/batch-download/file/{ticket_id}")
async def download_batch_file(
    ticket_id: str,
    request: Request,
):
    """消费一次性票据并流式返回 ZIP。Cookie 名按 ticket_id 隔离。"""
    cookie_name = f"comfyui_batch_dl_{ticket_id}"
    secret = request.cookies.get(cookie_name)
    ticket = download_ticket_registry.consume(ticket_id, secret)
    if ticket is None:
        raise HTTPException(status_code=410, detail="Download ticket missing, expired, or already used")
    try:
        return FileResponse(
            ticket.archive_path,
            filename="assets.zip",
            media_type="application/zip",
            background=BackgroundTask(
                lambda p=ticket.archive_path: download_ticket_registry.discard_archive(p)
            ),
        )
    except BaseException:
        download_ticket_registry.discard_archive(ticket.archive_path)
        raise
