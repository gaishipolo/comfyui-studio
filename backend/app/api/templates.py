"""模板 CRUD API"""
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.models import Task, Template, User
from app.schemas.schemas import (
    TemplateCreate, TemplateUpdate, TemplateResponse,
    TemplateImportPayload, TemplateImportResponse,
    WorkflowUploadResponse, WorkflowInventoryResponse,
    normalize_input_schema,
)
from app.services.workflow_inspect import inspect_workflow
from app.config import settings
from app.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _read_workflow_file(workflow_file: str) -> dict:
    """按文件名读已上传的工作流，读不到/格式不对就 400。"""
    safe_name = Path(workflow_file).name
    path = settings.WORKFLOW_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"工作流文件不存在：{safe_name}")
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail=f"工作流文件不是合法 JSON：{safe_name}")
    if not isinstance(workflow, dict):
        raise HTTPException(status_code=400, detail=f"工作流格式不正确：{safe_name}")
    return workflow


def _validate_workflow_config(workflow_file: str, param_mapping: dict, output_config: dict):
    """保存前校验「映射指向的节点存在」+「出图节点已勾选」。

    只在真的配了工作流时才查 —— generic_api 模板 workflow_file 为空，
    没有节点图可查，硬查会把它们全挡在门外。

    两件事各有出处：
    - 映射指向不存在的节点 → 任务会跑成功但参数没生效，静默失败，必须拦；
    - 出图节点必须显式勾选 → 一个工作流常有多个出图节点，用户只要其中一部分。
    """
    if not workflow_file:
        return
    workflow = _read_workflow_file(workflow_file)

    nodes = (output_config or {}).get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(
            status_code=400,
            detail="必须至少勾选一个出图节点（决定任务保存哪些节点的结果）",
        )
    missing_out = [str(n) for n in nodes if str(n) not in workflow]
    if missing_out:
        raise HTTPException(
            status_code=400,
            detail=f"勾选的出图节点在工作流里不存在：{', '.join(missing_out)}",
        )

    problems: list[str] = []
    for field_name, node_path in (param_mapping or {}).items():
        if not node_path or "." not in node_path:
            problems.append(f"字段「{field_name}」的映射路径非法：{node_path!r}")
            continue
        node_id = node_path.split(".", 1)[0]
        if node_id not in workflow:
            problems.append(f"字段「{field_name}」映射的节点 {node_id} 在工作流里不存在")
    if problems:
        raise HTTPException(status_code=400, detail="参数映射配置错误：" + "；".join(problems))


@router.get("", response_model=list[TemplateResponse])
async def list_templates(
    include_inactive: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """模板列表。include_inactive=true 返回全部（含停用，供管理页使用）"""
    query = select(Template).order_by(Template.sort_order)
    if not include_inactive:
        query = query.where(Template.is_active == True)
    result = await db.execute(query)
    return result.scalars().all()


# ⚠️ 这个静态路径必须排在 GET /{template_id} 前面 —— FastAPI 按注册顺序匹配，
# 放后面 "workflow-inventory" 会被当成 template_id 吃掉，回一个 404。
@router.get("/workflow-inventory", response_model=WorkflowInventoryResponse)
async def get_workflow_inventory(
    filename: str,
    user: User = Depends(require_admin),
):
    """解析已上传的工作流，回三份清单：节点表 / 可注入输入 / 出图节点。

    管理页靠它把「表单字段 → 工作流输入」做成下拉选、把出图节点做成勾选，
    用户不用再打开 JSON 文件对节点。
    """
    safe_name = Path(filename or "").name
    if not safe_name.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="请指定工作流文件名（.json）")
    workflow = _read_workflow_file(safe_name)
    try:
        inventory = inspect_workflow(workflow)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return WorkflowInventoryResponse(filename=safe_name, **inventory)


@router.get("/{template_id}", response_model=TemplateResponse)
async def get_template(
    template_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(Template, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.post("", response_model=TemplateResponse)
async def create_template(
    data: TemplateCreate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(Template).where(Template.name == data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Template name already exists")

    _validate_workflow_config(data.workflow_file, data.param_mapping, data.output_config)

    template = Template(**data.model_dump())
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


@router.put("/{template_id}", response_model=TemplateResponse)
async def update_template(
    template_id: str,
    data: TemplateUpdate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(Template, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # 改名查重，避免撞 unique 约束直接 500
    if data.name and data.name != template.name:
        existing = await db.execute(select(Template).where(Template.name == data.name))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Template name already exists")

    payload = data.model_dump(exclude_none=True)

    # 只在真的带了工作流相关字段时才校验 —— 光改启用状态/排序不该被配置校验挡下。
    # 校验对象是「改完之后的值」，不是这次 payload 里的值。
    if any(k in payload for k in ("workflow_file", "param_mapping", "output_config")):
        _validate_workflow_config(
            payload.get("workflow_file", template.workflow_file),
            payload.get("param_mapping", template.param_mapping),
            payload.get("output_config", template.output_config),
        )

    for key, value in payload.items():
        setattr(template, key, value)

    await db.commit()
    await db.refresh(template)
    return template


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """真正删除模板（释放名称）。

    与「停用」严格区分：停用 = is_active=False，保留记录与名称占用；
    删除 = 物理删除，名称可重新使用。界面是两个不同按钮，后端不能是同一个行为。

    已产生任务记录的模板不允许删除（否则任务历史成为孤儿），请改用「停用」。
    """
    template = await db.get(Template, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # 不能用 template.tasks —— 懒加载关系在 async 会话里会抛 MissingGreenlet
    count_result = await db.execute(
        select(func.count()).select_from(Task).where(Task.template_id == template_id)
    )
    task_count = count_result.scalar_one()
    if task_count:
        raise HTTPException(
            status_code=400,
            detail=f"该模板已有 {task_count} 条任务记录，无法删除；如需下线请使用「停用」",
        )

    await db.delete(template)
    await db.commit()
    return {"message": "Template deleted"}


@router.post("/upload-workflow", response_model=WorkflowUploadResponse)
async def upload_workflow(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
):
    """上传 ComfyUI 工作流 JSON 文件（仅 admin）。

    回执顺带带回三份清单（节点表 / 可注入输入 / 出图节点），
    界面据此渲染映射下拉与出图节点勾选 —— 用户不用再翻 JSON 对节点。
    """
    content = await file.read()
    try:
        workflow = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    # 校验是 ComfyUI 工作流格式（节点字典）
    if not isinstance(workflow, dict):
        raise HTTPException(status_code=400, detail="Invalid workflow format")

    try:
        inventory = inspect_workflow(workflow)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    node_count = len(workflow)

    # 安全：只取纯文件名，杜绝 ../ 路径穿越写入
    raw_name = Path(file.filename or "workflow.json").name
    if not raw_name.lower().endswith(".json"):
        raw_name += ".json"
    settings.WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    save_path = settings.WORKFLOW_DIR / raw_name
    # 同名文件自动加后缀，避免静默覆盖
    if save_path.exists():
        stem = save_path.stem
        save_path = settings.WORKFLOW_DIR / f"{stem}_{uuid.uuid4().hex[:6]}.json"
    save_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")

    # 无出图节点不拦上传：草稿工作流本身没毛病，文件该存还是存。
    # 硬拦截放在保存模板那步（必须勾选出图节点），这里只给明确警告。
    message = (
        f"工作流已上传：{node_count} 个节点，"
        f"{len(inventory['inputs'])} 个可注入输入，{len(inventory['outputs'])} 个出图节点"
    )
    if not inventory["outputs"]:
        message += "。⚠ 未检测到出图节点，请确认这个工作流真的会产出图片/视频/音频"

    return WorkflowUploadResponse(
        filename=save_path.name,
        node_count=node_count,
        message=message,
        **inventory,
    )


# ── 模板导出 / 导入 ──────────────────────────────────────────
# 导出文件是自包含的：模板配置 + 内嵌工作流 JSON。这样换一台机器导入即可用，
# 不用再去翻原实例的工作流目录拷文件。
#
# format / version 两个字段专门用来「拒绝不认识的文件」：
# 用户很容易拿个 zip、别家的 JSON、或者新版本导出的文件来试，
# 与其让它半死不活地导进去，不如直接说清楚为什么不行。

TEMPLATE_EXPORT_FORMAT = "comfyui-workbench/template"
TEMPLATE_EXPORT_VERSION = 1


def _export_payload(tpl: Template) -> dict:
    """导出哪些字段 —— 不含 id / created_at（那是本机的，换实例没意义）

    input_schema 走一遍归一化再导出：库里可能还存着 `image_upload` 这种历史别名，
    导出的文件应该是「接口现在给你的样子」，而不是「当年存进去的样子」。
    """
    return {
        "name": tpl.name,
        "description": tpl.description,
        "category": tpl.category,
        "icon": tpl.icon,
        "input_schema": normalize_input_schema(tpl.input_schema),
        "engine": tpl.engine,
        "capability": tpl.capability,
        "workflow_file": tpl.workflow_file,
        "param_mapping": tpl.param_mapping,
        "output_config": tpl.output_config,
        "is_active": tpl.is_active,
        "sort_order": tpl.sort_order,
    }


def _read_workflow_for_export(workflow_file: str) -> dict | None:
    """把工作流文件内容读出来内嵌进导出文件。读不到就返回 None（导出仍继续）。"""
    if not workflow_file:
        return None
    path = settings.WORKFLOW_DIR / Path(workflow_file).name
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def _free_template_name(db: AsyncSession, name: str) -> str:
    """重名时挑一个没被占用的名字。用「(导入)」「(导入 2)」而不是随机串 ——
    用户看得懂哪个是导入来的。"""
    names = set((await db.execute(select(Template.name))).scalars().all())
    if name not in names:
        return name
    for i in range(1, 100):
        candidate = f"{name} (导入)" if i == 1 else f"{name} (导入 {i})"
        if candidate not in names:
            return candidate
    return f"{name} (导入 {uuid.uuid4().hex[:4]})"


def _canonical_json(obj) -> str:
    """规范化 JSON 文本，用于内容比对（键序、缩进、空格都不影响结果）"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _save_workflow_dedup(workflow: dict, stem: str, prefer: str = "") -> str:
    """把内嵌工作流写进 WORKFLOW_DIR，内容相同的已有文件直接复用。

    去重不是洁癖：同一个模板导出文件被导入两次（换台机器验证、回滚重导）
    就会在目录里堆一串一模一样的工作流副本，谁也不知道哪个是当前用的。
    按内容哈希去重后，重复导入是幂等的。

    `prefer` 是导出文件里记的原文件名 —— 内容一致就优先复用它，
    这样导入后的模板还指着「当初那个文件」，而不是随便一个同内容的文件。
    """
    settings.WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(_canonical_json(workflow).encode("utf-8")).hexdigest()

    def same_content(path: Path) -> bool:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return hashlib.sha256(_canonical_json(existing).encode("utf-8")).hexdigest() == digest

    if prefer:
        candidate = settings.WORKFLOW_DIR / Path(prefer).name
        if candidate.exists() and same_content(candidate):
            return candidate.name

    for path in sorted(settings.WORKFLOW_DIR.glob("*.json")):
        if same_content(path):
            return path.name

    target = settings.WORKFLOW_DIR / f"{stem}_{digest[:6]}.json"
    target.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target.name


@router.get("/{template_id}/export")
async def export_template(
    template_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出单个模板为 JSON 文件（含内嵌工作流）。

    读模板本来就不需要管理员权限（生成页人人都要读），导出只是换个格式，
    不额外提权。
    """
    tpl = await db.get(Template, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    workflow = _read_workflow_for_export(tpl.workflow_file)
    doc = {
        "format": TEMPLATE_EXPORT_FORMAT,
        "version": TEMPLATE_EXPORT_VERSION,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": {
            "template_id": tpl.id,
            "updated_at": tpl.updated_at.isoformat(timespec="seconds") if tpl.updated_at else "",
        },
        "template": _export_payload(tpl),
        "workflow": workflow,
    }
    # 文件名带模板名方便认，但必须走 RFC 5987 编码 —— 中文名直接塞 filename=
    # 会被部分浏览器丢掉，变成一串乱码或纯 download.json
    filename = f"{tpl.name}.template.json"
    return Response(
        content=json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )


@router.post("/import", response_model=TemplateImportResponse)
async def import_template(
    payload: TemplateImportPayload,
    overwrite: bool = Query(False, description="同名模板已存在时覆盖它（保留原 id，任务不会断链）"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """导入模板导出文件。

    导入会改系统配置（模板是所有人生成时共用的），所以只给 admin。
    """
    warnings: list[str] = []

    if payload.format != TEMPLATE_EXPORT_FORMAT:
        raise HTTPException(
            status_code=400,
            detail=f"不是本系统导出的模板文件（format={payload.format or '空'}）",
        )
    if payload.version > TEMPLATE_EXPORT_VERSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该文件由更新版本导出（v{payload.version}），"
                f"当前只支持到 v{TEMPLATE_EXPORT_VERSION}，请先升级后端"
            ),
        )

    # 走 TemplateCreate 校验：input_schema 的归一化白名单顺带生效，
    # 不用在导入路径上再写一遍字段清洗
    try:
        data = TemplateCreate(**payload.template)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        raise HTTPException(
            status_code=400,
            detail=f"模板内容不合法：{first.get('loc', '')} {first.get('msg', '')}".strip(),
        )

    # 内嵌工作流落盘：内容相同就复用已有文件，绝不覆盖本机已有的同名工作流
    if payload.workflow:
        stem = Path(data.workflow_file or "workflow").stem or "workflow"
        data.workflow_file = _save_workflow_dedup(
            payload.workflow, stem, prefer=data.workflow_file
        )
    elif data.workflow_file:
        # 没带工作流却绑了文件名：本机大概率没这个文件，清掉绑定免得存进去就是个坏模板
        missing = settings.WORKFLOW_DIR / Path(data.workflow_file).name
        if not missing.exists():
            warnings.append(
                f"导出文件里没有内嵌工作流，本机也找不到「{data.workflow_file}」，"
                "已清空工作流绑定 —— 需要重新上传工作流并重配参数映射"
            )
            data.workflow_file = ""

    # 引擎 / 能力对不上不算错误：模板可以先落地，等插件装上就能用。
    # 但必须说出来，否则用户会以为是插件坏了
    from app.engines.factory import get_provider, list_engines

    if data.engine not in list_engines():
        warnings.append(
            f"本机没有安装引擎「{data.engine}」，模板已导入但暂时无法生成；"
            "装上同名插件后自动生效"
        )
    elif data.capability:
        try:
            caps = get_provider(data.engine).capabilities
            kinds = set(caps.keys()) if isinstance(caps, dict) else {c.get("kind") for c in caps}
            if data.capability not in kinds:
                warnings.append(
                    f"引擎「{data.engine}」当前没有能力「{data.capability}」，"
                    "模板字段可能与实际接口对不上"
                )
        except Exception:  # noqa: BLE001 —— 探活失败不该挡住导入
            warnings.append(f"引擎「{data.engine}」读取能力声明失败，跳过能力校验")

    existing = (
        await db.execute(select(Template).where(Template.name == data.name))
    ).scalar_one_or_none()

    if existing and overwrite:
        # 覆盖：保留 id —— 历史任务的 template_id 外键还指着它，换 id 等于断链
        for key, value in data.model_dump().items():
            setattr(existing, key, value)
        _validate_workflow_config(existing.workflow_file, existing.param_mapping, existing.output_config)
        await db.commit()
        await db.refresh(existing)
        warnings.append(f"已覆盖同名模板「{existing.name}」")
        return TemplateImportResponse(template=existing, warnings=warnings)

    if existing:
        data.name = await _free_template_name(db, data.name)
        warnings.append(f"已存在同名模板，本次导入为「{data.name}」")

    _validate_workflow_config(data.workflow_file, data.param_mapping, data.output_config)

    tpl = Template(**data.model_dump())
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return TemplateImportResponse(template=tpl, warnings=warnings)
