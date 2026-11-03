"""random_number 字段的哨兵值（-1）展开。

约定：用户在「随机数」字段里填 -1 表示「这次不要固定值，每次生成都换一个」——
ComfyUI 生态里 seed=-1 就是这个意思，前端表单也照这个提示。

两件事只在提交链路上做，不散到插件里：

1. **展开**：把 -1 换成一个落在 [min, max] 内的真实整数。插件收到的永远是
   确定的数字，不必为「随机」各写一遍；批量生成时每个任务各自展开，N 张图
   不会因为同种子而长得一模一样。
2. **回写**：展开后的值会存进 `task.params`。任务详情显示的就是真正用掉的
   种子，「再生成」也按它复现 —— 若把 -1 原样落库，用户事后既不知道用了
   什么，也追不回那张图。

边界语义：只有 -1 **不在** [min, max] 区间内时才当哨兵。字段把下界配成
-5 时，-1 是一个合法取值，按原样下发。
"""
from __future__ import annotations

import math
import random
from typing import Any

RANDOM_SENTINEL = -1
DEFAULT_RANDOM_MIN = 0
# 与前端 DEFAULT_RANDOM_MAX 对齐。上界是 2^31-1 而不是 2^64-1：任务参数要
# 经 JSON 回前端展示，「再生成」时又要走一遍表单，超过 2^53-1 的值会丢精度，
# 显示出来的种子和实际用的对不上。
DEFAULT_RANDOM_MAX = 2_147_483_647
MAX_SAFE_INTEGER = 2**53 - 1

_FIELD_TYPE = "random_number"


def _bound(field: dict, key: str, fallback: int) -> int | float:
    """读字段上的数值边界。bool 是 int 的子类，必须挡掉（True 会变成 1）"""
    raw = field.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    if isinstance(raw, float) and not math.isfinite(raw):
        return fallback
    return raw


def _normalize_range(low: int | float, high: int | float) -> tuple[int, int]:
    """整数化 + 夹到安全整数 + 上下界传反时交换（与前端 randomInt 行为一致）"""
    lo = max(math.ceil(low), -MAX_SAFE_INTEGER)
    hi = min(math.floor(high), MAX_SAFE_INTEGER)
    if lo > hi:
        lo, hi = hi, lo
    return int(lo), int(hi)


def _as_number(value: Any) -> int | float | None:
    """把参数值当数字看。

    必须接受字符串：模板的 `default` 是文本输入框存下来的，存量模板里就是
    `"seed": "-1"`。只认 int/float 的话，用户在表单里没改这一项、直接提交，
    就会把字符串原样透传下去，哨兵静默失效 —— 看起来「每次随机」其实没生效。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    return None


def is_sentinel(value: Any, lo: int, hi: int) -> bool:
    """该值是不是「请随机」的意思。

    -1 落在合法区间里说明用户就是想传 -1，不替换；其余情况（含 min=0 的常见
    配置）一律视为哨兵。字符串 "-1" 与数字 -1 等价。
    """
    number = _as_number(value)
    if number is None or number != RANDOM_SENTINEL:
        return False
    return not (lo <= RANDOM_SENTINEL <= hi)


def resolve_random_fields(
    input_schema: dict | None,
    params: dict,
    *,
    rng: Any = random,
) -> dict:
    """把 params 里 random_number 字段的 -1 展开成实际随机整数。

    没有需要展开的字段时原样返回入参（省一次拷贝）；否则返回新 dict，
    不修改调用方传进来的 params —— 批量提交时同一个 params 会被复用多次。
    """
    fields = input_schema.get("fields") if isinstance(input_schema, dict) else None
    if not isinstance(fields, list) or not isinstance(params, dict):
        return params

    resolved: dict | None = None
    for field in fields:
        if not isinstance(field, dict) or field.get("type") != _FIELD_TYPE:
            continue
        name = field.get("name")
        if not isinstance(name, str) or name not in params:
            continue
        lo, hi = _normalize_range(
            _bound(field, "min", DEFAULT_RANDOM_MIN),
            _bound(field, "max", DEFAULT_RANDOM_MAX),
        )
        if not is_sentinel(params[name], lo, hi):
            continue
        if resolved is None:
            resolved = dict(params)
        resolved[name] = rng.randint(lo, hi)
    return params if resolved is None else resolved
