"""流程参数的取值表达式 —— 门面。

流程里每个步骤的每个参数都写成「字面量」或「{{ 表达式 }}」：

    params: {
      "prompt": "{{ input.prompt }}",        ← 流程输入
      "image":  "{{ steps.gen.outputs }}",   ← 上游步骤的产物
      "size":   "2K",                       ← 字面量，原样传下去
    }

**为什么用表达式而不是「同名自动取 + 显式绑定」两套机制**：
两个节点可能都有同名字段（Flux2 和 Agnes 都有 prompt），同名自动取会把同一个
值喂给两步。所以「哪个字段吃哪个值」必须显式写出来 —— 这是流程独有的信息，
节点里不可能有（节点不知道自己会被用在哪一步）。

**不用 eval**：见 flow_expr_lang —— 只有白名单函数 + 字典下标，
表达式将来是 Agent 生成的，任意代码执行路径不可接受，而且必须可静态分析
（校验层要确认它引用了哪些路径、用了哪些函数），否则审批失去意义。

三层分工：
- `flow_paths`      路径取值（只走字典下标）
- `flow_expr_lang`  表达式语言：管道 / 比较 / 条件 + 函数白名单
- `flow_expr`（本文件）门面：把两者接到流程参数上

**列表下标**：`input.prompts[index]` —— 方括号里是变量名（不是字面量），
求值时从上下文取值再当下标用。循环节点就靠它把「第 i 条提示词」喂给节点。
方括号里只允许标识符，不允许表达式 —— 保持可静态分析。
"""
import re

from app.services.flow_expr_lang import (
    FUNCTION_NAMES,
    check as _lang_check,
    collect_paths as _lang_collect_paths,
    eval_expr,
    parse as _lang_parse,
)
from app.services.flow_paths import (
    FlowExprError,
    parse_ref,
    resolve_ref,
)

# 整体就是一个 {{ ... }}（前后没有别的文本）→ 求值后保留原始类型
_WHOLE_REF = re.compile(r"^\{\{\s*([^{}]+?)\s*\}\}$")
# 文本中夹着 {{ ... }} → 按字符串插值处理
_PART_REF = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

__all__ = [
    "FUNCTION_NAMES",
    "FlowExprError",
    "collect_base_refs",
    "collect_refs",
    "expr_problems",
    "parse_ref",
    "resolve_ref",
    "resolve_value",
]


def resolve_value(expr, ctx: dict):
    """求值一个参数取值。

    - 非字符串 → 原样返回（数字、布尔、列表等直接用）
    - 整个字符串就是一个 {{...}} → 返回引用的**原始类型**
      （上游产物是列表就该拿到列表，不能变成字符串）
    - 字符串里夹着 {{...}} → 做字符串插值，结果是字符串
    """
    if not isinstance(expr, str):
        return expr

    whole = _WHOLE_REF.match(expr.strip())
    if whole:
        return eval_expr(whole.group(1), ctx)

    if "{{" not in expr:
        return expr

    def _sub(match: re.Match) -> str:
        value = eval_expr(match.group(1), ctx)
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            # 列表/字典插进句子没有意义，且会产出不可预期的文本
            raise FlowExprError(
                f"引用「{match.group(1)}」是 {type(value).__name__}，不能插进文本"
                "（要用它的全部内容，请让整个取值就是一个 {{...}}）"
            )
        return str(value)

    return _PART_REF.sub(_sub, expr)


def collect_refs(expr) -> list[str]:
    """列出一个取值表达式引用了哪些路径 —— 校验时用来确认引用是否存在。

    表达式解析不了（语法错）时退回「按正则捞出像路径的部分」——
    校验层会另外报语法错，这里不能先炸掉。
    """
    if not isinstance(expr, str):
        return []
    raw = [m.group(1).strip() for m in _PART_REF.finditer(expr)]
    out: list[str] = []
    for item in raw:
        try:
            paths = _lang_collect_paths(_lang_parse(item))
        except FlowExprError:
            paths = []
        for path in paths:
            if path not in out:
                out.append(path)
    return out


def collect_base_refs(expr) -> list[str]:
    """列出一个取值表达式引用了哪些「基础路径」—— 去掉方括号下标。

    校验时要确认 `input.prompts[index]` 里的 `input.prompts` 存在，
    但 `index` 是循环变量（运行时才有），不该被当成流程输入来查。
    """
    out: list[str] = []
    for ref in collect_refs(expr):
        # input.prompts[index] → input.prompts
        base = ref.split("[", 1)[0]
        if base and base not in out:
            out.append(base)
    return out


def expr_problems(expr) -> list[str]:
    """静态检查一个取值表达式 —— 返回问题列表（空 = 没问题）。

    校验层在**保存流程时**调用：函数名不存在、参数个数不对、语法错，
    都当场报出来。别等跑到那一步、引擎都花钱了才发现。
    """
    if not isinstance(expr, str) or "{{" not in expr:
        return []
    problems: list[str] = []
    for m in _PART_REF.finditer(expr):
        item = m.group(1).strip()
        try:
            tree = _lang_parse(item)
        except FlowExprError as exc:
            problems.append(str(exc))
            continue
        problems.extend(_lang_check(tree))
    return problems
