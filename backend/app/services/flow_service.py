"""流程执行器 —— 一条流程 = 一串步骤，每步 = 一个现有节点。

**每一步跑成一个普通 Task**，所以并发闸门、进度轮询、失败重试、任务列表
全部复用现有实现，流程不需要自己的执行引擎。

第一刀的能力边界（数据模型一步到位，执行器是渐进的）：
  ✓ 顺序执行、步骤间产物自动注入
  ✗ 并行分层、循环、子流程嵌套、表达式管道

「产物自动注入」是这里唯一需要特殊处理的地方：上游产物是我们自己存储里
的文件，下游引擎不认，所以要先读字节、再交给下游引擎的 upload_input 换回
一个引擎侧的名字（和用户上传参考图走同一条路，见 services/engine_input.py）。
注意上下游引擎可以不同 —— 上游 ComfyUI 出图、下游供应商 API 图生图，
在流程里是常规用法。
"""
import asyncio
import logging

from app.database import async_session
from app.filestore import read_asset_bytes
from app.models.models import Asset, Flow, FlowRun, Task, Template
from app.services.engine_input import EngineInputError, push_bytes_to_engine
from app.services.flow_expr import FlowExprError, resolve_value
from app.services.random_fields import resolve_random_fields
from app.services.task_service import TaskService, spawn_background, spawn_task
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

_TERMINAL = ("done", "error", "cancelled")
# 一步等多久还没结束就放弃 —— 上限不是防慢，是防「永远等不到」：
# 引擎侧任务丢了/被外部清掉时，任务状态可能永远停在 running。
_STEP_TIMEOUT_SECONDS = 3600.0
_POLL_INTERVAL_SECONDS = 1.0


class FlowError(RuntimeError):
    """流程配置错或执行出错。消息面向使用者，不含内部细节。"""


def _field_map(input_schema: dict | list) -> dict:
    """节点的输入字段表 {字段名: 字段定义}。

    input_schema 历史上是裸 list，现在统一成 {"fields": [...]}，
    两种都认（库里可能还有老格式）。
    """
    if isinstance(input_schema, dict):
        fields = input_schema.get("fields", [])
    elif isinstance(input_schema, list):
        fields = input_schema
    else:
        fields = []
    return {
        str(f.get("name")): f
        for f in fields
        if isinstance(f, dict) and f.get("name")
    }


def _is_asset_list(value) -> bool:
    """判断一个值是不是「上游产物列表」。"""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, dict) and v.get("asset_id") for v in value)
    )


async def submit_flow(db, flow: Flow, params: dict, user) -> FlowRun:
    """创建运行记录并后台执行。返回刚创建的 FlowRun。"""
    run = FlowRun(
        flow_id=flow.id,
        flow_name=flow.name,
        created_by=user.id,
        params=dict(params or {}),
        status="running",
        progress=0.0,
        steps=[],
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await spawn_background(f"flow:{run.id}", _execute_flow(run.id))
    return run


async def _execute_flow(run_id: str) -> None:
    """后台执行一条流程。出错把原因写进 FlowRun，不往上抛。"""
    async with async_session() as db:
        run = await db.get(FlowRun, run_id)
        if run is None:
            return
        flow = await db.get(Flow, run.flow_id)
        if flow is None:
            run.status = "error"
            run.error_msg = "流程已不存在"
            run.finished_at = utcnow()
            await db.commit()
            return

        steps = list(flow.steps or [])
        ctx: dict = {"input": dict(run.params or {}), "steps": {}}
        state: list[dict] = []
        total = len(steps)

        for index, step in enumerate(steps):
            step_id = str(step.get("id") or f"step{index + 1}")
            try:
                resolved = await _resolve_step_params(step, ctx)
            except (FlowError, FlowExprError) as exc:
                await _fail_run(run_id, f"第 {index + 1} 步「{step_id}」：{exc}", state)
                return

            try:
                finished = await _run_step(run, step, step_id, resolved)
            except FlowError as exc:
                await _fail_run(run_id, f"第 {index + 1} 步「{step_id}」：{exc}", state)
                return

            if finished.status != "done":
                detail = finished.error_msg or f"任务状态 {finished.status}"
                await _fail_run(run_id, f"第 {index + 1} 步「{step_id}」失败：{detail}", state)
                return

            # 上游产物：既喂给下一步，也作为流程输出候选
            outputs = await _collect_outputs(finished.id)
            ctx["steps"][step_id] = {"outputs": outputs}
            state.append(
                {
                    "step_id": step_id,
                    "task_id": finished.id,
                    "status": finished.status,
                    "outputs": outputs,
                }
            )
            await _update_run(run_id, progress=(index + 1) / total * 100, steps=state)

        await _update_run(run_id, status="done", progress=100.0, steps=state)


async def _fail_run(run_id: str, message: str, state: list[dict]) -> None:
    async with async_session() as db:
        run = await db.get(FlowRun, run_id)
        if run is None:
            return
        run.status = "error"
        run.error_msg = message
        run.steps = state
        run.finished_at = utcnow()
        await db.commit()


async def _update_run(
    run_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    steps: list[dict] | None = None,
) -> None:
    async with async_session() as db:
        run = await db.get(FlowRun, run_id)
        if run is None:
            return
        if status is not None:
            run.status = status
        if progress is not None:
            run.progress = progress
        if steps is not None:
            run.steps = steps
        if run.status in ("done", "error", "cancelled"):
            run.finished_at = utcnow()
        await db.commit()


async def _resolve_step_params(step: dict, ctx: dict) -> dict:
    """把步骤的 params 表达式解析成实际参数值。

    上游产物引用（值是产物列表）要送进本步节点的引擎，换回引擎侧名字；
    字段不是文件类型却引用了产物，是配置错误 —— 直接报出来，别把一堆
    素材路径塞给引擎让它自己失败。
    """
    template_id = str(step.get("template_id") or "")
    if not template_id:
        raise FlowError("步骤缺少 template_id")

    async with async_session() as db:
        template = await db.get(Template, template_id)
        if template is None:
            raise FlowError(f"节点不存在：{template_id}")
        fields = _field_map(template.input_schema)
        engine = template.engine

    resolved: dict = {}
    for field, expr in (step.get("params") or {}).items():
        value = resolve_value(expr, ctx)
        spec = fields.get(field)
        if spec is None:
            raise FlowError(
                f"字段「{field}」在节点「{template.name}」里不存在"
                f"（可用的：{', '.join(sorted(fields)) or '无'}）"
            )
        if spec.get("type") == "file_upload":
            # 文件类型的字段，值统一是「素材引用」：要么是素材 id（用户传的），
            # 要么是上游产物列表。两种都要送进本步节点的引擎换回引擎侧名字 ——
            # 我们自己存储里的路径，引擎不认。
            value = await _push_to_engine(engine, value, spec)
        elif _is_asset_list(value):
            raise FlowError(
                f"字段「{field}」的类型是 {spec.get('type')}，不能接上游产物"
                "（只有 file_upload 类型的字段能接）"
            )
        resolved[field] = value
    return resolved


async def _push_to_engine(engine: str, value, spec: dict):
    """把素材引用推进引擎，返回引擎侧文件名（多值字段返回列表）。"""
    if isinstance(value, str) and value:
        items = [{"asset_id": value}]
    elif _is_asset_list(value):
        items = value
    else:
        raise FlowError("文件字段没有可用的素材引用")

    names: list[str] = []
    async with async_session() as db:
        for item in items:
            asset = await db.get(Asset, str(item.get("asset_id")))
            if asset is None:
                raise FlowError(f"素材不存在：{item.get('asset_id')}")
            data = read_asset_bytes(asset.file_path)
            if data is None:
                raise FlowError(f"素材读不出来：{asset.file_name}")
            try:
                names.append(await push_bytes_to_engine(engine, data, asset.file_name))
            except EngineInputError as exc:
                raise FlowError(f"素材送进引擎失败：{exc}") from exc
    if spec.get("multiple"):
        return names
    if len(names) > 1:
        raise FlowError(
            f"字段只接一张图，但上游产出了 {len(names)} 张"
            "（节点字段要标成多值才能接多张）"
        )
    return names[0] if names else ""


async def _run_step(run: FlowRun, step: dict, step_id: str, params: dict) -> Task:
    """建任务、启动、等它跑完。返回终态任务。"""
    template_id = str(step.get("template_id") or "")
    async with async_session() as db:
        template = await db.get(Template, template_id)
        if template is None:
            raise FlowError(f"节点不存在：{template_id}")
        # random_number 字段填 -1 = 每次生成自动换一个：逐任务展开，
        # 和单模板提交路径同一套逻辑（批量 N 张各用不同种子）
        final_params = resolve_random_fields(template.input_schema, params)
        task = await TaskService.create_task(
            db,
            template_id,
            final_params,
            created_by=run.created_by,
            flow_run_id=run.id,
            step_id=step_id,
        )
        task_id = task.id

    await spawn_task(task_id)
    return await _wait_task(task_id)


async def _wait_task(task_id: str) -> Task:
    """等任务到终态。

    每次轮询开一个短会话 —— 流程可能跑几分钟，长会话会把连接池占住，
    而连接池是给 50 个人用的（pool_size = 并发数 + 5）。
    """
    waited = 0.0
    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
        async with async_session() as db:
            task = await db.get(Task, task_id)
            if task is None:
                raise FlowError("任务记录消失")
            if task.status in _TERMINAL:
                return task
        if waited >= _STEP_TIMEOUT_SECONDS:
            raise FlowError(f"等待超时（{int(_STEP_TIMEOUT_SECONDS)} 秒）")


async def _collect_outputs(task_id: str) -> list[dict]:
    """取一个任务产出的素材，压成流程内部可引用的形状。"""
    async with async_session() as db:
        from sqlalchemy import select

        rows = (
            await db.execute(
                select(Asset).where(Asset.task_id == task_id, Asset.deleted_at.is_(None))
            )
        ).scalars().all()
        return [
            {
                "asset_id": a.id,
                "filename": a.file_name,
                "mime_type": a.mime_type,
            }
            for a in rows
        ]
