"""上传 API — 把用户输入的参考文件转发到生成引擎（ComfyUI 输入目录 / 插件 filestore）

架构说明：上传控件不是「图片专用」，而是按**文件种类（kind）**放行：
  image / audio / video，各自有扩展名白名单、MIME 前缀和大小上限。
加一种新种类只需在 FILE_KINDS 里加一项（前端 accept 传同一个 kind），
不要再像以前那样写死「只收图片」，逼得插件用 audio/audio2/audio3 这种槽位硬凑。
"""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.engines.factory import get_provider
from app.filestore import read_asset_bytes
from app.models.models import Asset, User
from app.auth import get_current_user
from app.schemas.schemas import AssetUploadRequest
from app.access import can_view_asset, task_owner_map

router = APIRouter(prefix="/api/upload", tags=["upload"])

MB = 1024 * 1024

# 文件种类 → 校验规则。ext 用来挡明显不对的文件名，prefixes 用来挡「从素材库选材」时的 MIME
FILE_KINDS: dict[str, dict] = {
    "image": {
        "label": "图片",
        "ext": {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp"},
        "prefixes": ("image/",),
        "max": 20 * MB,
    },
    "audio": {
        "label": "音频",
        "ext": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"},
        "prefixes": ("audio/",),
        "max": 50 * MB,
    },
    "video": {
        "label": "视频",
        "ext": {".mp4", ".webm", ".mov", ".mkv"},
        "prefixes": ("video/",),
        "max": 200 * MB,
    },
}

# 从素材库选材时放行的 mime 前缀（三类都放行，具体能不能用由插件决定）
ALLOWED_MIME_PREFIXES = tuple(p for k in FILE_KINDS.values() for p in k["prefixes"])


def _kind_rules(kind: str) -> dict:
    rules = FILE_KINDS.get((kind or "image").strip().lower())
    if rules is None:
        raise HTTPException(status_code=400, detail=f"不支持的文件种类：{kind}")
    return rules


async def _forward_to_engine(engine: str, data: bytes, name: str) -> str:
    """把字节推进引擎，返回引擎侧文件名（表单直接当字段值用）。"""
    try:
        provider = get_provider(engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        return await provider.upload_input(data, name)
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 —— 引擎侧报错统一收成 502
        raise HTTPException(status_code=502, detail=f"上传到生成引擎失败: {e}")


@router.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    engine: str = "comfyui",
    kind: str = Query("image", description="文件种类：image / audio / video"),
    user: User = Depends(get_current_user),
):
    """上传参考文件（图片 / 音频 / 视频），返回引擎侧文件名。"""
    rules = _kind_rules(kind)
    name = Path(file.filename or f"input.{kind}").name
    if Path(name).suffix.lower() not in rules["ext"]:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的{rules['label']}格式：{name}（允许 {', '.join(sorted(rules['ext']))}）",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > rules["max"]:
        raise HTTPException(
            status_code=400,
            detail=f"{rules['label']}超过 {rules['max'] // MB}MB 限制",
        )

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

    不再按上传大小限制卡一遍 —— 库里的文件本就是生成产物/历史上传，
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

    server_name = await _forward_to_engine(body.engine, data, asset.file_name)
    return {"filename": server_name, "engine": body.engine}
