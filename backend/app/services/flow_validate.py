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
import re

from app.models.models import Template
from app.services.field_types import is_file_field, normalized_field_map
from app.services.flow_expr import collect_base_refs, collect_refs


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
    """这一步的参数量引了「列表[序号]」吗 —— 那种情况下次数由列表长度决定。"""
    for expr in (step.get("params") or {}).values():
        if not isinstance(expr, str):
            continue
        if re.search(r"input\.[A-Za-z_][A-Za-z0-9_]*\[[A-Za-z_][A-Za-z0-9_]*\]", expr):
            return True
    return False


def _refs_upstream(expr) -> bool:
    """这个取值引用了上游步骤的产物吗。"""
    if not isinstance(expr, str):
        return False
    return any(ref.startswith("steps.") for ref in collect_refs(expr))


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
        sid = str(step.get("id") or f"step{index + 1}")
        label = step_label(steps, sid)
        step_type = step.get("type") or "template"
        if step_type == "flow":
            warnings.append(
                f"{label}是子流程 —— 还没实现，先按普通步骤跳过"
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
                    if len(parts) < 3 or parts[2] != "outputs":
                        warnings.append(
                            f"{label}的引用「{ref}」只认 steps.<步骤>.outputs 这种形状"
                        )
                    else:
                        upstream = parts[1]
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
                            if up_index >= index:
                                errors.append(
                                    f"{label}引用了后面的{step_label(steps, upstream)}"
                                    "（只能引用前面的步骤 —— 顺序执行下拿不到还没跑的产物）"
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
