"""受限表达式语言 —— 管道 / 比较 / 条件。

**为什么需要它**：流程要能表达「把上游提示词截断到 80 字」「当用户选了
『写实』风格就用 A 提示词，否则用 B」。没有这些，Agent 生成的流程只能是
「把 A 接到 B」，配置表达力不够，审批也失去意义。

**三条底线**（与 flow_paths 一样）：

1. **不 eval、不 getattr、不导入**。只有白名单函数 + 字典下标。
2. **可静态分析**：能列出表达式引用了哪些路径、用了哪些函数、参数对不对 ——
   校验层靠这个在**保存时**就拦住写错的配置，而不是等跑到那步才发现。
3. **错误消息给人看**：这些消息会直接显示在编排界面上，所以要写「哪里错了、
   怎么改」，不是「invalid syntax」。

语法（只在这三种地方出现：整体引用、插值、条件分支里）::

    expr      := ternary
    ternary   := comparison ('?' expr ':' expr)?
    comparison:= pipeline (('=='|'!='|'>'|'>='|'<'|'<=') pipeline)?
    pipeline  := primary ('|' fname ['(' args ')'])?
    primary   := STRING | NUMBER | true | false | null
                | path | fname '(' args ')' | '(' expr ')'

    path      := IDENT ('.' IDENT | '.' NUMBER | '[' IDENT ']')*

例子::

    {{ input.prompt }}
    {{ input.prompt | trim | truncate(80) }}
    {{ input.style == "写实" ? input.prompt_real : input.prompt_art }}
    {{ input.prompts[index] | upper }}

**管道传参约定**：管道里的函数默认把上一步的值当第一个参数 ——
`| truncate(80)` 等价于 `truncate(上一步的值, 80)`。
"""
import re
from dataclasses import dataclass, field

from app.services.flow_paths import FlowExprError, resolve_ref

# ── 词法 ──────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
      (?P<STRING>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<NUMBER>\d+(?:\.\d+)?)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<OP>==|!=|>=|<=|>|<)
    | (?P<PIPE>\|)
    | (?P<QUESTION>\?)
    | (?P<COLON>:)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<LBRACKET>\[)
    | (?P<RBRACKET>\])
    | (?P<COMMA>,)
    | (?P<DOT>\.)
    )
    """,
    re.VERBOSE,
)

_KEYWORDS = {"true": True, "false": False, "null": None}


@dataclass
class Lit:
    """字面量"""

    value: object


@dataclass
class Path:
    """引用路径 —— 求值时原样交给 flow_paths.resolve_ref，保证语义一致。"""

    text: str


@dataclass
class Call:
    """函数调用"""

    name: str
    args: list = field(default_factory=list)


@dataclass
class Pipe:
    """管道 —— base 是起点，stages 是 (函数名, 参数列表或 None)。"""

    base: object
    stages: list = field(default_factory=list)


@dataclass
class Compare:
    op: str
    left: object
    right: object


@dataclass
class Ternary:
    cond: object
    then: object
    otherwise: object


# ── 语法 ──────────────────────────────────────────────────────────────


def _tokenize(text: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    pos = 0
    length = len(text)
    while pos < length:
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == m.start():
            raise FlowExprError(f"表达式里有看不懂的字符：{text[pos]!r}")
        kind = m.lastgroup or ""
        value = m.group(kind)
        tokens.append((kind, value, m.start()))
        pos = m.end()
    tokens.append(("EOF", "", length))
    return tokens


class _Parser:
    """递归下降解析器。语法错误带位置，方便定位。"""

    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0

    # ── 基础工具 ──
    @property
    def current(self) -> tuple[str, str, int]:
        return self.tokens[self.pos]

    def advance(self) -> tuple[str, str, int]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind: str, what: str) -> tuple[str, str, int]:
        if self.current[0] != kind:
            raise FlowExprError(
                f"表达式这里少了{what}（{self._where()}）"
            )
        return self.advance()

    def _where(self) -> str:
        kind, value, _at = self.current
        if kind == "EOF":
            return "表达式末尾"
        return f"「{value}」附近"

    # ── 文法 ──
    def parse(self) -> object:
        node = self.parse_ternary()
        if self.current[0] != "EOF":
            raise FlowExprError(
                f"表达式「{self.text}」末尾有多余内容（{self._where()}）"
            )
        return node

    def parse_ternary(self) -> object:
        cond = self.parse_comparison()
        if self.current[0] == "QUESTION":
            self.advance()
            then = self.parse_ternary()
            self.expect("COLON", "「:」")
            otherwise = self.parse_ternary()
            return Ternary(cond, then, otherwise)
        return cond

    def parse_comparison(self) -> object:
        left = self.parse_pipeline()
        if self.current[0] == "OP":
            op = self.advance()[1]
            right = self.parse_pipeline()
            return Compare(op, left, right)
        return left

    def parse_pipeline(self) -> object:
        base = self.parse_primary()
        stages: list = []
        while self.current[0] == "PIPE":
            self.advance()
            kind, name, _ = self.advance()
            if kind != "IDENT":
                raise FlowExprError("「|」后面要写函数名")
            args: list | None = None
            if self.current[0] == "LPAREN":
                args = self.parse_args()
            stages.append((name, args))
        if not stages:
            return base
        return Pipe(base, stages)

    def parse_args(self) -> list:
        self.expect("LPAREN", "「(」")
        args: list = []
        if self.current[0] == "RPAREN":
            self.advance()
            return args
        while True:
            args.append(self.parse_ternary())
            if self.current[0] == "COMMA":
                self.advance()
                continue
            break
        self.expect("RPAREN", "「)」")
        return args

    def parse_primary(self) -> object:
        kind, value, _ = self.advance()
        if kind == "STRING":
            return Lit(_unquote(value))
        if kind == "NUMBER":
            return Lit(float(value) if "." in value else int(value))
        if kind == "LPAREN":
            # 分组 —— 让「(条件表达式) | 加工」这种写法能表达
            node = self.parse_ternary()
            self.expect("RPAREN", "「)」")
            return node
        if kind == "IDENT":
            if value in _KEYWORDS:
                return Lit(_KEYWORDS[value])
            if self.current[0] == "LPAREN":
                args = self.parse_args()
                return Call(value, args)
            return self.parse_path(value)
        raise FlowExprError(f"表达式这里写的不对（{value!r}）")

    def parse_path(self, first: str) -> Path:
        """路径 —— 原样拼回字符串，求值时交给 flow_paths（语义只有一处实现）。"""
        parts = [first]
        while True:
            kind = self.current[0]
            if kind == "DOT":
                self.advance()
                nkind, nvalue, _ = self.advance()
                if nkind == "IDENT":
                    parts.append("." + nvalue)
                elif nkind == "NUMBER":
                    parts.append("." + nvalue)
                else:
                    raise FlowExprError("「.」后面要写字段名")
            elif kind == "LBRACKET":
                self.advance()
                ikind, ivalue, _ = self.advance()
                if ikind not in ("IDENT", "NUMBER"):
                    raise FlowExprError("方括号里只能是变量名")
                self.expect("RBRACKET", "「]」")
                parts.append(f"[{ivalue}]")
            else:
                break
        return Path("".join(parts))


def _unquote(text: str) -> str:
    """去掉引号并解转义。

    **不能走 unicode_escape**：它按 latin-1 解释字节，中文会被打乱
    （"写实" 会变成乱码）。这里只处理常见的几个转义。
    """
    body = text[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            mapped = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
            if nxt == "u" and i + 5 < len(body) + 1:
                hexpart = body[i + 2 : i + 6]
                if len(hexpart) == 4:
                    try:
                        out.append(chr(int(hexpart, 16)))
                        i += 6
                        continue
                    except ValueError:
                        pass
        out.append(ch)
        i += 1
    return "".join(out)


# ── 函数白名单 ────────────────────────────────────────────────────────


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fn_trim(value):
    return _as_text(value).strip()


def _fn_upper(value):
    return _as_text(value).upper()


def _fn_lower(value):
    return _as_text(value).lower()


def _fn_truncate(value, length):
    try:
        limit = int(length)
    except (TypeError, ValueError):
        raise FlowExprError("truncate 的长度要写数字") from None
    if limit < 0:
        raise FlowExprError("truncate 的长度不能是负数")
    text = _as_text(value)
    return text if len(text) <= limit else text[:limit]


def _fn_replace(value, old, new):
    return _as_text(value).replace(_as_text(old), _as_text(new))


def _fn_default(value, fallback):
    """空值兜底 —— 空字符串和 null 都算空。"""
    if value is None:
        return fallback
    if isinstance(value, str) and value == "":
        return fallback
    if isinstance(value, list) and not value:
        return fallback
    return value


def _fn_join(value, sep=","):
    items = value if isinstance(value, list) else [value]
    return _as_text(sep).join(_as_text(i) for i in items)


def _fn_split(value, sep=","):
    text = _as_text(value)
    if sep == "":
        return list(text)
    return text.split(_as_text(sep))


def _fn_len(value):
    if isinstance(value, (list, str, dict)):
        return len(value)
    raise FlowExprError("len 只能对文字、列表、字典用")


def _fn_contains(value, needle):
    if isinstance(value, list):
        return needle in value
    return _as_text(needle) in _as_text(value)


def _fn_index(value, needle):
    if not isinstance(value, list):
        raise FlowExprError("index 只能对列表用")
    try:
        return value.index(needle)
    except ValueError:
        return -1


def _fn_int(value):
    try:
        return int(float(_as_text(value)))
    except (TypeError, ValueError):
        raise FlowExprError("int 的参数不是数字") from None


def _fn_number(value):
    try:
        return float(_as_text(value))
    except (TypeError, ValueError):
        raise FlowExprError("number 的参数不是数字") from None


def _fn_first(value):
    if not isinstance(value, list) or not value:
        raise FlowExprError("first 要对非空列表用")
    return value[0]


def _fn_last(value):
    if not isinstance(value, list) or not value:
        raise FlowExprError("last 要对非空列表用")
    return value[-1]


def _fn_now():
    from app.timeutils import utcnow

    return utcnow().isoformat(timespec="seconds")


def _fn_today(fmt="%Y-%m-%d"):
    """今天的日期 —— 可指定格式。"""
    from datetime import datetime

    return datetime.now().strftime(_as_text(fmt))


def _fn_date(value, fmt="%Y-%m-%d"):
    """把一个日期字符串按格式重新排 —— 管道里写 | date("%Y/%m/%d")。"""
    from datetime import datetime

    text = _as_text(value)
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).strftime(_as_text(fmt))
        except ValueError:
            continue
    raise FlowExprError(f"date 看不懂这个日期：{text!r}")


# 函数名 → 函数规格
#   func        实现
#   low/high    参数个数范围（**含管道带上来的那个值**：
#               管道里 | truncate(80) 是 2 个参数（值 + 长度），
#               直接写 truncate(值, 80) 也是 2 个 —— 两种写法一致）
#   takes_value 管道传进来的值算不算第一个参数
#               （now / today 不吃管道值 —— 管道对它们没意义）
_FUNCTIONS: dict[str, dict] = {
    "trim": {"func": _fn_trim, "low": 1, "high": 1, "takes_value": True},
    "upper": {"func": _fn_upper, "low": 1, "high": 1, "takes_value": True},
    "lower": {"func": _fn_lower, "low": 1, "high": 1, "takes_value": True},
    "truncate": {"func": _fn_truncate, "low": 2, "high": 2, "takes_value": True},
    "replace": {"func": _fn_replace, "low": 3, "high": 3, "takes_value": True},
    "default": {"func": _fn_default, "low": 2, "high": 2, "takes_value": True},
    "join": {"func": _fn_join, "low": 1, "high": 2, "takes_value": True},
    "split": {"func": _fn_split, "low": 1, "high": 2, "takes_value": True},
    "len": {"func": _fn_len, "low": 1, "high": 1, "takes_value": True},
    "contains": {"func": _fn_contains, "low": 2, "high": 2, "takes_value": True},
    "index": {"func": _fn_index, "low": 2, "high": 2, "takes_value": True},
    "int": {"func": _fn_int, "low": 1, "high": 1, "takes_value": True},
    "number": {"func": _fn_number, "low": 1, "high": 1, "takes_value": True},
    "first": {"func": _fn_first, "low": 1, "high": 1, "takes_value": True},
    "last": {"func": _fn_last, "low": 1, "high": 1, "takes_value": True},
    "today": {"func": _fn_today, "low": 0, "high": 1, "takes_value": False},
    "date": {"func": _fn_date, "low": 1, "high": 2, "takes_value": True},
    "now": {"func": _fn_now, "low": 0, "high": 0, "takes_value": False},
}

FUNCTION_NAMES = sorted(_FUNCTIONS)


# ── 求值 ──────────────────────────────────────────────────────────────


def evaluate(node: object, ctx: dict):
    """求值一棵语法树。出错抛 FlowExprError（消息面向使用者）。"""
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, Path):
        return resolve_ref(node.text, ctx)
    if isinstance(node, Call):
        return _call(node.name, node.args, ctx)
    if isinstance(node, Pipe):
        value = evaluate(node.base, ctx)
        for name, args in node.stages:
            # 管道约定：上一步的值当第一个参数（now/today 这类不吃值的除外）
            spec = _FUNCTIONS.get(name)
            takes = bool(spec and spec["takes_value"])
            extra = [evaluate(a, ctx) for a in (args or [])]
            values = ([value] if takes else []) + extra
            value = _call_values(name, values)
        return value
    if isinstance(node, Compare):
        return _compare(node, ctx)
    if isinstance(node, Ternary):
        cond = evaluate(node.cond, ctx)
        if not isinstance(cond, bool):
            raise FlowExprError(
                "「?」前面的条件要是比较结果（用 == / != / > 这类运算符）"
            )
        return evaluate(node.then if cond else node.otherwise, ctx)
    raise FlowExprError("表达式解析出了不认识的东西")


def _call(name: str, args: list, ctx: dict):
    """调用函数 —— args 是语法树节点（未求值）。"""
    spec = _FUNCTIONS.get(name)
    if spec is None:
        raise FlowExprError(
            f"没有「{name}」这个函数"
            f"（可用的：{', '.join(FUNCTION_NAMES)}）"
        )
    values = [evaluate(a, ctx) for a in args]
    return _call_values(name, values)


def _call_values(name: str, values: list):
    """调用函数 —— values 是已求值的实参。管道走这条路。"""
    spec = _FUNCTIONS.get(name)
    if spec is None:
        raise FlowExprError(
            f"没有「{name}」这个函数"
            f"（可用的：{', '.join(FUNCTION_NAMES)}）"
        )
    func, low, high = spec["func"], spec["low"], spec["high"]
    if len(values) < low or len(values) > high:
        raise FlowExprError(
            f"「{name}」要 {_arg_expect(spec)} 参数，现在给了 {len(values)} 个"
        )
    return func(*values)


def _compare(node: Compare, ctx: dict):
    left = evaluate(node.left, ctx)
    right = evaluate(node.right, ctx)
    op = node.op
    if op == "==":
        return _loose_eq(left, right)
    if op == "!=":
        return not _loose_eq(left, right)
    if op in (">", ">=", "<", "<="):
        return _compare_order(op, left, right)
    raise FlowExprError(f"不认识的比较运算符：{op}")


def _loose_eq(left, right) -> bool:
    """相等比较 —— 数字与数字字符串按数值比（用户填的输入框都是字符串）。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right or left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return left == right


def _compare_order(op: str, left, right) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        a, b = float(left), float(right)
    else:
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            a, b = _as_text(left), _as_text(right)
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    return a <= b


# ── 对外接口 ──────────────────────────────────────────────────────────


def parse(text: str) -> object:
    """解析表达式 → 语法树。语法错抛 FlowExprError。"""
    return _Parser(text).parse()


def eval_expr(text: str, ctx: dict):
    """解析并求值。"""
    return evaluate(parse(text), ctx)


def collect_paths(node: object) -> list[str]:
    """列出语法树里引用了哪些路径 —— 校验层靠它判断引用是否成立。"""
    out: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, Path):
            if n.text not in out:
                out.append(n.text)
        elif isinstance(n, Call):
            for a in n.args:
                walk(a)
        elif isinstance(n, Pipe):
            walk(n.base)
            for _name, args in n.stages:
                for a in args or []:
                    walk(a)
        elif isinstance(n, Compare):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, Ternary):
            walk(n.cond)
            walk(n.then)
            walk(n.otherwise)

    walk(node)
    return out


def check(node: object) -> list[str]:
    """静态检查 —— 返回问题列表（空列表 = 没问题）。

    校验层在保存流程时调用：函数名不存在、参数个数不对，都当场报出来，
    别等跑到那一步、引擎都花钱了才发现。
    """
    problems: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, Call):
            spec = _FUNCTIONS.get(n.name)
            if spec is None:
                problems.append(
                    f"没有「{n.name}」这个函数（可用的：{', '.join(FUNCTION_NAMES)}）"
                )
            elif not (spec["low"] <= len(n.args) <= spec["high"]):
                problems.append(
                    f"「{n.name}」要 {_arg_expect(spec)} 参数，现在给了 {len(n.args)} 个"
                )
            for a in n.args:
                walk(a)
        elif isinstance(n, Pipe):
            walk(n.base)
            for name, args in n.stages:
                spec = _FUNCTIONS.get(name)
                if spec is None:
                    problems.append(
                        f"没有「{name}」这个函数（可用的：{', '.join(FUNCTION_NAMES)}）"
                    )
                else:
                    # 管道把上一步的值算进去（now/today 不吃这个值）
                    given = len(args or []) + (1 if spec["takes_value"] else 0)
                    if not (spec["low"] <= given <= spec["high"]):
                        problems.append(
                            f"「{name}」要 {_arg_expect(spec)} 参数，现在给了 {given} 个"
                        )
                for a in args or []:
                    walk(a)
        elif isinstance(n, Compare):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, Ternary):
            walk(n.cond)
            walk(n.then)
            walk(n.otherwise)

    walk(node)
    return problems


def _arg_expect(spec: dict) -> str:
    low, high = spec["low"], spec["high"]
    return f"{low} 个" if low == high else f"{low}~{high} 个"
