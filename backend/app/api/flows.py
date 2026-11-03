"""流程编排 API —— 流程配置的增删改查、校验、运行。

权限口径（和素材的共享逻辑同一套思路）：
- 读：所有登录用户都能看（和读模板一样）
- 写：作者本人或 admin。**内容同事要能自己编排流程**，所以 member 也能建、
  也能改自己建的；改别人的要 admin —— 否则协作式编排会变成互相覆盖
- 运行：所有登录用户都能跑 active 流程（运营同事的日常）
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.disposition import attachment_disposition
from app.models.models import Flow, FlowRun, Template, User
from app.auth import get_current_user, require_admin
from app.schemas.schemas import (
    FlowCreate,
    FlowImportPayload,
    FlowImportResponse,
    FlowResponse,
    FlowRunResponse,
    FlowUpdate,
    FlowValidateRequest,
    FlowValidateResponse,
    NodeCapability,
)
from app.services.flow_service import FlowError, submit_flow
from app.timeutils import utcnow
from app.services.flow_validate import validate_flow_config
from app.services.flow_portability import (
    FLOW_EXPORT_FORMAT,
    FLOW_EXPORT_VERSION,
    build_export_doc,
    collect_references,
    dumps_doc,
    resolve_references,
    rewrite_references,
)

router = APIRouter(prefix="/api/flows", tags=["flows"])
logger = logging.getLogger(__name__)


def _can_edit(flow: Flow, user: User) -> bool:
    return user.role == "admin" or flow.created_by == user.id


@router.get("", response_model=list[FlowResponse])
async def list_flows(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """流程列表。默认只列 active 的，带 include_inactive 才带停用的。"""
    query = select(Flow).order_by(Flow.updated_at.desc())
    rows = (await db.execute(query)).scalars().all()
    return [FlowResponse.model_validate(f) for f in rows if f.status != "archived"]


@router.get("/capabilities", response_model=list[NodeCapability])
async def list_capabilities(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """能力清单 —— 可用的节点列表，**从数据库自动生成**。

    这份清单同时服务三方：编排器的「可选节点」下拉、给内容同事看的
    「有哪些节点能拼」、以及将来 Agent 编排时的输入（Agent 只看这份
    清单就知道能拼什么，不用读文档 —— 手写文档必然和实现漂移）。
    """
    rows = (
        await db.execute(select(Template).where(Template.is_active.is_(True)))
    ).scalars().all()
    caps: list[NodeCapability] = []
    for t in rows:
        schema = t.input_schema if isinstance(t.input_schema, dict) else {}
        fields = schema.get("fields", []) if isinstance(schema, dict) else []
        caps.append(
            NodeCapability(
                id=t.id,
                name=t.name,
                description=t.description,
                category=t.category,
                engine=t.engine,
                capability=t.capability,
                fields=[
                    {
                        "name": f.get("name"),
                        "label": f.get("label"),
                        "type": f.get("type"),
                        "required": bool(f.get("required")),
                        "options": f.get("options"),
                        "default": f.get("default"),
                        "accept": f.get("accept"),
                        "multiple": bool(f.get("multiple")),
                    }
                    for f in fields
                    if isinstance(f, dict)
                ],
            )
        )
    return caps


@router.post("/validate", response_model=FlowValidateResponse)
async def validate_flow(
    payload: FlowValidateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """校验一份流程配置（不落库）。

    编排器保存前走这里，将来 Agent 生成的配置也走这里 —— 同一道门。
    检查项：步骤 id 唯一、节点存在、参数字段存在、引用的流程输入存在、
    引用的上游步骤存在且在前面、有环，并给出依赖分层。
    """
    templates = {
        t.id: t
        for t in (await db.execute(select(Template))).scalars().all()
    }
    # 所有可调用的流程 —— 用来检查子流程引用是否存在
    flows = {
        f.id: f.name
        for f in (await db.execute(select(Flow))).scalars().all()
    }
    return validate_flow_config(
        steps=[s.model_dump() for s in payload.steps],
        input_schema=payload.input_schema,
        outputs=[o.model_dump() for o in payload.outputs],
        templates=templates,
        flows=flows,
    )


@router.get("/runs/{run_id}", response_model=FlowRunResponse)
async def get_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次运行的进度。"""
    run = await db.get(FlowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return FlowRunResponse.model_validate(run)


@router.get("/{flow_id}", response_model=FlowResponse)
async def get_flow(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    return FlowResponse.model_validate(flow)


@router.post("", response_model=FlowResponse)
async def create_flow(
    payload: FlowCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新建流程。"""
    flow = Flow(
        name=payload.name,
        description=payload.description,
        category=payload.category,
        icon=payload.icon,
        input_schema=payload.input_schema,
        outputs=[o.model_dump() for o in payload.outputs],
        steps=[s.model_dump() for s in payload.steps],
        status=payload.status,
        created_by=user.id,
        version=1,
    )
    db.add(flow)
    await db.commit()
    await db.refresh(flow)
    return FlowResponse.model_validate(flow)


@router.put("/{flow_id}", response_model=FlowResponse)
async def update_flow(
    flow_id: str,
    payload: FlowUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """改流程。没传的字段不动。"""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not _can_edit(flow, user):
        raise HTTPException(status_code=403, detail="只能改自己创建的流程")
    data = payload.model_dump(exclude_unset=True)
    if "steps" in data and data["steps"] is not None:
        data["steps"] = [s.model_dump() for s in payload.steps]
    for key, value in data.items():
        if value is not None:
            setattr(flow, key, value)
    flow.version = (flow.version or 0) + 1
    await db.commit()
    await db.refresh(flow)
    return FlowResponse.model_validate(flow)


@router.delete("/{flow_id}")
async def delete_flow(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删流程。运行记录保留（它们是历史，不是流程的一部分）。"""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not _can_edit(flow, user):
        raise HTTPException(status_code=403, detail="只能删自己创建的流程")
    await db.delete(flow)
    await db.commit()
    return {"message": "流程已删除", "id": flow_id}


@router.post("/{flow_id}/run", response_model=FlowRunResponse)
async def run_flow(
    flow_id: str,
    params: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """跑一条流程。

    提交后立刻返回（后台顺序执行），运行台轮询 GET /api/flows/runs/{id}
    看每一步的进度。
    """
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if flow.status != "active":
        raise HTTPException(status_code=400, detail="流程未启用，不能运行")
    try:
        run = await submit_flow(db, flow, params or {}, user)
    except FlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FlowRunResponse.model_validate(run)


@router.get("/{flow_id}/export")
async def export_flow(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出流程为 JSON 文件。

    包里带上引用的节点与子流程快照 —— 流程不只有自己的配置，它引用了别的
    东西，导到另一台机器上那些引用可能不存在。子流程是递归收集的。

    导出只是换个格式，不提权 —— 读流程本来就不需要管理员权限。
    """
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")

    templates, flows, missing = await collect_references(db, flow)
    if missing:
        # 引用了但本机已经没有的东西 —— 导出的包会残缺，导入到别处得到空壳流程
        raise HTTPException(
            status_code=409,
            detail=(
                f"流程引用的{'、'.join(missing)}在本机已不存在，无法导出"
                "（先把这些引用修好，或删掉相关步骤）"
            ),
        )
    doc = build_export_doc(
        flow,
        templates,
        flows,
        exported_at=utcnow().isoformat(timespec="seconds") + "Z",
    )
    filename = f"{flow.name}.flow.json"
    return Response(
        content=dumps_doc(doc),
        media_type="application/json",
        headers={
            "Content-Disposition": attachment_disposition(filename),
            "Cache-Control": "no-store",
        },
    )


@router.post("/import", response_model=FlowImportResponse)
async def import_flow(
    payload: FlowImportPayload,
    overwrite: bool = Query(False, description="同名流程已存在时覆盖它"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """导入流程导出文件。

    **引用怎么解析**：本机有同 id 的节点/子流程 → 用本机的（对方可能改过，
    本机的才是当前真相）；没有 → 按包里的快照创建一个。

    导入会往系统里加流程（可能连带加节点），所以只给 admin。
    """
    if payload.format != FLOW_EXPORT_FORMAT:
        raise HTTPException(
            status_code=400,
            detail=f"不是本系统导出的流程文件（format={payload.format or '空'}）",
        )
    if payload.version > FLOW_EXPORT_VERSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该文件由更新版本导出（v{payload.version}），"
                f"当前只支持到 v{FLOW_EXPORT_VERSION}，请先升级后端"
            ),
        )
    if not isinstance(payload.flow, dict) or not payload.flow.get("name"):
        raise HTTPException(status_code=400, detail="流程内容不合法：缺少 name")

    warnings: list[str] = []

    # 引用解析：先建/复用节点与子流程，拿到 id 映射
    id_map, ref_warnings = await resolve_references(db, payload.model_dump(), user.id)
    warnings.extend(ref_warnings)

    name = str(payload.flow.get("name") or "").strip()
    steps = rewrite_references(payload.flow.get("steps") or [], id_map)

    existing = (
        await db.execute(select(Flow).where(Flow.name == name))
    ).scalars().first()
    if existing is not None and not overwrite:
        raise HTTPException(
            status_code=409,
            detail=f"流程「{name}」已存在（可以传 overwrite=true 覆盖它）",
        )

    data = {
        "name": name,
        "description": str(payload.flow.get("description") or ""),
        "category": str(payload.flow.get("category") or "image"),
        "icon": str(payload.flow.get("icon") or ""),
        "input_schema": payload.flow.get("input_schema") or {},
        "outputs": payload.flow.get("outputs") or [],
        "steps": steps,
        "status": str(payload.flow.get("status") or "active"),
    }

    if existing is not None:
        for key, value in data.items():
            setattr(existing, key, value)
        existing.version = (existing.version or 1) + 1
        await db.commit()
        await db.refresh(existing)
        return FlowImportResponse(
            id=existing.id, name=existing.name, created=False, warnings=warnings
        )

    flow = Flow(created_by=user.id, **data)
    db.add(flow)
    await db.commit()
    await db.refresh(flow)
    return FlowImportResponse(id=flow.id, name=flow.name, created=True, warnings=warnings)
