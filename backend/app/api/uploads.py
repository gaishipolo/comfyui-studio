"""上传 API — 把用户输入的参考图片转发到生成引擎（ComfyUI 输入目录）"""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.engines.factory import get_provider
from app.filestore import read_asset_bytes
from app.models.models import Asset, User
from app.auth import get_current_user
from app.schemas.schemas import AssetUploadRequest
from app.access import can_view_asset, task_owner_map

router = APIRouter(prefix="/api/upload", tags=["upload"])

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp"}
MAX_SIZE = 20 * 1024 * 1024  # 20MB

# 从素材库选材时放行的 mime 前缀。现在只有图片；以后支持视频（或别的类型）
# 在这里加一行即可 —— 前端弹窗也按同样的 kind 前缀过滤，两边同步扩。
ALLOWED_MIME_PREFIXES = ("image/",)


@router.post("/image")
async def upload_image(
    file: UploadFile = File(...),
    engine: str = "comfyui",
    user: User = Depends(get_current_user),
):
    """上传输入图片到生成引擎，返回引擎侧文件名（可直接填入工作流参数）"""
    name = Path(file.filename or "input.png").name
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式：{name}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="图片超过 20MB 限制")

    try:
        provider = get_provider(engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        server_name = await provider.upload_input(data, name)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"上传到生成引擎失败: {e}")

    return {"filename": server_name, "engine": engine}


@router.post("/from-asset")
async def upload_from_asset(
    body: AssetUploadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把素材库里已有的文件推进生成引擎，返回引擎侧文件名。

    素材文件在本服务的磁盘上，而 ComfyUI 是独立的机器，看不到我们的存储目录；
    所以「从素材库选择」选完仍要把字节送进引擎输入目录。返回值与 /image 同构，
    表单拿到文件名后走完全相同的参数注入链路，无需区分来源。

    不再按 20MB 上传限制卡一遍 —— 库里的文件本就是生成产物/历史上传，
    当初入库时已经过一次校验。
    """
    asset = await db.get(Asset, body.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在")

    # 用户隔离：只能拿自己可见的素材当输入（自己的 + 已共享的），看不见的报同款 404
    owners = await task_owner_map(db, {asset.task_id})
    if not can_view_asset(user, owners.get(asset.task_id, ""), asset.is_shared):
        raise HTTPException(status_code=404, detail="素材不存在")

    if not any((asset.mime_type or "").startswith(p) for p in ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"该素材类型暂不支持作为参考输入：{asset.mime_type or asset.file_name}",
        )

    # 越界防护复用 filestore 的唯一实现，不复制第二份
    data = read_asset_bytes(asset.file_path)
    if data is None:
        raise HTTPException(status_code=404, detail="素材文件找不到了")

    try:
        provider = get_provider(body.engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        server_name = await provider.upload_input(data, asset.file_name)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"上传到生成引擎失败: {e}")

    return {"filename": server_name, "engine": body.engine}
