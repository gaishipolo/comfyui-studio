"""random_number 的 -1 哨兵展开（提交链路）回归。

约定：表单里随机数字段填 -1 = 「每次生成自动换一个」。真正取值发生在
`api/tasks.py` 提交时，用的是 `services/random_fields.resolve_random_fields`。

这里钉住四件事：
1. -1 一定被换成区间内的真实整数，且不越界、不越安全整数；
2. 批量提交时每个任务各自展开（否则 N 张图同一个种子等于白批量）；
3. -1 如果本来就在字段的合法区间里，就是普通值，不替换；
4. 展开后的参数是新对象，不改调用方传进来的那份。
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.random_fields import (  # noqa: E402
    DEFAULT_RANDOM_MAX,
    MAX_SAFE_INTEGER,
    RANDOM_SENTINEL,
    is_sentinel,
    resolve_random_fields,
)


def schema(*fields: dict) -> dict:
    return {"fields": list(fields)}


def seed_field(**extra: object) -> dict:
    field = {"name": "seed", "label": "种子", "type": "random_number"}
    field.update(extra)
    return field


def rng() -> random.Random:
    return random.Random(20261103)


def test_sentinel_constant_matches_convention() -> None:
    assert RANDOM_SENTINEL == -1, RANDOM_SENTINEL


def test_sentinel_is_expanded_into_range() -> None:
    params = {"seed": RANDOM_SENTINEL}
    out = resolve_random_fields(schema(seed_field(min=0, max=100)), params, rng=rng())
    assert out["seed"] != RANDOM_SENTINEL, out
    assert 0 <= out["seed"] <= 100, out
    assert isinstance(out["seed"], int) and not isinstance(out["seed"], bool), out


def test_missing_bounds_fall_back_to_default_range() -> None:
    out = resolve_random_fields(schema(seed_field()), {"seed": -1}, rng=rng())
    assert 0 <= out["seed"] <= DEFAULT_RANDOM_MAX, out
    assert out["seed"] <= MAX_SAFE_INTEGER


def test_each_batch_task_gets_a_different_seed() -> None:
    # 批量生成走的是同一个 params 展开 N 次，必须各自不同。
    # 注意复用同一个 rng 实例：每次新建同种子的 Random 会得到完全相同的序列，
    # 那样测出来是「都相同」，但那是夹具的错误，不是实现的问题
    sch = schema(seed_field(min=0, max=10**9))
    shared = {"seed": -1, "prompt": "cat"}
    shared_rng = rng()
    seeds = {resolve_random_fields(sch, shared, rng=shared_rng)["seed"] for _ in range(5)}
    assert len(seeds) == 5, seeds


def test_explicit_value_is_left_alone() -> None:
    out = resolve_random_fields(schema(seed_field(min=0, max=100)), {"seed": 42}, rng=rng())
    assert out["seed"] == 42, out


def test_zero_is_not_confused_with_sentinel() -> None:
    # 0 是最常见的合法种子，不能被当成假值重抽
    out = resolve_random_fields(schema(seed_field()), {"seed": 0}, rng=rng())
    assert out["seed"] == 0, out


def test_sentinel_inside_range_is_a_real_value() -> None:
    # 下界配成 -5 时，-1 落在区间内 —— 用户就是想要 -1，不能替换
    out = resolve_random_fields(
        schema(seed_field(min=-5, max=5)), {"seed": -1}, rng=rng()
    )
    assert out["seed"] == -1, out
    assert not is_sentinel(-1, -5, 5)


def test_negative_range_without_sentinel_is_detected() -> None:
    # 区间完全不含 -1 时仍按哨兵处理，展开结果落在区间内
    out = resolve_random_fields(
        schema(seed_field(min=-100, max=-10)), {"seed": -1}, rng=rng()
    )
    assert -100 <= out["seed"] <= -10, out


def test_swapped_bounds_are_normalized() -> None:
    out = resolve_random_fields(schema(seed_field(min=90, max=10)), {"seed": -1}, rng=rng())
    assert 10 <= out["seed"] <= 90, out


def test_degenerate_range_collapses_to_single_value() -> None:
    out = resolve_random_fields(schema(seed_field(min=7, max=7)), {"seed": -1}, rng=rng())
    assert out["seed"] == 7, out


def test_boolean_bounds_are_ignored() -> None:
    # bool 是 int 子类：True 当 min 会让区间变成 [1, 1]
    out = resolve_random_fields(
        schema(seed_field(min=True, max=False)), {"seed": -1}, rng=rng()
    )
    assert 0 <= out["seed"] <= DEFAULT_RANDOM_MAX, out


def test_huge_max_is_clamped_to_safe_integer() -> None:
    # 超过 2^53-1 的值经 JSON 回前端会丢精度，种子「看起来变了其实重复」
    out = resolve_random_fields(
        schema(seed_field(min=0, max=2**64 - 1)), {"seed": -1}, rng=rng()
    )
    assert 0 <= out["seed"] <= MAX_SAFE_INTEGER, out


def test_bool_param_is_not_treated_as_sentinel() -> None:
    out = resolve_random_fields(schema(seed_field()), {"seed": True}, rng=rng())
    assert out["seed"] is True, out


def test_string_sentinel_is_expanded() -> None:
    # 模板的 default 是文本框存下来的，存量模板里就是 "seed": "-1"。
    # 只认数字的话，用户不改这一项直接提交，随机就静默失效了
    out = resolve_random_fields(schema(seed_field(min=0, max=100)), {"seed": "-1"}, rng=rng())
    assert isinstance(out["seed"], int), out
    assert 0 <= out["seed"] <= 100, out


def test_string_sentinel_with_surrounding_spaces() -> None:
    out = resolve_random_fields(schema(seed_field(min=0, max=100)), {"seed": "  -1  "}, rng=rng())
    assert isinstance(out["seed"], int) and 0 <= out["seed"] <= 100, out


def test_string_sentinel_inside_range_is_left_alone() -> None:
    out = resolve_random_fields(schema(seed_field(min=-5, max=5)), {"seed": "-1"}, rng=rng())
    assert out["seed"] == "-1", out


def test_garbage_string_is_left_alone() -> None:
    out = resolve_random_fields(schema(seed_field()), {"seed": "-1abc"}, rng=rng())
    assert out["seed"] == "-1abc", out


def test_non_random_field_keeps_its_minus_one() -> None:
    # 普通数字/文本框里的 -1 是普通值（比如 CFG 步数 -1 表示自动）
    sch = schema(
        {"name": "steps", "type": "number", "min": -100, "max": 100},
        {"name": "note", "type": "text"},
        seed_field(min=0, max=100),
    )
    out = resolve_random_fields(sch, {"steps": -1, "note": "-1", "seed": -1}, rng=rng())
    assert out["steps"] == -1, out
    assert out["note"] == "-1", out
    assert out["seed"] != -1, out


def test_missing_field_in_params_is_ignored() -> None:
    out = resolve_random_fields(schema(seed_field()), {"prompt": "cat"}, rng=rng())
    assert out == {"prompt": "cat"}, out


def test_input_params_are_not_mutated() -> None:
    params = {"seed": -1, "prompt": "cat"}
    resolve_random_fields(schema(seed_field()), params, rng=rng())
    assert params["seed"] == -1, f"入参被就地改写: {params}"


def test_no_sentinel_returns_same_object() -> None:
    # 没有要展开的字段时省掉一次拷贝
    params = {"seed": 12}
    assert resolve_random_fields(schema(seed_field()), params, rng=rng()) is params


def test_malformed_schema_is_tolerated() -> None:
    for broken in (None, {}, {"fields": "oops"}, {"fields": [None, 42, {"type": "random_number"}]}):
        params = {"seed": -1}
        assert resolve_random_fields(broken, params, rng=rng()) == {"seed": -1}, broken


def main() -> int:
    tests = [
        test_sentinel_constant_matches_convention,
        test_sentinel_is_expanded_into_range,
        test_missing_bounds_fall_back_to_default_range,
        test_each_batch_task_gets_a_different_seed,
        test_explicit_value_is_left_alone,
        test_zero_is_not_confused_with_sentinel,
        test_sentinel_inside_range_is_a_real_value,
        test_negative_range_without_sentinel_is_detected,
        test_swapped_bounds_are_normalized,
        test_degenerate_range_collapses_to_single_value,
        test_boolean_bounds_are_ignored,
        test_huge_max_is_clamped_to_safe_integer,
        test_bool_param_is_not_treated_as_sentinel,
        test_string_sentinel_is_expanded,
        test_string_sentinel_with_surrounding_spaces,
        test_string_sentinel_inside_range_is_left_alone,
        test_garbage_string_is_left_alone,
        test_non_random_field_keeps_its_minus_one,
        test_missing_field_in_params_is_ignored,
        test_input_params_are_not_mutated,
        test_no_sentinel_returns_same_object,
        test_malformed_schema_is_tolerated,
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
