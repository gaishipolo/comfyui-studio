"""Agent 编排规格 —— **从代码生成**，不是手写文档。

**为什么必须是生成的**：Agent 只看这份规格就知道能拼什么。
如果规格是手写的，它必然和实现漂移 —— 加了新的节点类型忘了写进文档，
Agent 就会一直用旧规则拼流程，产出一堆跑不通的配置，而且没人知道是文档错了。

所以这里的每个数字都从**真实来源**取：
- 可用节点 → 数据库里 active 的 Template（和编排器看到的是同一批）
- 可用函数 → flow_expr_lang.FUNCTION_NAMES（白名单本身）
- 字段类型 → field_types 归一后的口径
- 步骤类型 / 依赖规则 → flow_validate 的实际校验逻辑

**规格里不该出现的东西**：内部标识（input. / steps. 这些路径形态要给，
因为 Agent 得写出表达式），但不该出现「怎么实现的」——Agent 只需要知道
能做什么、怎么写合法。
"""
from app.services.flow_expr_lang import FUNCTION_NAMES
from app.services.field_types import normalized_field_map

# 步骤类型 —— 与 flow_validate 认可的类型一致
STEP_TYPES = [
    {
        "type": "template",
        "label": "普通节点",
        "description": "执行一个节点（Template）。上下游靠 params 里的表达式连接。",
    },
    {
        "type": "repeat",
        "label": "循环",
        "description": (
            "把同一个节点跑 N 次，每次喂不同的输入。"
            "次数默认由列表输入的长度决定（count=0），"
            "也可以用 count_expr 指定。"
            "循环里可以用三个变量：index（第几次，从 0 起）、total（一共几次）、"
            "item（当前这次对应的那一项）。"
        ),
    },
    {
        "type": "flow",
        "label": "子流程",
        "description": (
            "调用另一条流程。子流程的输入按字段同名自动对上，"
            "也可以用 params 显式指定。"
            "子流程不能调用自己，嵌套最多 5 层。"
        ),
    },
]

# 表达式语法 —— 举例说明，不写完整文法（那是实现细节）
EXPRESSION_SYNTAX = [
    {
        "form": "{{ input.<字段> }}",
        "label": "取流程输入",
        "example": "{{ input.prompt }}",
        "description": "流程的输入字段。运营在运行台填的值。",
    },
    {
        "form": "{{ steps.<步骤id>.outputs }}",
        "label": "接上游产物",
        "example": "{{ steps.gen.outputs }}",
        "description": (
            "上游步骤产出的素材。只有图片/文件类型的字段能接。"
            "取其中一张用 .first / .last（一个工作流常出好几张图）。"
        ),
    },
    {
        "form": "{{ input.prompts[index] }}",
        "label": "取列表的第 N 项",
        "example": "{{ input.prompts[index] }}",
        "description": "循环时把列表里的第 i 项喂给节点（index 是循环变量）。",
    },
    {
        "form": "{{ <表达式> | <函数> }}",
        "label": "加工",
        "example": "{{ input.prompt | trim | truncate(80) }}",
        "description": (
            "取到值之后做处理。可以串多个，从左到右依次应用。"
            "管道里的函数把上一步的值当第一个参数。"
        ),
    },
    {
        "form": "{{ <字段> <运算符> \"<值>\" ? <表达式> : <表达式> }}",
        "label": "按条件取值",
        "example": '{{ input.style == "写实" ? input.real_prompt : input.art_prompt }}',
        "description": (
            "判断某个流程输入，满足用一边、不满足用另一边。"
            "运算符有 == 与 !=。"
        ),
    },
]

# 运算符 —— 与 flow_expr_lang 的比较运算一致
COMPARISON_OPS = [
    {"op": "==", "label": "等于"},
    {"op": "!=", "label": "不等于"},
]


def build_function_catalog() -> list[dict]:
    """可用函数清单 —— 与后端白名单同源。

    每项带参数说明，Agent 才知道怎么传参。
    """
    docs = {
        "trim": {"args": [], "summary": "去掉首尾空格"},
        "upper": {"args": [], "summary": "转成大写"},
        "lower": {"args": [], "summary": "转成小写"},
        "truncate": {
            "args": [{"name": "length", "type": "number", "summary": "保留多少字"}],
            "summary": "截断到指定字数",
        },
        "replace": {
            "args": [
                {"name": "old", "type": "string", "summary": "要找的文字"},
                {"name": "new", "type": "string", "summary": "换成什么"},
            ],
            "summary": "替换文字",
        },
        "default": {
            "args": [{"name": "fallback", "type": "string", "summary": "为空时用这个"}],
            "summary": "值为空时的兜底",
        },
        "join": {
            "args": [{"name": "sep", "type": "string", "summary": "分隔符", "optional": True}],
            "summary": "把列表拼成一个字符串",
        },
        "split": {
            "args": [{"name": "sep", "type": "string", "summary": "分隔符", "optional": True}],
            "summary": "按分隔符拆成列表",
        },
        "len": {"args": [], "summary": "算长度"},
        "contains": {
            "args": [{"name": "needle", "type": "string", "summary": "要找的文字"}],
            "summary": "是否包含",
        },
        "index": {
            "args": [{"name": "needle", "type": "string", "summary": "要找的项"}],
            "summary": "在列表里的位置（找不到是 -1）",
        },
        "int": {"args": [], "summary": "转成整数"},
        "number": {"args": [], "summary": "转成数字"},
        "first": {"args": [], "summary": "取第一个"},
        "last": {"args": [], "summary": "取最后一个"},
        "today": {
            "args": [{"name": "fmt", "type": "string", "summary": "日期格式", "optional": True}],
            "summary": "今天的日期",
        },
        "date": {
            "args": [
                {"name": "value", "type": "string", "summary": "日期字符串"},
                {"name": "fmt", "type": "string", "summary": "日期格式", "optional": True},
            ],
            "summary": "把日期按格式重排",
        },
        "now": {"args": [], "summary": "当前时间"},
    }
    catalog: list[dict] = []
    for name in FUNCTION_NAMES:
        doc = docs.get(name, {"args": [], "summary": ""})
        catalog.append({"name": name, "summary": doc["summary"], "args": doc["args"]})
    return catalog


def build_node_catalog(templates) -> list[dict]:
    """可用节点清单 —— 与编排器的「可选节点」下拉同一批。

    fields 用归一后的口径（image_upload → file_upload），
    否则 Agent 会照着旧别名写出接不了产物的配置。
    """
    nodes: list[dict] = []
    for tpl in templates:
        fields = normalized_field_map(tpl.input_schema)
        nodes.append(
            {
                "id": tpl.id,
                "name": tpl.name,
                "description": tpl.description or "",
                "category": tpl.category or "",
                "engine": tpl.engine or "",
                "capability": tpl.capability or "",
                "fields": [
                    {
                        "name": name,
                        "label": spec.get("label") or name,
                        "type": spec.get("type") or "text",
                        "required": bool(spec.get("required")),
                        "options": spec.get("options") or [],
                        "default": spec.get("default"),
                        "multiple": bool(spec.get("multiple")),
                        "accepts_output": spec.get("type") == "file_upload",
                    }
                    for name, spec in sorted(fields.items())
                ],
            }
        )
    return nodes


def build_spec(templates, flows=None) -> dict:
    """完整规格 —— Agent 编排流程需要知道的全部信息。

    templates 是数据库里 active 的 Template（编排器能用的节点）。
    flows 是可作为子流程调用的流程（给 Agent 挑）。
    """
    return {
        "version": 1,
        "intro": (
            "流程是一串步骤，每步是一个节点。"
            "步骤之间靠 params 里的表达式连接："
            "「{{ input.x }}」取流程输入，「{{ steps.<id>.outputs }}」接上游产物。"
            "配置是纯数据，不写代码 —— 生成后会被校验、试跑，人审批后才生效。"
        ),
        "steps": STEP_TYPES,
        "expressions": {
            "syntax": EXPRESSION_SYNTAX,
            "functions": build_function_catalog(),
            "comparison_ops": COMPARISON_OPS,
        },
        "nodes": build_node_catalog(templates),
        "subflows": [
            {"id": f.id, "name": f.name, "description": f.description or ""}
            for f in (flows or [])
        ],
        "rules": [
            "步骤 id 在流程内唯一，且只能引用前面的步骤（顺序执行，拿不到还没跑的产物）",
            "只有图片/文件类型的字段能接上游产物",
            "文件类型的字段必须用表达式取值，不能写字面量",
            "不能有循环依赖（步骤之间互相等）",
            "流程输入的字段名要在 input_schema 里声明过",
            "子流程不能调用自己，嵌套最多 5 层",
        ],
        "workflow": [
            {"step": 1, "action": "GET /api/flows/agent/spec", "note": "拿这份规格"},
            {"step": 2, "action": "POST /api/flows/agent/draft", "note": "提交配置，落成草稿"},
            {"step": 3, "action": "POST /api/flows/{id}/dry-run", "note": "试跑（产物不进素材库）"},
            {"step": 4, "action": "POST /api/flows/{id}/approve", "note": "人审批后生效"},
        ],
    }
