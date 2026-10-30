"""引擎插件管理 API —— 上传 Provider 代码、配置注入、启停（仅 admin）

上传 = 服务端执行任意 Python，只给 admin 且留审计（uploaded_by/sha256）。
加载失败只影响该插件（PluginError 文案直接给界面看），绝不拖垮主服务。
"""
import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.access import is_admin
from app.database import get_db
from app.engines.factory import get_provider, set_engine_config, unregister_provider
from app.engines.plugin_loader import (
    PLUGIN_DIR,
    PluginError,
    describe_provider,
    load_all_plugins,
    load_plugin_file,
)
from app.models.models import EnginePlugin, User

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

_SAFE_NAME = re.compile(r"^[a-z0-9_\-]{1,64}$")


class PluginConfigBody(BaseModel):
    config: dict = {}


class PluginEnabledBody(BaseModel):
    enabled: bool


def _require_admin(user: User):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可管理引擎插件")


async def _registry_snapshot(db: AsyncSession) -> list[dict]:
    """注册表 + DB 记录合并成界面要的列表：内置引擎 + 插件一视同仁列出"""
    rows = (await db.execute(select(EnginePlugin))).scalars().all()
    by_name = {r.name: r for r in rows}

    items: list[dict] = []
    # 内置引擎（comfyui/generic_api）：没有 DB 行，描述从类读
    from app.engines.factory import _REGISTRY  # noqa: PLC2701 —— 描述要读注册表
    for name, cls in _REGISTRY.items():
        row = by_name.get(name)
        items.append({
            **describe_provider(cls),
            "builtin": name in ("comfyui", "generic_api"),
            "enabled": True,
            "last_error": "",
            "filename": getattr(row, "filename", "") if row else "",
            "uploaded_by": getattr(row, "uploaded_by", "") if row else "",
            "config": row.config if row else {},
            "has_config": bool(getattr(cls, "config_schema", [])),
        })
    # DB 有记录但当前没注册的（停用/加载失败/文件丢了）
    for row in rows:
        if row.name in _REGISTRY:
            continue
        items.append({
            "name": row.name,
            "label": row.label or row.name,
            "capabilities": {},
            "config_schema": [],
            "builtin": False,
            "enabled": row.enabled,
            "last_error": row.last_error,
            "filename": row.filename,
            "uploaded_by": row.uploaded_by,
            "config": row.config,
            "has_config": bool(row.config) or bool(row.last_error),
        })
    items.sort(key=lambda x: (x["builtin"] is False, x["name"]))
    return items


@router.get("")
async def list_plugins(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """引擎清单（内置 + 插件）—— 模板配置的引擎下拉 / 能力声明都从这读"""
    _require_admin(user)
    return await _registry_snapshot(db)


@router.post("/upload")
async def upload_plugin(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传 Provider 代码（.py）→ 校验 → 加载 → 注册 → 记账"""
    _require_admin(user)
    if not (file.filename or "").endswith(".py"):
        raise HTTPException(status_code=400, detail="只接受 .py 文件")

    code = await file.read()
    if len(code) > 512 * 1024:
        raise HTTPException(status_code=400, detail="插件文件过大（上限 512KB）")

    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PLUGIN_DIR / f"_upload_{hashlib.sha256(code).hexdigest()[:12]}.py"
    tmp_path.write_bytes(code)
    try:
        info = load_plugin_file(tmp_path)
    except PluginError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"插件加载失败: {e}") from e

    # 加载成功：按注册名落正式文件（同名重传 = 升级覆盖）
    name = info["name"]
    if not _SAFE_NAME.match(name):
        unregister_provider(name)
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"注册名「{name}」不合法（小写字母/数字/下划线/连字符）")
    final_path = PLUGIN_DIR / f"{name}.py"
    tmp_path.replace(final_path)

    sha = hashlib.sha256(code).hexdigest()
    row = (await db.execute(select(EnginePlugin).where(EnginePlugin.name == name))).scalar_one_or_none()
    if row is None:
        row = EnginePlugin(name=name, filename=final_path.name, uploaded_by=user.id)
        db.add(row)
    row.label = info["label"]
    row.filename = final_path.name
    row.sha256 = sha
    row.enabled = True
    row.last_error = ""
    # 换了代码重新注入配置（实例缓存已在 register_provider 时清掉）
    set_engine_config(name, row.config)
    await db.commit()
    return {"ok": True, **info}


@router.put("/{name}/config")
async def save_config(
    name: str,
    body: PluginConfigBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """保存引擎运行配置（base_url/api_key 等）—— 存 DB 不进代码，换 key 不用重传"""
    _require_admin(user)
    row = (await db.execute(select(EnginePlugin).where(EnginePlugin.name == name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    row.config = body.config
    set_engine_config(name, body.config)  # 顺带让实例缓存失效，下次用新配置实例化
    await db.commit()
    return {"ok": True}


@router.put("/{name}/enabled")
async def set_enabled(
    name: str,
    body: PluginEnabledBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """启停插件：停用 = 从注册表摘掉（模板提交会报引擎不可用），启用 = 重新加载"""
    _require_admin(user)
    row = (await db.execute(select(EnginePlugin).where(EnginePlugin.name == name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="插件不存在")

    if not body.enabled:
        unregister_provider(name)
        row.enabled = False
        await db.commit()
        return {"ok": True}

    path = PLUGIN_DIR / row.filename
    if not path.exists():
        row.last_error = "代码文件丢失，无法启用"
        await db.commit()
        raise HTTPException(status_code=400, detail="代码文件丢失，无法启用")
    try:
        info = load_plugin_file(path)
    except PluginError as e:
        row.last_error = str(e)
        await db.commit()
        raise HTTPException(status_code=400, detail=f"加载失败: {e}") from e
    row.enabled = True
    row.last_error = ""
    set_engine_config(name, row.config)
    await db.commit()
    return {"ok": True, **info}


@router.delete("/{name}")
async def delete_plugin(
    name: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除插件：注销 + 删文件 + 删账"""
    _require_admin(user)
    if name in ("comfyui", "generic_api"):
        raise HTTPException(status_code=400, detail="内置引擎不可删除")
    row = (await db.execute(select(EnginePlugin).where(EnginePlugin.name == name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    unregister_provider(name)
    (PLUGIN_DIR / row.filename).unlink(missing_ok=True)
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.post("/{name}/test")
async def test_plugin(
    name: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """健康检查 —— 真调一次 provider.health_check()，验证配置与连通性"""
    _require_admin(user)
    try:
        provider = get_provider(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"引擎未启用: {e}") from e
    ok = await provider.health_check()
    return {"ok": ok, "detail": "" if ok else "health_check 未通过（检查 base_url / api_key 配置）"}


async def restore_plugins(db: AsyncSession) -> list[dict]:
    """启动时恢复：扫目录加载 → 按 DB 启停/配置校正 → 补账。

    约定：文件系统是代码的唯一事实源，DB 是元数据（启停/配置/审计）的唯一事实源。
    手工扔进 plugins/ 的文件会被加载并自动补一条记录（uploaded_by 为空）。
    """
    results = load_all_plugins()
    rows = (await db.execute(select(EnginePlugin))).scalars().all()
    by_name = {r.name: r for r in rows}

    for item in results:
        name = item.get("name")
        if not item["ok"]:
            # 加载失败的文件，错误记到同名账上（能对上才记）
            for row in rows:
                if row.filename == item["file"]:
                    row.last_error = item["error"]
            continue
        row = by_name.get(name)
        if row is None:
            row = EnginePlugin(name=name, label=item["label"], filename=item["file"], uploaded_by="")
            db.add(row)
        if not row.enabled:
            unregister_provider(name)  # 账上是停用状态 → 摘掉
            continue
        row.last_error = ""
        set_engine_config(name, row.config)

    # 账上有但文件丢了的
    for row in rows:
        if not (PLUGIN_DIR / row.filename).exists() and row.name not in ("comfyui", "generic_api"):
            row.last_error = "代码文件丢失"
            unregister_provider(row.name)

    await db.commit()
    return results
