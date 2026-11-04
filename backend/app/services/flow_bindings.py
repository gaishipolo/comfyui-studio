"""自动接上（同名绑定）的规则 —— 前后端各一份，规则必须逐字一致。

**为什么需要它**：节点参数不配也能跑，是这套系统「不用教用户写表达式」的
根本。但「不配 = 接什么」必须有唯一答案 —— 否则界面显示的和实际跑的不是
一回事，而这种错不会报错，只会让用户拿到错的图。

**规则（两条，按顺序）**：

1. 节点字段的**标签**与某个流程输入字段的**标签**相同 → 用那个流程输入
2. 节点字段的**标签**与某个上游步骤的**产出名**相同 → 用那个步骤的产物
   （只有文件/图片类字段能接产物 —— 文本字段喂产物列表没有意义）

**为什么按标签而不是内部标识**：流程输入的 name 是系统生成的（input1、
input2…），节点字段的 name 是英文（prompt、image…）。两边内部标识基本
对不上，但**标签都是人写的、而且多半是同一个词**（「提示词」「参考图」）。
按标签接，才符合人的直觉。

标签为空时退回按 name 比 —— 内部标识一致也算同名。

前端对应 `frontend/src/components/flowBindings.ts`，两边规则逐字相同，
契约清单见 `backend/tests/test_flow_bindings.py` 与
`frontend/tests/flow_bindings.test.ts`。
"""


def same_name(
    a_label: str | None,
    a_name: str | None,
    b_label: str | None,
    b_name: str | None,
) -> bool:
    """两个东西是不是「同名」。

    **标签有值时只比标签** —— 标签是人的意图。内部标识相同却标签不同，
    说明它们本来就是两回事（流程输入的 name 是系统生成的，撞上英文
    字段名是巧合不是约定）。只有一方没有标签时，才退回看内部标识。
    """
    if a_label and b_label:
        return a_label == b_label
    if a_name and b_name:
        return a_name == b_name
    return False


def match_input(field: dict, input_fields: list[dict]) -> dict | None:
    """这个字段该接哪个流程输入。接不上返回 None。

    field: {"name": 内部标识, "label": 界面标签}
    input_fields: [{"name": ..., "label": ...}, ...]
    """
    for f in input_fields:
        if same_name(
            field.get("label"),
            field.get("name"),
            f.get("label"),
            f.get("name"),
        ):
            return f
    return None


def match_upstream(field: dict, upstream_outputs: dict[str, str]) -> dict | None:
    """这个字段该接哪个上游步骤的产物。接不上返回 None。

    upstream_outputs: {产出名: 步骤 id}

    返回 {"name": 产出名, "step_id": 步骤 id} —— **产出名也要带出来**：
    引用要写成 `{{ steps.s1.参考图 }}` 而不是笼统的 outputs，
    否则子流程（一个步骤可能有多个具名产出）会接错。
    """
    for name, step_id in upstream_outputs.items():
        if same_name(field.get("label"), field.get("name"), name, name):
            return {"name": name, "step_id": step_id}
    return None


def upstream_output_names(ctx: dict) -> dict[str, str]:
    """从上下文里抽出「产出名 → 步骤 id」。

    上游步骤的具名产出放在 ctx["steps"][id]["named"] 里；子流程步骤的
    具名产出也在那里（子流程自己的 outputs 声明解析出来的）。
    """
    out: dict[str, str] = {}
    for step_id, sctx in (ctx.get("steps") or {}).items():
        for name in (sctx.get("named") or {}):
            out.setdefault(name, step_id)
    return out


# ── 执行前的绑定补全 ──────────────────────────────────────────────
#
# **为什么要在执行前补**：自动接上如果不落到参数里，两步之间就没有依赖 ——
# 分层会把它们算成同一层并行，上游还没跑完，下游就接不到东西。
# 所以运行前把「自动」补成显式引用（`{{ steps.s1.参考图 }}`），
# 分层和求值自然都对了。
#
# **只补在内存里，不落库**：用户的配置里仍然写着"这是自动的"，
# 界面照旧显示自动接到了什么；改了流程输入的标签，下次运行自动跟着变。

_IDENT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _path_segment(name: str) -> str | None:
    """产出名能不能当路径的一段。不能就返回 None（改用笼统的 outputs）。"""
    if not name:
        return None
    if all(ch in _IDENT_OK or ord(ch) > 127 for ch in name):
        return name
    return None


def apply_auto_bindings(
    steps: list[dict],
    input_fields: list[dict],
    field_specs: dict[str, dict],
    sub_output_names: dict[str, list[str]] | None = None,
) -> list[dict]:
    """把步骤的 auto_fields 补成显式引用，返回补全后的步骤（副本）。

    规则和 match_input / match_upstream 一致：

    1. 字段标签跟流程输入标签相同 → `{{ input.<输入名> }}`
    2. 字段标签跟上游产出名相同 → `{{ steps.<步骤id>.<产出名> }}`
       （只有文件/图片类字段能接产物 —— 文本字段喂产物列表没有意义）

    接不上就不补：那个字段不传，节点用自己的默认值。

    **只能接前面的步骤**：后面的还没跑，接了也是空的。所以按顺序处理，
    每处理完一步就把它的产出名登记进去。

    field_specs: {步骤 id: {字段名: 字段定义}}
    sub_output_names: {子流程 id: [它的具名产出名…]}
    """
    sub_output_names = sub_output_names or {}
    out: list[dict] = []
    upstream: dict[str, str] = {}

    for step in steps:
        sid = str(step.get("id") or "")
        auto = list(step.get("auto_fields") or [])
        if auto:
            params = dict(step.get("params") or {})
            specs = field_specs.get(sid, {})
            for field in auto:
                if field in params:
                    # 显式配了的优先 —— 自动只是默认值
                    continue
                spec = specs.get(field)
                if spec is None:
                    continue
                target = {"name": field, "label": spec.get("label")}

                src = match_input(target, input_fields)
                if src is not None:
                    params[field] = "{{ input." + str(src["name"]) + " }}"
                    continue

                hit = match_upstream(target, upstream)
                if hit is None:
                    continue
                if spec.get("type") not in ("file_upload", "image_upload"):
                    # 文本字段接不了产物
                    continue
                segment = _path_segment(hit["name"])
                if segment:
                    params[field] = "{{ steps." + hit["step_id"] + "." + segment + " }}"
                else:
                    params[field] = "{{ steps." + hit["step_id"] + ".outputs }}"
            step = {**step, "params": params}
        out.append(step)

        # 这一步的产出名登记进去，供后面的步骤接
        if step.get("type") == "flow":
            names = sub_output_names.get(str(step.get("flow_id") or ""), [])
        else:
            name = str(step.get("output_name") or step.get("title") or "").strip()
            names = [name] if name else []
        for name in names:
            upstream.setdefault(name, sid)
    return out
