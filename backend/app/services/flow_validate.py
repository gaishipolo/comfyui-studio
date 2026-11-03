"""流程配置校验 + 依赖分层。

编排器保存前、Agent 生成后走的是同一道门 —— 所以它必须在服务层，
不能是某个界面的私有逻辑。

检查项分两档：
- error：这份配置跑不起来（引用了不存在的节点/字段、环、文件字段填了字面量）
- warning：能跑但可能有坑（引用的上游步骤在后面 —— 顺序执行下拿不到产物）

依赖分层用 Kahn 算法：同一层的步骤互不依赖，可以并行。第一刀执行器
还是顺序的，但分层结果一并返回 —— 编排界面要显示「第几步」，
将来并行时执行器直接用这个分层，不用重新算。
"""
from app.models.models import Template
from app.services.flow_expr import collect_refs


def _field_map(input_schema) -> dict:
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


def _input_field_names(input_schema) -> set[str]:
    return set(_field_map(input_schema))


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
) -> dict:
    """校验一份流程配置。返回 {ok, errors, warnings, layers}。"""
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

    for index, step in enumerate(steps):
        sid = str(step.get("id") or f"第{index + 1}步")
        step_type = step.get("type") or "template"
        if step_type != "template":
            warnings.append(
                f"步骤「{sid}」的类型是 {step_type}，第一刀只支持 template"
                "（子流程与循环还没实现）"
            )
            continue

        template_id = str(step.get("template_id") or "")
        template = templates.get(template_id)
        if template is None:
            errors.append(f"步骤「{sid}」引用的节点不存在：{template_id}")
            continue

        fields = _field_map(template.input_schema)
        for field, expr in (step.get("params") or {}).items():
            if field not in fields:
                errors.append(
                    f"步骤「{sid}」的字段「{field}」在节点「{template.name}」里不存在"
                    f"（可用：{', '.join(sorted(fields)) or '无'}）"
                )
                continue
            spec = fields[field]
            is_ref = isinstance(expr, str) and "{{" in expr

            if spec.get("type") == "file_upload" and not is_ref:
                errors.append(
                    f"步骤「{sid}」的字段「{field}」是文件类型，必须用引用取值"
                    "（写素材 id 或 {{...}} 表达式），不能写字面量"
                )

            for ref in collect_refs(expr):
                if ref.startswith("input."):
                    name = ref.split(".", 1)[1]
                    if name not in input_names:
                        errors.append(
                            f"步骤「{sid}」引用了流程输入「{name}」，但流程输入里没有这个字段"
                        )
                elif ref.startswith("steps."):
                    parts = ref.split(".")
                    if len(parts) < 3 or parts[2] != "outputs":
                        warnings.append(
                            f"步骤「{sid}」的引用「{ref}」只认 steps.<步骤>.outputs 这种形状"
                        )
                    else:
                        upstream = parts[1]
                        if upstream not in seen:
                            errors.append(
                                f"步骤「{sid}」引用了不存在的步骤「{upstream}」"
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
                            if up_index >= index:
                                errors.append(
                                    f"步骤「{sid}」引用了后面的步骤「{upstream}」"
                                    "（只能引用前面的步骤 —— 顺序执行下拿不到还没跑的产物）"
                                )

    # ── 输出声明 ──
    for out in outputs:
        name = str(out.get("name") or "")
        src = str(out.get("from_step") or out.get("from") or "")
        if not name:
            errors.append("输出声明缺少 name")
        if src and src not in seen:
            errors.append(f"输出「{name}」引用的步骤不存在：{src}")

    layers, cycle = compute_layers(steps)
    if cycle:
        errors.append(f"步骤之间存在循环依赖：{', '.join(cycle)}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "layers": layers,
    }
