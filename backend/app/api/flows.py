"""流程编排 API —— 流程配置的增删改查、校验、运行。

权限口径（和素材的共享逻辑同一套思路）：
- 读：所有登录用户都能看（和读模板一样）
- 写：作者本人或 admin。**内容同事要能自己编排流程**，所以 member 也能建、
  也能改自己建的；改别人的要 admin —— 否则协作式编排会变成互相覆盖
- 运行：所有登录用户都能跑 active 流程（运营同事的日常）
"""
import logging
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.disposition import attachment_disposition
from app.models.models import Flow, FlowRun, Template, User
from app.auth import get_current_user, require_admin
from app.schemas.schemas import (
    AgentApproveRequest,
    AgentApproveResponse,
    AgentDraftRequest,
    AgentDraftResponse,
    AgentGenerateRequest,
    AgentGenerateResponse,
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
from app.services import agent_llm
from app.services.agent_spec import build_spec
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


async def _validation_context(db: AsyncSession) -> tuple[dict, dict]:
    """校验用的真实数据：全部节点 + 全部流程。

    流程引用子流程时要查它是否存在，所以不能只取 active 的流程 ——
    草稿流程也可能被引用（编排过程中先串起来再上线是常规做法）。
    """
    templates = {
        t.id: t for t in (await db.execute(select(Template))).scalars().all()
    }
    flows = {
        f.id: f.name for f in (await db.execute(select(Flow))).scalars().all()
    }
    return templates, flows


def _reject_invalid_config(result: dict, *, action: str) -> None:
    """配置有错就不落库 —— 不让坏流程进数据库，别等跑的时候才发现。"""
    if result["ok"]:
        return
    detail = "；".join(result["errors"][:5])
    raise HTTPException(
        status_code=400,
        detail=f"流程配置有问题，{action}被拒绝：{detail}",
    )


@router.post("/validate", response_model=FlowValidateResponse)
async def validate_flow(
    payload: FlowValidateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """校验一份流程配置（不落库）。

    编排器保存前、将来 Agent 生成的配置都走这里 —— 同一道门。
    检查项：步骤 id 唯一、节点存在、参数字段存在、引用的流程输入存在、
    引用的上游步骤存在、有环，并给出依赖分层。
    """
    templates, flows = await _validation_context(db)
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


# ── Agent 编排 ───────────────────────────────────────────────────────
#
# 四步：spec（拿规格）→ draft（提交配置，落草稿）→ trial（试跑）
#      → approve（人审批后生效）
#
# **为什么 Agent 的配置和人手工编的配置走同一道门**：
# Agent 只是另一个写配置的人，它的产出一样可能写错（引用了不存在的节点、
# 字段接错、步骤成环）。给它特权等于放弃质量控制 —— 所以 draft 提交
# 用的就是编排器保存时走的那个 validate_flow_config。


@router.get("/agent/spec")
async def agent_spec(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """编排规格 —— Agent 想知道「能拼什么、怎么写合法」就问这里。

    **这份规格是从代码生成的**，不是手写文档：
    - 可用节点 → 数据库里 active 的模板（和编排器下拉里同一批）
    - 可用函数 → 表达式白名单本身
    - 步骤类型与规则 → 校验器的实际逻辑

    手写的规格必然和实现漂移 —— 加了新东西忘了写进文档，
    Agent 就会一直按旧规则拼，产出一堆跑不通的配置。
    """
    templates = (
        await db.execute(select(Template).where(Template.is_active.is_(True)))
    ).scalars().all()
    flows = (
        await db.execute(select(Flow).where(Flow.status == "active"))
    ).scalars().all()
    return build_spec(templates, flows)


async def _unique_flow_name(db: AsyncSession, base: str) -> str:
    """给 Agent 生成的草稿挑一个没被占用的名字。

    为什么自动加后缀而不是报错：一句话生成是「多试几次」的功能，
    第二次生成撞上第一条草稿的名字就失败，体验上莫名其妙。
    人工新建走的是另一条路（同名直接报错），因为那是人在决定名字。
    """
    base = (base or "Agent 流程").strip()[:110] or "Agent 流程"
    for index in range(1, 51):
        candidate = base if index == 1 else f"{base} · {index}"
        exists = (
            await db.execute(select(Flow.id).where(Flow.name == candidate))
        ).scalars().first()
        if exists is None:
            return candidate
    raise HTTPException(
        status_code=409,
        detail="同名草稿太多了，先在编排器里清理一下再生成",
    )


@router.get("/agent/status")
async def agent_status(user: User = Depends(get_current_user)):
    """Agent 通道状态 —— 界面据此决定「能不能用一句话生成」。

    只回是否可用和模型名，**不回 base_url / key**：前端不需要，
    而这些信息对排查部署问题以外的人没有价值。
    """
    configured = agent_llm.is_configured()
    return {
        "configured": configured,
        "model": settings.LLM_MODEL.strip() if configured else "",
    }


def _node_snapshot(t: Template):
    """把节点拍成普通对象，供 LLM 调用期间使用。

    为什么要拍：调模型可能几十秒，这段时间不该一直攥着数据库连接
    （50 个人同时点，连接池会直接排满）。拍完快照就结束读事务，
    把连接还回去 —— 校验只需要 id / name / input_schema 这几项。
    """
    return SimpleNamespace(
        id=t.id,
        name=t.name,
        description=t.description or "",
        category=t.category or "",
        engine=t.engine or "",
        capability=t.capability or "",
        input_schema=t.input_schema or {},
    )


@router.post("/agent/generate", response_model=AgentGenerateResponse)
async def agent_generate(
    payload: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一句话需求 → 流程配置 → **草稿**。

    这是「生成」入口，和 `/agent/draft`（提交现成配置）的区别只是
    「谁写配置」：模型写还是外部 Agent 写。落库规则完全一样 ——
    校验、draft、试跑、人审批，一步不少。

    **生成失败不落库**：只回错误和轨迹，让人改需求再来一次。
    留一堆跑不通的半成品草稿，比没有这个功能更糟。
    """
    user_id = user.id
    template_rows = (
        await db.execute(select(Template).where(Template.is_active.is_(True)))
    ).scalars().all()
    flow_rows = (
        await db.execute(select(Flow).where(Flow.status == "active"))
    ).scalars().all()

    nodes = [_node_snapshot(t) for t in template_rows]
    subflows = [
        SimpleNamespace(id=f.id, name=f.name, description=f.description or "")
        for f in flow_rows
    ]
    templates = {t.id: t for t in nodes}
    flows = {f.id: f.name for f in subflows}
    spec = build_spec(nodes, subflows)

    # 结束读事务，把连接还给池子；接下来的等待是网络时间，不是数据库时间
    await db.rollback()

    try:
        result = await agent_llm.generate_flow_draft(
            payload.requirement,
            spec,
            templates,
            flows,
        )
    except agent_llm.AgentLLMError as exc:
        # 通道不可用是**部署问题**，不是需求问题 —— 文案要区分开，
        # 否则用户会一直改需求，而真正该做的是找管理员配模型
        return AgentGenerateResponse(ok=False, message=str(exc), errors=[str(exc)])

    if not result.ok or not result.config:
        return AgentGenerateResponse(
            ok=False,
            errors=result.errors,
            attempts=result.attempts,
            model=result.model,
            message=result.message,
        )

    config = result.config
    name = await _unique_flow_name(db, str(config.get("name") or "Agent 流程"))
    note_parts = [f"模型 {result.model}"]
    if result.attempts:
        note_parts.append(f"第 {len(result.attempts)} 次尝试通过校验")
    if result.note.strip():
        note_parts.append(result.note.strip())

    flow = Flow(
        name=name,
        description=str(config.get("description") or ""),
        category=str(config.get("category") or payload.category),
        icon=str(config.get("icon") or ""),
        input_schema=config.get("input_schema") or {},
        outputs=config.get("outputs") or [],
        steps=config.get("steps") or [],
        # 关键：模型只写配置，不能自己上线
        status="draft",
        created_by=user_id,
        version=1,
        agent_note=" · ".join(note_parts),
    )
    db.add(flow)
    await db.commit()
    await db.refresh(flow)

    return AgentGenerateResponse(
        ok=True,
        flow_id=flow.id,
        name=flow.name,
        note=flow.agent_note,
        warnings=result.warnings,
        layers=result.layers,
        attempts=result.attempts,
        model=result.model,
        message=f"已生成草稿「{flow.name}」，试跑确认效果后再审批上线",
    )


@router.post("/agent/draft", response_model=AgentDraftResponse)
async def agent_draft(
    payload: AgentDraftRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Agent 提交流程配置 → 校验 → 落成**草稿**。

    校验不过就不落库，返回错误让 Agent 自己改（ok=false, errors 非空）。
    校验过了也只是 draft —— **人审批后才 active**，Agent 不能自己上线。

    同名流程已存在时**不覆盖** —— 直接报错。Agent 反复提交同名配置
    应该是改自己的草稿，不是把别人在用的流程冲掉。
    """
    templates = {
        t.id: t for t in (await db.execute(select(Template))).scalars().all()
    }
    flows = {
        f.id: f.name for f in (await db.execute(select(Flow))).scalars().all()
    }
    result = validate_flow_config(
        steps=[s.model_dump() for s in payload.steps],
        input_schema=payload.input_schema,
        outputs=[o.model_dump() for o in payload.outputs],
        templates=templates,
        flows=flows,
    )
    if not result["ok"]:
        return AgentDraftResponse(
            ok=False,
            errors=result["errors"],
            warnings=result["warnings"],
            layers=result["layers"],
        )

    existing = (
        await db.execute(select(Flow).where(Flow.name == payload.name))
    ).scalars().first()
    if existing is not None:
        return AgentDraftResponse(
            ok=False,
            errors=[f"流程「{payload.name}」已存在，换个名字再提交"],
            warnings=result["warnings"],
        )

    flow = Flow(
        name=payload.name,
        description=payload.description,
        category=payload.category,
        icon=payload.icon,
        input_schema=payload.input_schema,
        outputs=[o.model_dump() for o in payload.outputs],
        steps=[s.model_dump() for s in payload.steps],
        # 关键：Agent 生成的流程一律 draft —— 要人批
        status="draft",
        created_by=user.id,
        version=1,
        agent_note=payload.note,
    )
    db.add(flow)
    await db.commit()
    await db.refresh(flow)
    return AgentDraftResponse(
        ok=True,
        flow_id=flow.id,
        version=flow.version,
        warnings=result["warnings"],
        layers=result["layers"],
    )


@router.post("/{flow_id}/trial", response_model=FlowRunResponse)
async def trial_flow(
    flow_id: str,
    params: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """试跑一条流程 —— **产物不进正式素材库**。

    Agent 生成的流程上线前得先看看效果。试跑真的调引擎、真的出图，
    但产物打上试跑标记，默认从素材库排除（`?trial=only` 能翻出来看）。

    **草稿也能试跑** —— 这正是试跑的用途：审批人要先看效果再批。
    """
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not _can_edit(flow, user):
        raise HTTPException(status_code=403, detail="只能试跑自己创建的流程")
    try:
        run = await submit_flow(db, flow, params or {}, user, is_trial=True)
    except FlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FlowRunResponse.model_validate(run)


@router.post("/{flow_id}/approve", response_model=AgentApproveResponse)
async def approve_flow(
    flow_id: str,
    payload: AgentApproveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """审批一份草稿 —— 批准上线或驳回。

    **谁能批**：作者本人或 admin。
    Agent 生成的流程作者是提交它的人 —— 他最清楚这份配置想干什么。

    批准 → status=active（能被运行台跑到）；
    驳回 → status=archived（留档，但不再出现在可用流程里）。

    审批意见写进 review_note，出问题时知道当时是怎么定的。
    """
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not _can_edit(flow, user):
        raise HTTPException(status_code=403, detail="只能审批自己创建的流程")
    if flow.status != "draft":
        return AgentApproveResponse(
            ok=False,
            flow_id=flow_id,
            status=flow.status,
            message=f"只有草稿需要审批（当前状态：{flow.status}）",
        )

    flow.status = "active" if payload.approve else "archived"
    flow.reviewed_by = user.id
    flow.reviewed_at = utcnow()
    flow.review_note = payload.note
    flow.version = (flow.version or 0) + 1
    await db.commit()
    await db.refresh(flow)
    return AgentApproveResponse(
        ok=True,
        flow_id=flow_id,
        status=flow.status,
        message="已批准上线" if payload.approve else "已驳回",
    )


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
    """新建流程。

    **服务端也校验**：编排器保存前会调 /validate，但接口不能依赖「调用方
    一定校验过」—— 直接调 API、脚本导入、Agent 都可能绕过界面。让坏配置
    进库，代价是跑到一半才失败（引擎的钱已经花了）。
    """
    steps = [s.model_dump() for s in payload.steps]
    outputs = [o.model_dump() for o in payload.outputs]
    templates, flows = await _validation_context(db)
    _reject_invalid_config(
        validate_flow_config(
            steps=steps,
            input_schema=payload.input_schema,
            outputs=outputs,
            templates=templates,
            flows=flows,
        ),
        action="创建",
    )

    flow = Flow(
        name=payload.name,
        description=payload.description,
        category=payload.category,
        icon=payload.icon,
        input_schema=payload.input_schema,
        outputs=outputs,
        steps=steps,
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
    """改流程。没传的字段不动。

    **状态不能从这里改**：draft → active 必须走审批接口。否则拿到编辑权限
    的人（作者本人）可以直接把 Agent 生成的草稿改成 active，审批留痕
    就成摆设了。归档同理走审批的驳回。
    """
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not _can_edit(flow, user):
        raise HTTPException(status_code=403, detail="只能改自己创建的流程")
    data = payload.model_dump(exclude_unset=True)
    if "steps" in data and data["steps"] is not None:
        data["steps"] = [s.model_dump() for s in payload.steps]

    # 只要动了会影响可执行性的字段，就重新校验一遍（和保存前同一道门）
    if any(key in data for key in ("steps", "input_schema", "outputs")):
        templates, flows = await _validation_context(db)
        _reject_invalid_config(
            validate_flow_config(
                steps=data.get("steps") if data.get("steps") is not None else list(flow.steps or []),
                input_schema=(
                    data.get("input_schema")
                    if data.get("input_schema") is not None
                    else dict(flow.input_schema or {})
                ),
                outputs=(
                    data.get("outputs")
                    if data.get("outputs") is not None
                    else list(flow.outputs or [])
                ),
                templates=templates,
                flows=flows,
                flow_id=flow.id,
            ),
            action="保存",
        )

    # 状态只由审批接口流转，普通编辑不能顺手把它改成 active
    data.pop("status", None)

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
    """删流程。运行记录保留（它们是历史，不是流程的一部分）。

    **被别的流程当子流程调用时不许删**：直接删会留下断链，那条父流程
    要到运行时才报「子流程不存在」，而且改不回来（步骤还在，引用没了）。
    提示用户先去改父流程，比事后排查便宜。
    """
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not _can_edit(flow, user):
        raise HTTPException(status_code=403, detail="只能删自己创建的流程")

    others = (
        await db.execute(select(Flow).where(Flow.id != flow_id))
    ).scalars().all()
    users = [
        other.name
        for other in others
        if other.status != "archived"
        and any(
            str(s.get("flow_id") or "") == flow_id for s in (other.steps or [])
        )
    ]
    if users:
        shown = "、".join(users[:3]) + (" 等" if len(users) > 3 else "")
        raise HTTPException(
            status_code=409,
            detail=f"流程「{shown}」正在调用它，先把那些步骤改掉再删",
        )

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

    # 引用重写之后必须再校验一遍：导出文件可能来自旧版本、被手改过，
    # 或者引用解析后仍然接不上。导入是写库操作，坏配置不能进来。
    templates, all_flows = await _validation_context(db)
    _reject_invalid_config(
        validate_flow_config(
            steps=data["steps"],
            input_schema=data["input_schema"],
            outputs=data["outputs"],
            templates=templates,
            flows=all_flows,
            flow_id=existing.id if existing is not None else "",
        ),
        action="导入",
    )

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
