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

**列表下标**：`input.prompts[index]` —— 方括号里是变量名（不是字面量），
求值时从上下文取值再当下标用。循环节点就靠它把「第 i 条提示词」喂给节点。
方括号里只允许标识符，不允许表达式 —— 保持可静态分析。
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
# 带方括号下标的路径：input.prompts[index]
_BRACKET_INDEX = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]")
# 路径里允许出现方括号（段内下标），也要允许纯点号分段
# —— 两者的区别只是有没有方括号，所以合并成一个正则：
#   input.prompts[index]      段内带方括号
#   steps.gen.outputs.first   纯点号（first/last 是列表取值）
_PATH_WITH_BRACKET = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*"
    r"(\.[A-Za-z_][A-Za-z0-9_]*|\[[A-Za-z0-9_]+\])*$"
)
# 段内带方括号的路径（用于区分「要不要走方括号求值」）
_PATH_WITH_INNER_BRACKET = re.compile(r"\[[A-Za-z0-9_]+\]")


class FlowExprError(ValueError):
    """表达式写错或引用不存在。带上下文，便于界面直接显示给用户。"""


def parse_ref(path: str) -> list[str]:
    """把 "steps.gen.outputs" 拆成 ["steps", "gen", "outputs"]。"""
    path = path.strip()
    if not (_PATH.match(path) or _PATH_WITH_INDEX.match(path)):
        raise FlowExprError(f"引用路径不合法：{path!r}")
    return path.split(".")


def _split_index(path: str, part: str, ctx: dict) -> int:
    """方括号里的下标求值 —— 只允许标识符，取它的整数值。"""
    m = _BRACKET_INDEX.fullmatch(part)
    if not m:
        # 方括号里是数字 —— 那是「固定下标」，写法应该是 .0 而不是 [0]
        if re.fullmatch(r"\[\d+\]", part):
            raise FlowExprError(
                f"引用「{path}」的固定下标请写成点号形式"
                f"（{part} → .{part[1:-1]}）"
            )
        raise FlowExprError(f"下标写法不合法：{part!r}（方括号里只能是变量名）")
    name = m.group(1)
    if name not in ctx:
        raise FlowExprError(f"下标变量「{name}」不存在")
    raw = ctx[name]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise FlowExprError(f"下标变量「{name}」不是整数（{raw!r}）")
    if raw < 0:
        raise FlowExprError(f"下标变量「{name}」不能是负数")
    return raw


def resolve_ref(path: str, ctx: dict):
    """按路径从上下文取值。取值只走字典下标 —— 不 getattr，不 eval。

    列表可以用 .first / .last 取元素 —— ComfyUI 一个工作流常出好几张图，
    下游只要其中一张时必须能指名，否则只能全喂过去然后报「字段只接一张图」。

    方括号下标 `input.prompts[index]` —— 循环节点靠它把「第 i 条」喂给节点。
    方括号里只允许标识符（变量名），不允许表达式 —— 保持可静态分析。
    """
    path = path.strip()
    # 只有「段内带方括号」才走新求值器；纯点号路径走原逻辑
    # （steps.gen.outputs.first 里的 first 是列表取值，不是方括号下标）
    if _PATH_WITH_BRACKET.match(path) and _PATH_WITH_INNER_BRACKET.search(path):
        return _resolve_with_bracket(path, ctx)
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


def _resolve_with_bracket(path: str, ctx: dict):
    """求值带方括号下标的路径，如 input.prompts[index]。"""
    # 按 '.' 分段，但方括号里的点不拆（虽然现在也不允许）
    segments: list[str] = []
    buf = ""
    depth = 0
    for ch in path:
        if ch == "[":
            depth += 1
            buf += ch
        elif ch == "]":
            depth -= 1
            buf += ch
        elif ch == "." and depth == 0:
            segments.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        segments.append(buf)

    cur: object = ctx
    for i, seg in enumerate(segments):
        bracket = _BRACKET_INDEX.fullmatch(seg)
        if bracket:
            index = _split_index(path, seg, ctx)
            if not isinstance(cur, list):
                raise FlowExprError(f"引用「{path}」的第 {i + 1} 段不是列表，不能取下标")
            if index >= len(cur):
                raise FlowExprError(f"引用「{path}」越界（只有 {len(cur)} 项）")
            cur = cur[index]
            continue

        # 段里可能既有名字又有下标：prompts[index]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(.*)$", seg)
        if not m:
            raise FlowExprError(f"引用「{path}」的第 {i + 1} 段不合法：{seg!r}")
        name, rest = m.group(1), m.group(2)
        if not isinstance(cur, dict):
            raise FlowExprError(f"引用「{path}」的第 {i + 1} 段找不到（取值不是对象）")
        if name not in cur:
            raise FlowExprError(f"引用「{path}」不存在（第 {i + 1} 段 {name!r} 找不到）")
        cur = cur[name]
        if rest:
            index = _split_index(path, rest, ctx)
            if not isinstance(cur, list):
                raise FlowExprError(f"引用「{path}」不能取下标（{name} 不是列表）")
            if index >= len(cur):
                raise FlowExprError(f"引用「{path}」越界（只有 {len(cur)} 项）")
            cur = cur[index]
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
    return [
        r
        for r in refs
        if _PATH.match(r) or _PATH_WITH_INDEX.match(r) or _PATH_WITH_BRACKET.match(r)
    ]


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
