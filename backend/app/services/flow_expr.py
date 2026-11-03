"""流程参数的取值表达式

流程里每个步骤的每个参数都写成「字面量」或「{{ 引用 }}」：

    params: {
      "prompt": "{{ input.prompt }}",        ← 流程输入
      "image":  "{{ steps.gen.outputs }}",   ← 上游步骤的产物
      "size":   "2K",                       ← 字面量，原样传下去
    }

**为什么用表达式而不是「同名自动取 + 显式绑定」两套机制**：
两个节点可能都有同名字段（Flux2 和 Agnes 都有 prompt），同名自动取会把同一个
值喂给两步。所以「哪个字段吃哪个值」必须显式写出来 —— 这是流程独有的信息，
节点里不可能有（节点不知道自己会被用在哪一步）。

**不用 eval**：这里只做受限的路径解析（input.x / steps.s1.outputs），
取值只走字典下标，不碰 getattr、不允许导入。将来接 Agent 时，Agent 生成的
配置里出现任意代码执行路径是不可接受的 —— 表达式语言必须保持可静态分析。

第一刀只支持两种引用；函数管道、条件表达式留到第二刀（接口已经留好，
加的时候不用改调用方）。
"""
import re

# 整体就是一个 {{ ... }}（前后没有别的文本）→ 求值后保留原始类型
_WHOLE_REF = re.compile(r"^\{\{\s*([^{}]+?)\s*\}\}$")
# 文本中夹着 {{ ... }} → 按字符串插值处理
_PART_REF = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
# 允许的引用路径：字母数字下划线 + 点分段。
# 末段可以是纯数字 —— 那是列表下标（steps.s1.outputs.0）。
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PATH_WITH_INDEX = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*(\.\d+)?$"
)


class FlowExprError(ValueError):
    """表达式写错或引用不存在。带上下文，便于界面直接显示给用户。"""


def parse_ref(path: str) -> list[str]:
    """把 "steps.gen.outputs" 拆成 ["steps", "gen", "outputs"]。"""
    path = path.strip()
    if not (_PATH.match(path) or _PATH_WITH_INDEX.match(path)):
        raise FlowExprError(f"引用路径不合法：{path!r}")
    return path.split(".")


def resolve_ref(path: str, ctx: dict):
    """按路径从上下文取值。取值只走字典下标 —— 不 getattr，不 eval。

    列表可以用 .first / .last 取元素 —— ComfyUI 一个工作流常出好几张图，
    下游只要其中一张时必须能指名，否则只能全喂过去然后报「字段只接一张图」。
    """
    parts = parse_ref(path)
    cur: object = ctx
    for i, part in enumerate(parts):
        if isinstance(cur, dict):
            if part not in cur:
                raise FlowExprError(
                    f"引用「{path}」不存在（第 {i + 1} 段 {part!r} 找不到）"
                )
            cur = cur[part]
        elif isinstance(cur, list):
            if part == "first":
                if not cur:
                    raise FlowExprError(f"引用「{path}」取不到元素（上游没有产物）")
                cur = cur[0]
            elif part == "last":
                if not cur:
                    raise FlowExprError(f"引用「{path}」取不到元素（上游没有产物）")
                cur = cur[-1]
            elif part.isdigit():
                index = int(part)
                if index >= len(cur):
                    raise FlowExprError(
                        f"引用「{path}」越界（上游只有 {len(cur)} 个产物）"
                    )
                cur = cur[index]
            else:
                raise FlowExprError(
                    f"引用「{path}」的第 {i + 1} 段 {part!r} 不是列表能用的"
                    "（first / last / 数字下标）"
                )
        else:
            raise FlowExprError(
                f"引用「{path}」不存在（第 {i + 1} 段 {part!r} 找不到）"
            )
    return cur


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
        return resolve_ref(whole.group(1), ctx)

    if "{{" not in expr:
        return expr

    def _sub(match: re.Match) -> str:
        value = resolve_ref(match.group(1), ctx)
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
    """列出一个取值表达式引用了哪些路径 —— 校验时用来确认引用是否存在。"""
    if not isinstance(expr, str):
        return []
    refs = [m.group(1).strip() for m in _PART_REF.finditer(expr)]
    return [r for r in refs if _PATH.match(r) or _PATH_WITH_INDEX.match(r)]
