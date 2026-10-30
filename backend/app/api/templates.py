"""模板 CRUD API"""
import json
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.models import Task, Template, User
from app.schemas.schemas import (
    TemplateCreate, TemplateUpdate, TemplateResponse,
    WorkflowUploadResponse, WorkflowInventoryResponse,
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
