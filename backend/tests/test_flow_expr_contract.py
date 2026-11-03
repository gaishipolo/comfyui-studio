"""表达式契约清单 —— 前端能生成的形态，后端必须都认得。

**这是两套实现的最大风险点**：前端 `flowExpr.ts` 和后端 `flow_expr_lang.py`
各有一套语法与格式化。两边对「同一个表达式怎么写」的理解一旦不一致，
界面存进去的表达式后端就解析不了 —— 而且要等到跑流程、引擎都花钱了才炸。

所以这里把**前端会生成的全部形态**列成一份清单，后端逐条断言能解析、
能求值。前端那边有一份**逐字相同**的清单（`frontend/tests/flow_expr.test.ts`
里的 CANONICAL_FORMS）—— **改一边就要改另一边**。

清单本身就是契约。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.flow_expr import (  # noqa: E402
    FlowExprError,
    collect_refs,
    expr_problems,
    resolve_value,
)

# 前端会生成的全部表达式形态 —— 必须与
# frontend/tests/flow_expr.test.ts 的 CANONICAL_FORMS 逐字相同
CANONICAL_FORMS = [
    # 固定值
    "2K",
    "1024",
    # 流程输入
    "{{ input.prompt_a }}",
    "{{ input.prompts[index] }}",
    # 上游产物
    "{{ steps.gen.outputs }}",
    "{{ steps.gen.outputs.first }}",
    "{{ steps.gen.outputs.last }}",
    # 加工链
    "{{ input.prompt_a | trim }}",
    '{{ input.prompt_a | trim | truncate("80") }}',
    "{{ input.prompt_a | upper }}",
    "{{ input.prompt_a | lower }}",
    '{{ input.prompt_a | default("未填写") }}',
    "{{ input.prompt_a | len }}",
    '{{ input.prompt_a | contains("狗") }}',
    "{{ steps.gen.outputs | len }}",
    # 条件
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
    '{{ input.style != "写实" ? input.prompt_a : input.prompt_b }}',
    '{{ input.style == "写实" ? "写实提示词" : input.prompt_b }}',
    # 条件 + 整体加工
    '{{ (input.style == "写实" ? input.prompt_a : input.prompt_b) | trim }}',
]

CTX = {
    "input": {
        "prompt_a": "  一只拉布拉多  ",
        "prompts": ["猫", "狗", "鸟"],
        "style": "写实",
        "prompt_b": "油画提示词",
    },
    "steps": {
        "gen": {
            "outputs": [
                {"asset_id": "a1", "filename": "a.png"},
                {"asset_id": "a2", "filename": "b.png"},
            ]
        }
    },
    "index": 1,
}


def test_every_canonical_form_parses() -> None:
    """清单里每一条后端都认得 —— 不认得就是两边语法理解不一致。"""
    for raw in CANONICAL_FORMS:
        try:
            resolve_value(raw, CTX)
        except FlowExprError as exc:
            raise AssertionError(f"后端解析不了前端会生成的表达式：{raw} → {exc}") from exc


def test_every_canonical_form_passes_static_check() -> None:
    """清单里每一条静态检查都过 —— 前端能生成的，后端不该报错。"""
    for raw in CANONICAL_FORMS:
        problems = expr_problems(raw)
        assert not problems, f"{raw} 被静态检查挑出毛病：{problems}"


def test_canonical_forms_reference_expected_paths() -> None:
    """引用抽取要正确 —— 依赖分层靠它算。"""
    assert collect_refs("{{ input.prompt_a | trim }}") == ["input.prompt_a"]
    assert collect_refs("{{ steps.gen.outputs | len }}") == ["steps.gen.outputs"]
    assert collect_refs(
        '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}'
    ) == ["input.style", "input.prompt_a", "input.prompt_b"]
    assert collect_refs(
        '{{ (input.style == "写实" ? input.prompt_a : input.prompt_b) | trim }}'
    ) == ["input.style", "input.prompt_a", "input.prompt_b"]


def test_canonical_forms_evaluate_correctly() -> None:
    """清单里每一条求值结果都对。"""
    cases = [
        ("{{ input.prompt_a | trim }}", "一只拉布拉多"),
        ('{{ input.prompt_a | trim | truncate("3") }}', "一只拉"),
        ("{{ input.prompt_a | upper }}", "  一只拉布拉多  "),
        ('{{ input.prompt_a | default("未填写") }}', "  一只拉布拉多  "),
        ("{{ steps.gen.outputs | len }}", 2),
        ('{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}', "  一只拉布拉多  "),
        (
            '{{ (input.style == "写实" ? input.prompt_a : input.prompt_b) | trim }}',
            "一只拉布拉多",
        ),
    ]
    for raw, expected in cases:
        actual = resolve_value(raw, CTX)
        assert actual == expected, f"{raw} 求值成 {actual!r}，期望 {expected!r}"


def test_canonical_list_is_not_a_stub() -> None:
    """清单不是空壳 —— 少一条就意味着有一种形态没人验。"""
    assert len(CANONICAL_FORMS) >= 19, "清单不该这么短"


def main() -> int:
    tests = [
        test_every_canonical_form_parses,
        test_every_canonical_form_passes_static_check,
        test_canonical_forms_reference_expected_paths,
        test_canonical_forms_evaluate_correctly,
        test_canonical_list_is_not_a_stub,
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
