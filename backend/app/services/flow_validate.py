"""流程配置校验 + 依赖分层。

编排器保存前、Agent 生成后走的是同一道门 —— 所以它必须在服务层，
不能是某个界面的私有逻辑。

检查项分两档：
- error：这份配置跑不起来（引用了不存在的节点/字段、环、文件字段填了字面量）
- warning：能跑但可能有问题（引用了列表里排在后面的步骤 —— 执行按依赖分层，
  会自动提前，但列表顺序和实际执行顺序不一致，容易误读）

依赖分层用 Kahn 算法：同一层的步骤互不依赖，可以并行。执行器直接用这个
分层结果，不再自己算一遍。
"""
from app.models.models import Template
from app.services.field_types import is_file_field, normalized_field_map
from app.services.flow_expr import collect_base_refs, collect_refs, expr_problems


# 循环步骤里才有的变量
_LOOP_VARS = {"index", "total", "item"}


def _field_map(input_schema) -> dict:
    """归一后的字段表 —— image_upload 这类历史别名在这里统一成 file_upload。

    直接读库里的原始 schema 会让老模板的 image_upload 字段既接不了上游
    产物，校验还会误报「只有 file_upload 类型的字段能接」。
    """
    return normalized_field_map(input_schema)


def _input_field_names(input_schema) -> set[str]:
    return set(_field_map(input_schema))


def _has_list_source(step: dict) -> bool:
    """这一步的参数里引了「列表[序号]」吗 —— 那种情况下次数由列表长度决定。

    用引用解析而不是正则：字段名允许中文，正则写 `[A-Za-z_]` 会漏掉
    `input.提示词列表[index]`，于是合法的循环配置被误报「没找到列表来源」。
    """
    for expr in (step.get("params") or {}).values():
        if not isinstance(expr, str):
            continue
        for ref in collect_refs(expr):
            if ref.startswith("input.") and "[" in ref:
                return True
    return False


def _refs_upstream(expr) -> bool:
    """这个取值引用了上游步骤的产物吗。"""
    if not isinstance(expr, str):
        return False
    return any(ref.startswith("steps.") for ref in collect_refs(expr))


def _expr_errors(expr) -> list[str]:
    """表达式本身的问题（语法错、函数不存在、参数个数不对）。

    以前这些错误只在跑到那一步时才暴露 —— 引擎都花钱了才发现白跑。
    现在挪到保存流程时，配置写错当场就能改。
    """
    return expr_problems(expr)


def step_label(steps: list[dict], sid: str) -> str:
    """步骤在界面上的名字 —— 优先用显示名（title），退回 id。

    校验错误要给内容同事看，「步骤「gen」的字段…」这种裸 id 没人认得；
    有显示名就说「第 2 步「生成参考图」的字段…」。
    """
    for index, step in enumerate(steps):
        if str(step.get("id") or "") != sid:
            continue
        title = str(step.get("title") or "").strip()
        return f"第 {index + 1} 步「{title}」" if title else f"第 {index + 1} 步"
    return f"步骤「{sid}」"


def compute_layers(steps: list[dict]) -> tuple[list[list[str]], list[str]]:
    """按依赖分层。返回 (分层结果, 环里的步骤 id)。

    分层：每一步的依赖 = 它引用的上游步骤 id。第 0 层没有依赖。
    """
    ids = [str(s.get("id") or "") for s in steps]
    deps: dict[str, set[str]] = {sid: set() for sid in ids}
    for step in steps:
        sid = str(step.get("id") or "")
        for _field, expr in (step.get("params") or {}).items():
            for ref in collect_refs(expr):
                if not ref.startswith("steps."):
                    continue
                parts = ref.split(".")
                if len(parts) >= 2:
                    deps[sid].add(parts[1])

    layers: list[list[str]] = []
    done: set[str] = set()
    pending = {sid: set(d) & set(ids) for sid, d in deps.items()}
    while pending:
        # 依赖都已完成的步骤就是这一层的成员 —— 同层互不依赖，可以并行
        ready = sorted(sid for sid, d in pending.items() if d <= done)
        if not ready:
            # 剩下的都在等对方 —— 成环
            return layers, sorted(pending)
        layers.append(ready)
        done.update(ready)
        for sid in ready:
            pending.pop(sid, None)
    return layers, []


def validate_flow_config(
    *,
    steps: list[dict],
    input_schema: dict,
    outputs: list[dict],
    templates: dict[str, Template],
    flows: dict[str, str] | None = None,
    flow_id: str = "",
) -> dict:
    """校验一份流程配置。返回 {ok, errors, warnings, layers}。

    flows 是「所有可调用的流程」{流程id: 流程名}，用来检查子流程引用。
    flow_id 是当前流程自己的 id（用来发现「调用自己」）。
    """
    flows = flows or {}
    errors: list[str] = []
    warnings: list[str] = []

    # ── 步骤 id 唯一 ──
    seen: set[str] = set()
    for index, step in enumerate(steps):
        sid = str(step.get("id") or "")
        if not sid:
            errors.append(f"第 {index + 1} 步缺少 id")
            continue
        if sid in seen:
            errors.append(f"步骤 id 重复：{sid}")
        seen.add(sid)

    input_names = _input_field_names(input_schema)
    # 步骤 id → 类型：引用子流程的具名产出（steps.sub.final_image）时要放行
    step_types = {
        str(s.get("id") or ""): str(s.get("type") or "template") for s in steps
    }

    for index, step in enumerate(steps):
        sid = str(step.get("id") or f"step{index + 1}")
        label = step_label(steps, sid)
        step_type = step.get("type") or "template"
        if step_type == "flow":
            # 子流程：检查它存在、不是自己
            sub_id = str(step.get("flow_id") or "")
            if not sub_id:
                errors.append(f"{label}是子流程，但没指定调用哪条流程")
            elif sub_id == flow_id:
                errors.append(f"{label}调用了自己（子流程不能调用自己）")
            elif sub_id not in flows:
                errors.append(f"{label}调用的子流程不存在：{sub_id}")
            # 子流程的参数也要能算出来（跟普通步骤一样校验引用）
            for _field, expr in (step.get("params") or {}).items():
                for problem in _expr_errors(expr):
                    errors.append(f"{label}的参数写法有问题：{problem}")
                for ref in collect_base_refs(expr):
                    if ref.startswith("input."):
                        name = ref.split(".", 1)[1]
                        if name not in input_names:
                            errors.append(
                                f"{label}引用了流程输入「{name}」，但流程输入里没有这个字段"
                            )
            continue
        if step_type not in ("template", "repeat"):
            errors.append(f"{label}的类型 {step_type} 不认识")
            continue

        template_id = str(step.get("template_id") or "")
        template = templates.get(template_id)
        if template is None:
            errors.append(f"{label}引用的节点不存在：{template_id}")
            continue

        fields = _field_map(template.input_schema)
        for field, expr in (step.get("params") or {}).items():
            if field not in fields:
                spec_label = str(fields.get(field, {}).get("label") or field)
                errors.append(
                    f"{label}的字段「{spec_label}」在节点「{template.name}」里不存在"
                    f"（可用：{', '.join(sorted(fields)) or '无'}）"
                )
                continue
            spec = fields[field]
            is_ref = isinstance(expr, str) and "{{" in expr

            for problem in _expr_errors(expr):
                errors.append(f"{label}的字段「{spec.get('label') or field}」写法有问题：{problem}")

            if is_file_field(spec) and not is_ref:
                errors.append(
                    f"{label}的字段「{spec.get('label') or field}」是文件类型，"
                    "必须用引用取值（写素材 id 或 {{...}} 表达式），不能写字面量"
                )

            # 引用上游产物只能喂给文件字段 —— 普通字段接了就是配置错误。
            # 这条检查原来只在执行器里（跑到那一步才报），挪到校验层，
            # 保存流程时就拦住，别让用户白跑一次引擎才发现。
            if not is_file_field(spec) and _refs_upstream(expr):
                errors.append(
                    f"{label}的字段「{spec.get('label') or field}」的类型是 "
                    f"{spec.get('type')}，不能接上游产物"
                    "（只有图片/文件类型的字段能接）"
                )

            for ref in collect_base_refs(expr):
                if ref.startswith("input."):
                    name = ref.split(".", 1)[1]
                    if name not in input_names:
                        errors.append(
                            f"{label}引用了流程输入「{name}」，但流程输入里没有这个字段"
                        )
                elif ref in _LOOP_VARS:
                    # index / total / item 只在循环步骤里才有意义
                    if step_type != "repeat":
                        errors.append(
                            f"{label}用了循环变量「{ref}」，但它不是循环步骤"
                            "（只有「循环」步骤才能用 index / total / item）"
                        )
            for ref in collect_refs(expr):
                if ref.startswith("steps."):
                    parts = ref.split(".")
                    if len(parts) < 3:
                        warnings.append(
                            f"{label}的引用「{ref}」不完整"
                            "（应为 steps.<步骤>.outputs，子流程也可以用产出名）"
                        )
                        continue
                    upstream, suffix = parts[1], parts[2]
                    # 子流程的具名产出（steps.sub.final_image）是合法写法；
                    # 普通节点只有 outputs 这一种产物入口。
                    if suffix != "outputs" and step_types.get(upstream) != "flow":
                        warnings.append(
                            f"{label}的引用「{ref}」只认 steps.<步骤>.outputs 这种形状"
                        )
                    if upstream == sid:
                        errors.append(f"{label}引用了自己的产物（成环）")
                        continue
                    if upstream not in seen:
                        errors.append(
                            f"{label}引用了不存在的步骤「{upstream}」"
                        )
                    else:
                        up_index = next(
                            (
                                i
                                for i, s in enumerate(steps)
                                if str(s.get("id") or "") == upstream
                            ),
                            -1,
                        )
                        # 执行按依赖分层，引用排在后面的步骤也能跑对 ——
                        # 只提示「列表顺序和实际顺序不一致」，不拦。
                        if up_index > index:
                            warnings.append(
                                f"{label}引用了排在后面的{step_label(steps, upstream)}"
                                "（执行时会按依赖自动提前，但建议把它拖到前面）"
                            )

    # ── 循环次数 ──
    for step in steps:
        if (step.get("type") or "template") != "repeat":
            continue
        label = step_label(steps, str(step.get("id") or ""))
        count_expr = str(step.get("count_expr") or "").strip()
        if count_expr:
            for ref in collect_base_refs(count_expr):
                if ref.startswith("input."):
                    name = ref.split(".", 1)[1]
                    if name not in input_names:
                        errors.append(
                            f"{label}的循环次数引用了流程输入「{name}」，"
                            "但流程输入里没有这个字段"
                        )
        else:
            count = step.get("count")
            # count = 0 的含义是「由列表长度决定」—— 循环的默认用法，
            # 不该报错（列表长度要等运行时才知道）。
            if not isinstance(count, int) or count < 0:
                errors.append(f"{label}的循环次数不能是负数")
            elif count == 0 and not _has_list_source(step):
                errors.append(
                    f"{label}的循环次数是 0，但没找到列表来源"
                    "（填具体次数，或在参数里引用「列表[序号]」来按列表长度跑）"
                )

    # ── 输出声明 ──
    out_names: set[str] = set()
    for out in outputs:
        name = str(out.get("name") or "").strip()
        src = str(out.get("from_step") or out.get("from") or "")
        pick = str(out.get("pick") or "all")
        if not name:
            errors.append("输出声明缺少 name")
        elif name in out_names:
            # 重名会让父流程拿到不确定的那一个（后写覆盖前写），必须拦
            errors.append(f"输出名「{name}」重复了，换一个")
        else:
            out_names.add(name)
        if src and src not in seen:
            errors.append(f"输出「{name}」引用的步骤不存在：{src}")
        if pick not in ("all", "first", "last"):
            errors.append(
                f"输出「{name}」的取值方式「{pick}」不认识（只能是 all / first / last）"
            )

    layers, cycle = compute_layers(steps)
    if cycle:
        errors.append(f"步骤之间存在循环依赖：{', '.join(cycle)}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "layers": layers,
    }
