"""流程执行器 —— 一条流程 = 一组步骤，每步 = 一个现有节点。

**每一步跑成一个普通 Task**，所以并发闸门、进度轮询、失败重试、任务列表
全部复用现有实现，流程不需要自己的执行引擎。

能力边界（数据模型一步到位，执行器是渐进的）：
  ✓ 依赖分层并行（同层步骤一起提交）
  ✓ 循环（repeat，按列表长度或固定次数展开）
  ✓ 子流程嵌套（递归执行，产出按名字暴露）
  ✓ 表达式管道 / 条件

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
from app.services.flow_expr import FlowExprError, collect_refs, resolve_value
from app.services.field_types import is_file_field, normalized_field_map
from app.services.flow_validate import compute_layers, step_label
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


# 循环次数上限 —— 防止一条流程把引擎队列塞爆。
# 上限定 200：够覆盖「一集 20 镜头」这类场景，再多就该拆流程了。
MAX_LOOP_COUNT = 200


def _is_asset_list(value) -> bool:
    """判断一个值是不是「上游产物列表」。"""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, dict) and v.get("asset_id") for v in value)
    )


def _is_asset_ref(value) -> bool:
    """单个产物引用 —— 表达式写了 .first / .last 时求值出来是这个形状。"""
    return isinstance(value, dict) and bool(value.get("asset_id"))


async def submit_flow(db, flow: Flow, params: dict, user, is_trial: bool = False) -> FlowRun:
    """创建运行记录并后台执行。返回刚创建的 FlowRun。

    is_trial = 试跑（Agent 生成的流程上线前先跑一遍看看效果）。
    试跑真的调引擎、真的出图，但产物打上标记，不进正式素材库。
    """
    run = FlowRun(
        flow_id=flow.id,
        flow_name=flow.name,
        created_by=user.id,
        params=dict(params or {}),
        status="running",
        progress=0.0,
        steps=[],
        is_trial=is_trial,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await spawn_background(f"flow:{run.id}", _execute_flow(run.id))
    return run


async def _execute_flow(run_id: str) -> None:
    """后台执行入口 —— **兜住所有异常**，别让运行记录永远停在 running。

    spawn_background 只把异常写日志、不会改运行状态；如果数据库连接断了、
    磁盘读失败、数据形状没预料到，异常逃出去后前端就一直转圈，运营只能
    重跑一条新的。这里统一落成 error，并保留已经写进去的步骤进度。
    """
    try:
        await _run_flow(run_id)
    except asyncio.CancelledError:
        # 进程停机时的协作式取消：状态由停机流程处理，不能当成业务失败
        raise
    except FlowError as exc:
        await _mark_run_error(run_id, str(exc))
    except Exception:
        logger.exception("流程执行异常 run_id=%s", run_id)
        await _mark_run_error(
            run_id, "流程执行出错（内部错误），请联系管理员查看服务端日志"
        )


async def _run_flow(run_id: str) -> None:
    """真正的执行过程。配置/引擎错误抛 FlowError，由入口统一落库。

    **开头用短会话读完就释放**：整个执行过程可能跑几分钟，长会话会把
    连接池占住（池是给 50 个人用的），而且它持有的读事务会挡住后面
    `_update_run` 的写入 —— 状态写不进去，流程永远卡在 running。
    """
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
        params = dict(run.params or {})

    ctx: dict = {"input": params, "steps": {}}
    state: list[dict] = []
    total = len(steps)
    # 同一次运行里，(引擎, 素材) → 引擎侧名字。循环 200 次用同一张参考图时，
    # 不能把同一张图往引擎传 200 遍 —— 上传是网络 + 磁盘开销，纯浪费。
    input_cache: dict[tuple[str, str], str] = {}

    # 按依赖分层：同层的步骤互不依赖，一起提交、一起等。
    # 「谁吃谁」是流程声明的，「谁能同时跑」是算出来的 —— 不让用户配并行组，
    # 因为并行组配错了（把有依赖的放进同一组）会拿不到产物，而且错得很隐蔽。
    layers, cycle = compute_layers(steps)
    if cycle:
        await _fail_run(run_id, f"步骤之间存在循环依赖：{', '.join(cycle)}", state)
        return
    if not layers:
        await _update_run(run_id, status="done", progress=100.0, steps=state)
        return

    by_id = {str(s.get("id") or ""): s for s in steps}
    # 进度按「完成的步骤数」算，不按任务数 —— 循环一步会展开成 N 个任务，
    # 按任务数算会超过 100%（一步循环 10 次 = 1000%）。
    done_steps: set[str] = set()

    for layer_index, layer_ids in enumerate(layers):
        layer_steps = [(sid, by_id[sid]) for sid in layer_ids if sid in by_id]
        if not layer_steps:
            continue

        # ── 先把整层都提交出去（这一步不等待，任务在后台跑）──
        # 循环步骤在这一层展开成 N 个实例（N 个任务一起提交，并行跑）
        # 子流程步骤起一个协程、**不在这里等**：等它就等于把整层堵成串行
        started: list[tuple[str, str, int]] = []
        subflow_jobs: list[tuple[str, dict, asyncio.Task]] = []
        for step_id, step in layer_steps:
            step_type = str(step.get("type") or "template")
            if step_type == "flow":
                job = asyncio.create_task(
                    _run_subflow(
                        run,
                        step,
                        step_id,
                        ctx,
                        depth=0,
                        ancestors=(flow.id,),
                        input_cache=input_cache,
                    )
                )
                subflow_jobs.append((step_id, step, job))
                continue

            try:
                instances = await _expand_instances(step, step_id, ctx)
            except (FlowError, FlowExprError) as exc:
                _cancel_jobs(subflow_jobs)
                await _fail_run(run_id, f"{step_label(steps, step_id)}：{exc}", state)
                return

            for iteration, iteration_ctx in instances:
                try:
                    resolved = await _resolve_step_params(
                        step, iteration_ctx, input_cache
                    )
                except (FlowError, FlowExprError) as exc:
                    _cancel_jobs(subflow_jobs)
                    await _fail_run(
                        run_id,
                        f"{step_label(steps, step_id)}第 {iteration + 1} 次：{exc}",
                        state,
                    )
                    return
                try:
                    task_id = await _start_step(run, step, step_id, resolved)
                except FlowError as exc:
                    _cancel_jobs(subflow_jobs)
                    await _fail_run(
                        run_id,
                        f"{step_label(steps, step_id)}第 {iteration + 1} 次：{exc}",
                        state,
                    )
                    return
                started.append((step_id, task_id, iteration))

        # ── 等这一层的子流程（普通节点此刻已经在引擎队列里跑）──
        for step_id, _step, job in subflow_jobs:
            try:
                sub_ctx, sub_state = await job
            except FlowError as exc:
                _cancel_jobs(subflow_jobs)
                await _fail_run(run_id, f"{step_label(steps, step_id)}：{exc}", state)
                return
            except Exception:
                _cancel_jobs(subflow_jobs)
                raise
            # 只把「产物」暴露给父流程 —— 子流程的 input / steps 是它
            # 内部的事，父流程不该读到（读了也没意义，引用路径也不同）。
            ctx["steps"][step_id] = _expose_subflow(sub_ctx)
            state.append(
                {
                    "step_id": step_id,
                    "task_id": "",
                    "status": "done",
                    "layer": layer_index,
                    "iteration": 0,
                    "outputs": _subflow_outputs(sub_ctx),
                    "subflow": sub_state,
                }
            )
            done_steps.add(step_id)

        # ── 再等这一层的普通任务全部跑完 ──
        finished: list[tuple[str, Task, int]] = []
        for step_id, task_id, iteration in started:
            try:
                finished.append((step_id, await _wait_task(task_id), iteration))
            except FlowError as exc:
                await _fail_run(run_id, f"{step_label(steps, step_id)}：{exc}", state)
                return

        # 同层一步失败，同层其他步骤的产物仍然入库（跑都跑了，别浪费），
        # 但后续层不执行 —— 它们拿不到这一步的产物。
        failed = [(sid, t) for sid, t, _ in finished if t.status != "done"]
        # 同一循环步骤的 N 个实例，产物汇总成一份给下游
        merged: dict[str, list[dict]] = {}
        for step_id, task, iteration in finished:
            outputs = await _collect_outputs(task.id)
            merged.setdefault(step_id, []).extend(outputs)
            state.append(
                {
                    "step_id": step_id,
                    "task_id": task.id,
                    "status": task.status,
                    "layer": layer_index,
                    "iteration": iteration,
                    "outputs": outputs,
                }
            )
        for step_id, outputs in merged.items():
            ctx["steps"][step_id] = {"outputs": outputs}
            done_steps.add(step_id)
        progress = min(100.0, len(done_steps) / total * 100) if total else 100.0
        await _update_run(run_id, progress=progress, steps=state)

        if failed:
            step_id, task = failed[0]
            detail = task.error_msg or f"任务状态 {task.status}"
            await _fail_run(
                run_id,
                f"{step_label(steps, step_id)}失败：{detail}",
                state,
            )
            return

    # 流程级产出：按声明解析
    final_outputs = _resolve_flow_outputs(flow.outputs or [], ctx, steps)
    await _update_run(
        run_id,
        status="done",
        progress=100.0,
        steps=state,
        outputs=final_outputs,
    )


def _resolve_flow_outputs(declarations: list, ctx: dict, steps: list | None = None) -> list[dict]:
    """按流程的输出声明解析出流程级产物。

    声明形如 `{name, from_step, pick}`：
    - name      产出名（父流程按这个名字引用）
    - from_step 来源步骤 id
    - pick      取哪部分：all / first / last

    **没声明时默认交付最后一步的产物** —— 大多数流程就是「跑完拿结果」，
    逼用户先声明一遍才能用是本末倒置。

    **「最后一步」按列表顺序找，不按执行完成顺序找**：同层步骤是并行跑的，
    ctx 的插入顺序由谁先跑完决定 —— 拿它当「最后一步」会时好时坏。
    """
    step_outputs = ctx.get("steps") or {}
    if not declarations:
        order = [str(s.get("id") or "") for s in (steps or [])]
        if not order:
            order = list(step_outputs.keys())
        for step_id in reversed(order):
            outs = (step_outputs.get(step_id) or {}).get("outputs") or []
            if outs:
                return [
                    {
                        "name": "output",
                        "pick": "all",
                        "outputs": outs,
                    }
                ]
        return []

    resolved: list[dict] = []
    for decl in declarations:
        name = str(decl.get("name") or "").strip()
        src = str(decl.get("from_step") or decl.get("from") or "").strip()
        pick = str(decl.get("pick") or "all").strip()
        if not name or not src:
            continue
        outs = (step_outputs.get(src) or {}).get("outputs") or []
        if pick == "first" and outs:
            outs = outs[:1]
        elif pick == "last" and outs:
            outs = outs[-1:]
        resolved.append({"name": name, "pick": pick, "outputs": outs})
    return resolved


# 子流程嵌套的最大深度 —— 防流程 A 调 B、B 又调 A 的无限递归。
# 5 层够覆盖「主流程 → 集子流程 → 分镜子流程 → 镜头子流程」这类结构。
MAX_SUBFLOW_DEPTH = 5


def _subflow_outputs(sub_ctx: dict) -> list[dict]:
    """子流程的产物汇总 —— 它的每个具名产出都摊平成列表。"""
    outs: list[dict] = []
    for value in (sub_ctx.get("named") or {}).values():
        if isinstance(value, list):
            outs.extend(value)
        elif value:
            outs.append(value)
    return outs


def _expose_subflow(sub_ctx: dict) -> dict:
    """子流程对父流程暴露的形状。

    只暴露产物：input / steps 是子流程内部的事，父流程引用它们没有意义。
    具名产出摊在顶层，父流程写 `{{ steps.sub.final_image }}` 就能拿到。

    **父流程和嵌套子流程必须共用这一个函数** —— 之前两处各写一遍，
    嵌套那处漏了摊平，导致「子流程里再调子流程」时具名产出引用不到。
    """
    exposed: dict = {"outputs": _subflow_outputs(sub_ctx)}
    exposed.update(sub_ctx.get("named") or {})
    return exposed


def _cancel_jobs(jobs: list[tuple[str, dict, asyncio.Task]]) -> None:
    """失败收尾：取消同层还没跑完的子流程。

    子流程内部已经提交的引擎任务取消不掉（和同层普通任务一样，跑都跑了），
    但至少不会继续往下提交新的层。
    """
    for _sid, _step, job in jobs:
        if not job.done():
            job.cancel()


async def _run_subflow(
    run: FlowRun,
    step: dict,
    step_id: str,
    parent_ctx: dict,
    depth: int,
    ancestors: tuple[str, ...] = (),
    input_cache: dict[tuple[str, str], str] | None = None,
) -> tuple[dict, list[dict]]:
    """执行一条子流程。返回 (子流程的上下文, 子流程的步骤状态)。

    **输入怎么传**：同名自动取作默认，显式绑定作覆盖 —— 和步骤字段一个规则，
    不引入新概念。子流程的输入字段叫「风格」，父流程的输入里也有「风格」
    就自动对上；父流程想传别的值，就在这一步的 params 里显式写。

    **输出怎么被引用**：子流程的 outputs 声明（{name, from_step, pick}）
    解析出来的具名产出，放进 ctx["named"][name]。父流程写
    `{{ steps.<步骤id>.final_image }}` 就能拿到。
    """
    if depth >= MAX_SUBFLOW_DEPTH:
        raise FlowError(
            f"子流程嵌套超过 {MAX_SUBFLOW_DEPTH} 层"
            "（可能是流程互相调用形成了环）"
        )

    flow_id = str(step.get("flow_id") or "")
    if not flow_id:
        raise FlowError("子流程步骤没有指定要调用哪条流程")

    async with async_session() as db:
        sub_flow = await db.get(Flow, flow_id)
        if sub_flow is None:
            raise FlowError(f"子流程不存在：{flow_id}")
        if sub_flow.id == run.flow_id:
            raise FlowError("子流程不能调用自己")
        # A → B → C → B 这种环：只查「是不是根流程」查不出来，
        # 会一直递归到深度上限才报错，文案还只能说「可能是环」。
        if sub_flow.id in ancestors:
            chain = " → ".join([*ancestors, sub_flow.id])
            raise FlowError(f"子流程互相调用形成了环：{chain}")
        sub_steps = list(sub_flow.steps or [])
        sub_outputs_decl = list(sub_flow.outputs or [])
        sub_input_schema = dict(sub_flow.input_schema or {})
        sub_name = sub_flow.name

    if input_cache is None:
        input_cache = {}

    # ── 组装子流程的输入 ──
    sub_params = _build_subflow_params(sub_input_schema, step, parent_ctx)

    # 子流程在自己的上下文里跑 —— 步骤 id 与父流程独立
    sub_ctx: dict = {"input": sub_params, "steps": {}, "named": {}}
    sub_state: list[dict] = []

    layers, cycle = compute_layers(sub_steps)
    if cycle:
        raise FlowError(f"子流程「{sub_name}」内部存在循环依赖")
    if not layers:
        return sub_ctx, sub_state

    by_id = {str(s.get("id") or ""): s for s in sub_steps}

    for layer_index, layer_ids in enumerate(layers):
        layer_steps = [(sid, by_id[sid]) for sid in layer_ids if sid in by_id]
        if not layer_steps:
            continue

        prefix = f"子流程「{sub_name}」的"
        started: list[tuple[str, str, int]] = []
        subflow_jobs: list[tuple[str, dict, asyncio.Task]] = []
        for sid, sstep in layer_steps:
            if str(sstep.get("type") or "template") == "flow":
                job = asyncio.create_task(
                    _run_subflow(
                        run,
                        sstep,
                        sid,
                        sub_ctx,
                        depth=depth + 1,
                        ancestors=(*ancestors, sub_flow.id),
                        input_cache=input_cache,
                    )
                )
                subflow_jobs.append((sid, sstep, job))
                continue

            try:
                instances = await _expand_instances(sstep, sid, sub_ctx)
            except (FlowError, FlowExprError) as exc:
                _cancel_jobs(subflow_jobs)
                raise FlowError(f"{prefix}{step_label(sub_steps, sid)}：{exc}") from exc

            for iteration, iteration_ctx in instances:
                try:
                    resolved = await _resolve_step_params(
                        sstep, iteration_ctx, input_cache
                    )
                except (FlowError, FlowExprError) as exc:
                    _cancel_jobs(subflow_jobs)
                    raise FlowError(
                        f"{prefix}{step_label(sub_steps, sid)}第 {iteration + 1} 次：{exc}"
                    ) from exc
                try:
                    task_id = await _start_step(run, sstep, sid, resolved)
                except FlowError as exc:
                    _cancel_jobs(subflow_jobs)
                    raise FlowError(
                        f"{prefix}{step_label(sub_steps, sid)}第 {iteration + 1} 次：{exc}"
                    ) from exc
                started.append((sid, task_id, iteration))

        for sid, _sstep, job in subflow_jobs:
            try:
                sub2_ctx, sub2_state = await job
            except FlowError as exc:
                _cancel_jobs(subflow_jobs)
                raise FlowError(f"{prefix}{step_label(sub_steps, sid)}：{exc}") from exc
            except Exception:
                _cancel_jobs(subflow_jobs)
                raise
            # 和父流程同一套暴露规则 —— 具名产出摊在 steps.<id> 下，
            # 否则「子流程里再调子流程」时 {{ steps.x.final }} 取不到。
            sub_ctx["steps"][sid] = _expose_subflow(sub2_ctx)
            sub_state.append(
                {
                    "step_id": sid,
                    "task_id": "",
                    "status": "done",
                    "layer": layer_index,
                    "iteration": 0,
                    "outputs": _subflow_outputs(sub2_ctx),
                    "subflow": sub2_state,
                }
            )

        finished: list[tuple[str, Task, int]] = []
        for sid, task_id, iteration in started:
            try:
                finished.append((sid, await _wait_task(task_id), iteration))
            except FlowError as exc:
                raise FlowError(f"{prefix}{step_label(sub_steps, sid)}：{exc}") from exc

        failed = [(sid, t) for sid, t, _ in finished if t.status != "done"]
        merged: dict[str, list[dict]] = {}
        for sid, task, iteration in finished:
            outs = await _collect_outputs(task.id)
            merged.setdefault(sid, []).extend(outs)
            sub_state.append(
                {
                    "step_id": sid,
                    "task_id": task.id,
                    "status": task.status,
                    "layer": layer_index,
                    "iteration": iteration,
                    "outputs": outs,
                }
            )
        for sid, outs in merged.items():
            sub_ctx["steps"][sid] = {"outputs": outs}

        if failed:
            sid, task = failed[0]
            detail = task.error_msg or f"任务状态 {task.status}"
            raise FlowError(f"{prefix}{step_label(sub_steps, sid)}失败：{detail}")

    # ── 子流程的具名产出 ──
    for item in _resolve_flow_outputs(sub_outputs_decl, sub_ctx, sub_steps):
        sub_ctx["named"][item["name"]] = item["outputs"]
    if not sub_ctx["named"]:
        # 没声明产出 → 默认最后一步的产物，名字就叫 output
        last = _resolve_flow_outputs([], sub_ctx, sub_steps)
        if last:
            sub_ctx["named"]["output"] = last[0]["outputs"]
    return sub_ctx, sub_state


def _build_subflow_params(input_schema: dict, step: dict, parent_ctx: dict) -> dict:
    """组装传给子流程的参数。

    **同名自动取作默认，显式绑定作覆盖**：
    - 子流程的输入字段叫「风格」，父流程上下文里也有「风格」→ 自动对上
    - 父流程想传别的 → 在这一步的 params 里显式写 `{"风格": "{{ input.画风 }}"}`

    和步骤字段用同一套规则，不引入新概念。
    """
    fields = input_schema.get("fields") if isinstance(input_schema, dict) else []
    fields = fields if isinstance(fields, list) else []
    parent_input = parent_ctx.get("input") or {}

    params: dict = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "")
        if not name:
            continue
        # 同名自动取
        if name in parent_input:
            params[name] = parent_input[name]

    # 显式绑定覆盖
    for field, expr in (step.get("params") or {}).items():
        try:
            params[field] = resolve_value(expr, parent_ctx)
        except FlowExprError as exc:
            raise FlowError(f"子流程参数「{field}」算不出来：{exc}") from exc
    return params


async def _expand_instances(step: dict, step_id: str, ctx: dict) -> list[tuple[int, dict]]:
    """把一个步骤展开成要跑几次。

    普通步骤 → 一次，上下文原样。
    循环步骤（type=repeat）→ N 次，每次的上下文里带上循环变量：
      index  当前是第几次（从 0 开始）
      total  一共跑几次
      item   当前这次对应的那一项（列表输入时）

    **为什么循环必须每次输入不同**：现有节点一次只出 1 个产物，跑 N 次
    但每次参数一样，等于跑 N 遍同样的活。所以循环的价值全在
    「每次喂不同的提示词」—— 那就需要一个列表输入 + 索引。
    """
    step_type = str(step.get("type") or "template")
    if step_type != "repeat":
        return [(0, ctx)]

    count_expr = step.get("count_expr") or ""
    # count = 0 的含义是「由列表长度决定」—— 不能写成 `or 1`，那会把 0 变成 1
    raw_count = step.get("count")
    count = 0 if raw_count is None else int(raw_count)
    # 列表输入：次数由列表长度决定（省掉一个要填的数）
    source = _loop_source(step, ctx)
    try:
        if isinstance(count_expr, str) and count_expr.strip():
            resolved = resolve_value(count_expr, ctx)
            count = int(resolved)
        elif source is not None and count <= 0:
            count = len(source)
        else:
            count = int(count)
    except (TypeError, ValueError) as exc:
        raise FlowError(f"循环次数不是整数：{count_expr!r}") from exc
    except FlowExprError as exc:
        raise FlowError(f"循环次数算不出来：{exc}") from exc

    if count < 1:
        raise FlowError(f"循环次数至少是 1（现在 {count}）")
    if count > MAX_LOOP_COUNT:
        raise FlowError(f"循环次数不能超过 {MAX_LOOP_COUNT}（现在 {count}）")

    instances: list[tuple[int, dict]] = []
    for i in range(count):
        iteration_ctx = dict(ctx)
        iteration_ctx["index"] = i
        iteration_ctx["total"] = count
        if source is not None:
            if i >= len(source):
                raise FlowError(
                    f"循环要跑 {count} 次，但列表只有 {len(source)} 项"
                    "（次数不能超过列表长度）"
                )
            iteration_ctx["item"] = source[i]
        else:
            # 没有列表来源时 item 就是序号 —— 别让 {{ item }} 引用不到东西
            iteration_ctx["item"] = i
        instances.append((i, iteration_ctx))
    return instances


# 循环列表来源的识别：`input.<字段>[index]`
def _loop_source(step: dict, ctx: dict) -> list | None:
    """循环每次喂的那一项 —— 来自流程输入里的列表字段。

    **从表达式的结构识别，不从求值结果猜。**
    `{{ input.prompts[index] }}` 求值出来是「第 i 项」——一个字符串，
    不是列表。所以不能靠「哪个 param 求值结果是 list」来判断来源。

    约定：param 里写了 `input.<字段>[...]` 这种形式，那个 `<字段>`
    就是列表来源。循环次数默认由它的长度决定（count 留 0 即可）。

    **用 collect_refs 而不是正则**：    字段名允许中文，正则里的
    `[A-Za-z_]` 会漏掉 `input.提示词列表[index]`（校验和执行都会错）。
    路径解析器已经支持中文标识符，结构识别就该复用它。
    """
    inputs = ctx.get("input") or {}
    for expr in (step.get("params") or {}).values():
        if not isinstance(expr, str) or "{{" not in expr:
            continue
        for ref in collect_refs(expr):
            if not ref.startswith("input.") or "[" not in ref:
                continue
            name = ref[len("input.") :].split("[", 1)[0]
            value = inputs.get(name)
            if isinstance(value, list):
                return value
    return None


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


async def _mark_run_error(run_id: str, message: str) -> None:
    """把运行标记为失败，**不动已写入的步骤进度**。

    内部异常可能在任何一行抛出，手里没有可信的 state —— 不能拿一个空列表
    把用户已经看到的进度覆盖掉。已经结束的运行也不改（不能让收尾异常
    把成功改写成失败）。
    """
    async with async_session() as db:
        run = await db.get(FlowRun, run_id)
        if run is None or run.status in ("done", "cancelled"):
            return
        run.status = "error"
        run.error_msg = message
        run.finished_at = utcnow()
        await db.commit()


async def _update_run(
    run_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    steps: list[dict] | None = None,
    outputs: list[dict] | None = None,
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
        if outputs is not None:
            run.outputs = outputs
        if run.status in ("done", "error", "cancelled"):
            run.finished_at = utcnow()
        await db.commit()


async def _resolve_step_params(
    step: dict,
    ctx: dict,
    input_cache: dict[tuple[str, str], str] | None = None,
) -> dict:
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
        fields = normalized_field_map(template.input_schema)
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
        if is_file_field(spec):
            # 文件类型的字段，值统一是「素材引用」：要么是素材 id（用户传的），
            # 要么是上游产物列表。两种都要送进本步节点的引擎换回引擎侧名字 ——
            # 我们自己存储里的路径，引擎不认。
            value = await _push_to_engine(engine, value, spec, input_cache)
        elif _is_asset_list(value) or _is_asset_ref(value):
            raise FlowError(
                f"字段「{field}」的类型是 {spec.get('type')}，不能接上游产物"
                "（只有 file_upload 类型的字段能接）"
            )
        resolved[field] = value
    return resolved


async def _push_to_engine(
    engine: str,
    value,
    spec: dict,
    cache: dict[tuple[str, str], str] | None = None,
):
    """把素材引用推进引擎，返回引擎侧文件名（多值字段返回列表）。

    cache 是**一次运行内**的映射：(引擎, 素材 id) → 引擎侧名字。
    循环里每轮吃同一张参考图是常规用法，不缓存就会把同一张图上传 N 次。
    """
    if isinstance(value, str) and value:
        items = [{"asset_id": value}]
    elif _is_asset_ref(value):
        # 表达式取了 .first / .last —— 单个产物
        items = [value]
    elif _is_asset_list(value):
        items = value
    else:
        raise FlowError("文件字段没有可用的素材引用")

    cache = cache if cache is not None else {}
    names: list[str] = []
    async with async_session() as db:
        for item in items:
            asset_id = str(item.get("asset_id"))
            key = (engine, asset_id)
            cached = cache.get(key)
            if cached is not None:
                names.append(cached)
                continue
            asset = await db.get(Asset, asset_id)
            if asset is None:
                raise FlowError(f"素材不存在：{asset_id}")
            data = read_asset_bytes(asset.file_path)
            if data is None:
                raise FlowError(f"素材读不出来：{asset.file_name}")
            try:
                pushed = await push_bytes_to_engine(engine, data, asset.file_name)
            except EngineInputError as exc:
                raise FlowError(f"素材送进引擎失败：{exc}") from exc
            cache[key] = pushed
            names.append(pushed)
    if spec.get("multiple"):
        return names
    if len(names) > 1:
        raise FlowError(
            f"字段只接一张图，但上游产出了 {len(names)} 张"
            "（节点字段要标成多值才能接多张）"
        )
    return names[0] if names else ""


async def _start_step(run: FlowRun, step: dict, step_id: str, params: dict) -> str:
    """建任务并启动，**不等它跑完** —— 返回 task_id。

    分开是为了并行：同层的步骤要全部先提交出去，再一起等。
    真正限制并发的是引擎侧的闸门（按引擎分池），这里只管尽快把任务送进队列。
    """
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
            is_trial=bool(run.is_trial),
        )
        task_id = task.id

    await spawn_task(task_id)
    return task_id


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
