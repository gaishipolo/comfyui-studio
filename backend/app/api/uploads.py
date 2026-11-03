"""上传 API — 把用户输入的参考文件转发到生成引擎（ComfyUI 输入目录 / 插件 filestore）

架构说明：上传控件不是「图片专用」，而是按**文件种类（kind）**放行：
  image / audio / video，各自有扩展名白名单、MIME 前缀和大小上限。
加一种新种类只需在 FILE_KINDS 里加一项（前端 accept 传同一个 kind），
不要再像以前那样写死「只收图片」，逼得插件用 audio/audio2/audio3 这种槽位硬凑。
"""
from pathlib import Path

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.engines.factory import acquire_provider
from app.filestore import (
    FILESTORE_UNAVAILABLE_DETAIL,
    FileStoreUnavailableError,
    open_asset,
    read_asset_bytes,
)
from app.models.models import Asset, User
from app.auth import get_current_user
from app.safe_detail import safe_detail
from app.schemas.schemas import AssetUploadRequest
from app.access import can_view_asset
from app.upload_rules import (
    ALLOWED_MIME_PREFIXES,
    FILE_KINDS,
    MB,
    fallback_filename as _fallback_filename,
    kind_rules as _kind_rules,
    read_upload_bounded,
)

router = APIRouter(prefix="/api/upload", tags=["upload"])
logger = logging.getLogger(__name__)


async def _forward_to_engine(engine: str, data: bytes, name: str) -> str:
    """把字节推进引擎，返回引擎侧文件名（表单直接当字段值用）。"""
    try:
        lease = acquire_provider(engine)
    except ValueError as e:
        logger.error(
            "Provider upload acquisition failed engine=%s filename=%s",
            engine,
            name,
            exc_info=(type(e), e, e.__traceback__),
        )
        raise HTTPException(status_code=400, detail=safe_detail(e)) from e
    try:
        with lease as provider:
            return await provider.upload_input(data, name)
    except NotImplementedError as e:
        logger.error(
            "Provider upload unsupported engine=%s filename=%s",
            engine,
            name,
            exc_info=(type(e), e, e.__traceback__),
        )
        raise HTTPException(status_code=400, detail=safe_detail(e)) from e
    except Exception as e:  # noqa: BLE001 —— 引擎侧报错统一收成 502
        # 异常原文可能带上游完整 URL（含 query 里的 key），过脱敏再回 —— 与 /engines 同一道防线
        logger.error(
            "Provider upload failed engine=%s filename=%s",
            engine,
            name,
            exc_info=(type(e), e, e.__traceback__),
        )
        raise HTTPException(status_code=502, detail=f"上传到生成引擎失败: {safe_detail(e)}") from e


@router.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    engine: str = "comfyui",
    kind: str = Query("image", description="文件种类：image / audio / video"),
    user: User = Depends(get_current_user),
):
    """上传参考文件（图片 / 音频 / 视频），返回引擎侧文件名。"""
    rules = _kind_rules(kind)
    name = Path(file.filename or _fallback_filename(kind)).name
    if Path(name).suffix.lower() not in rules["ext"]:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的{rules['label']}格式：{name}（允许 {', '.join(sorted(rules['ext']))}）",
        )

    data = await read_upload_bounded(file, rules)

    server_name = await _forward_to_engine(engine, data, name)
    return {"filename": server_name, "engine": engine, "kind": kind}


@router.post("/image")
async def upload_image(
    file: UploadFile = File(...),
    engine: str = "comfyui",
    user: User = Depends(get_current_user),
):
    """兼容入口：等价于 /upload/file?kind=image（老前端 / 老脚本还在用）。"""
    return await upload_file(file=file, engine=engine, kind="image", user=user)


@router.post("/from-asset")
async def upload_from_asset(
    body: AssetUploadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把素材库里已有的文件推进生成引擎，返回引擎侧文件名。

    素材文件在本服务的磁盘上，而 ComfyUI 是独立的机器，看不到我们的存储目录；
    所以「从素材库选择」选完仍要把字节送进引擎输入目录。返回值与 /file 同构，
    表单拿到文件名后走完全相同的参数注入链路，无需区分来源。

    数据库大小只作为快速拦截提示；转发前还会以 FileStore 当前大小和实际
    读取字节数为准，避免陈旧元数据绕过上传限制。
    """
    asset = await db.get(Asset, body.asset_id)
    # 回收站里的素材不能当参考输入：用户已经"删"了它，还能继续拿它喂引擎，
    # 等于删除没有生效。按 404 处理，与 get_asset 口径一致。
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="素材不存在")

    # 用户隔离：只能拿自己可见的素材当输入（自己的 + 已共享的），看不见的报同款 404
    if not can_view_asset(user, asset.owner_id, asset.is_shared):
        raise HTTPException(status_code=404, detail="素材不存在")

    if not any((asset.mime_type or "").startswith(p) for p in ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"该素材类型暂不支持作为参考输入：{asset.mime_type or asset.file_name}",
        )

    # 按文件种类套和上传一致的大小上限：素材可能是历史上传/批量导入进来的，
    # 当初入库的限额未必和现在一致（比如老库里混进了超 20M 的图片），
    # 转发到引擎前再卡一道，避免把引擎撑爆 —— 且在读盘之前判，
    # 不为「注定要拒」的大文件把几百 MB 读进内存。
    kind = (asset.mime_type or "").split("/")[0]
    rules = FILE_KINDS.get(kind)
    # 数据库 file_size 可能为 0 或已经过时。先向当前存储后端查询权威大小，
    # 且在整文件读取前关闭这个只用于元数据的句柄。
    try:
        handle = await asyncio.to_thread(open_asset, asset.file_path)
    except FileStoreUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=FILESTORE_UNAVAILABLE_DETAIL,
        ) from exc
    if handle is None:
        raise HTTPException(status_code=404, detail="素材文件找不到了")
    try:
        stored_size = handle.size
    finally:
        handle.close()
    if rules and stored_size > rules["max"]:
        raise HTTPException(
            status_code=400,
            detail=f"{rules['label']}超过 {rules['max'] // MB}MB 限制（{asset.file_name}）",
        )

    # 越界防护复用 filestore 的唯一实现，不复制第二份；
    # to_thread：素材可能是几百 MB 的视频，同步读盘会冻结事件循环
    try:
        data = await asyncio.to_thread(read_asset_bytes, asset.file_path)
    except FileStoreUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=FILESTORE_UNAVAILABLE_DETAIL,
        ) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="素材文件找不到了")
    if rules and len(data) > rules["max"]:
        raise HTTPException(
            status_code=400,
            detail=f"{rules['label']}超过 {rules['max'] // MB}MB 限制（{asset.file_name}）",
        )

    server_name = await _forward_to_engine(body.engine, data, asset.file_name)
    return {"filename": server_name, "engine": body.engine}
