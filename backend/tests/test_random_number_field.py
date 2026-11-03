"""随机数字段类型（random_number）的 schema 归一化回归。

背景：input_schema 归一化是**白名单**实现 —— 类型不在 _FIELD_TYPES 里会被
静默降级成 text，未知键会被清除。所以新增控件类型时，前后端必须同时改，
否则模板保存一次，字段就从「数字 + 随机按钮」变成普通文本框，而且不报错。
本测试就是钉住这条契约。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.schemas import normalize_input_schema  # noqa: E402


def one(field: dict) -> dict:
    """归一化单个字段，返回处理后的字段体"""
    result = normalize_input_schema({"fields": [field]})
    assert len(result["fields"]) == 1, result
    return result["fields"][0]


def test_random_number_survives_normalization() -> None:
    field = one({"name": "seed", "label": "随机种子", "type": "random_number"})
    assert field["type"] == "random_number", field
    assert field["label"] == "随机种子", field


def test_min_max_are_preserved() -> None:
    field = one({"name": "seed", "type": "random_number", "min": 0, "max": 4294967295})
    assert field["min"] == 0, field
    assert field["max"] == 4294967295, field


def test_zero_bound_is_not_dropped() -> None:
    # 0 是合法下界（种子就是从 0 开始），不能被当成假值清掉
    field = one({"name": "seed", "type": "random_number", "min": 0})
    assert "min" in field and field["min"] == 0, field


def test_boolean_bound_is_rejected() -> None:
    # bool 是 int 的子类：不显式排除的话 True 会变成 min=1
    field = one({"name": "seed", "type": "random_number", "min": True, "max": False})
    assert "min" not in field and "max" not in field, field


def test_non_numeric_bound_is_rejected() -> None:
    field = one({"name": "seed", "type": "random_number", "min": "0", "max": "100"})
    assert "min" not in field and "max" not in field, field


def test_bounds_only_apply_to_numeric_controls() -> None:
    # 文本框带 min/max 属于脏数据，应被清掉而不是原样存进模板
    field = one({"name": "prompt", "type": "text", "min": 0, "max": 10})
    assert "min" not in field and "max" not in field, field


def test_number_also_accepts_bounds() -> None:
    field = one({"name": "steps", "type": "number", "min": 1, "max": 150})
    assert field["min"] == 1 and field["max"] == 150, field


def test_normalization_is_idempotent() -> None:
    raw = {
        "fields": [
            {"name": "seed", "label": "种子", "type": "random_number", "min": 0, "max": 100, "required": True},
            {"name": "n", "type": "number", "min": 1.5, "max": 9.5},
        ]
    }
    once = normalize_input_schema(raw)
    twice = normalize_input_schema(once)
    assert once == twice, f"{once} != {twice}"


def test_unknown_type_still_falls_back_to_text() -> None:
    # 白名单的兜底行为不能被这次改动带跑偏
    field = one({"name": "x", "type": "bogus_type"})
    assert field["type"] == "text", field


def main() -> int:
    tests = [
        test_random_number_survives_normalization,
        test_min_max_are_preserved,
        test_zero_bound_is_not_dropped,
        test_boolean_bound_is_rejected,
        test_non_numeric_bound_is_rejected,
        test_bounds_only_apply_to_numeric_controls,
        test_number_also_accepts_bounds,
        test_normalization_is_idempotent,
        test_unknown_type_still_falls_back_to_text,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
