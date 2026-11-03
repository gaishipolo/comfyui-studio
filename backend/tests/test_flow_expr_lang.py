"""表达式语言：管道 / 比较 / 条件 —— 离线纯函数。

**这层是 Agent 能生成流程的前提**：流程配置必须能表达
「把提示词截断到 80 字」「用户选了写实风格就用 A 提示词，否则用 B」，
否则 Agent 生成的配置只能是把 A 接到 B，审批也就失去意义。

但**表达力必须受限**：
1. 不 eval、不 getattr、不导入 —— 只有白名单函数 + 字典下标
2. 可静态分析 —— 校验层要能列出它引用了哪些路径、用了哪些函数
3. 错误消息面向使用者 —— 会直接显示在编排界面上
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.flow_expr import (  # noqa: E402
    FlowExprError,
    collect_base_refs,
    collect_refs,
    expr_problems,
    resolve_value,
)
from app.services.flow_expr_lang import FUNCTION_NAMES  # noqa: E402


CTX = {
    "input": {
        "prompt": "  拉布拉多拉拖拉机  ",
        "count": 3,
        "style": "写实",
        "real": "写实提示词",
        "art": "油画提示词",
        "empty": "",
        "prompts": ["猫", "狗", "鸟"],
        "tags": ["a", "b", "c"],
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
    "total": 3,
    "item": "一只狗",
}


def test_pipe_trim() -> None:
    """管道：上一步的值传给下一个函数。"""
    assert resolve_value("{{ input.prompt | trim }}", CTX) == "拉布拉多拉拖拉机"


def test_pipe_chain() -> None:
    """管道可以串多个。"""
    assert resolve_value("{{ input.prompt | trim | truncate(5) }}", CTX) == "拉布拉多拉"


def test_pipe_truncate_length() -> None:
    """truncate 的长度是第二个参数 —— 管道写法是 | truncate(80)。"""
    assert resolve_value("{{ input.prompt | trim | truncate(3) }}", CTX) == "拉布拉"


def test_pipe_upper_lower() -> None:
    assert resolve_value("{{ input.style | upper }}", CTX) == "写实"
    assert resolve_value("{{ item | upper }}", CTX) == "一只狗"


def test_pipe_on_list_output() -> None:
    """管道也能作用在上游产物列表上。"""
    assert resolve_value("{{ steps.gen.outputs | len }}", CTX) == 2


def test_pipe_default_fallback() -> None:
    """default 兜底 —— 空字符串算空。"""
    assert resolve_value('{{ input.empty | default("未填") }}', CTX) == "未填"
    assert resolve_value('{{ input.style | default("未填") }}', CTX) == "写实"


def test_pipe_replace() -> None:
    """replace 要两个参数 —— 值 + 旧 + 新。"""
    assert (
        resolve_value('{{ input.prompt | trim | replace("狗", "猫") }}', CTX)
        == "拉布拉多拉拖拉机"
    )


def test_pipe_join_split() -> None:
    assert resolve_value('{{ input.tags | join(", ") }}', CTX) == "a, b, c"
    assert resolve_value('{{ input.style | split("写") }}', CTX) == ["", "实"]


def test_pipe_contains() -> None:
    assert resolve_value('{{ input.prompt | contains("拉布") }}', CTX) is True
    assert resolve_value('{{ input.prompt | contains("熊猫") }}', CTX) is False


def test_pipe_index_of() -> None:
    assert resolve_value('{{ input.tags | index("b") }}', CTX) == 1
    assert resolve_value('{{ input.tags | index("z") }}', CTX) == -1


def test_compare_ternary() -> None:
    """条件：比较 + 三元。"""
    assert (
        resolve_value('{{ input.style == "写实" ? input.real : input.art }}', CTX)
        == "写实提示词"
    )


def test_ternary_else_branch() -> None:
    ctx = {**CTX, "input": {**CTX["input"], "style": "油画"}}
    assert (
        resolve_value('{{ input.style == "写实" ? input.real : input.art }}', ctx)
        == "油画提示词"
    )


def test_ternary_numeric_compare() -> None:
    """数字比较 —— 输入框来的值是字符串，'3' > '2' 也要按数值比。"""
    assert resolve_value('{{ input.count > 2 ? "多" : "少" }}', CTX) == "多"
    assert resolve_value('{{ input.count < 2 ? "多" : "少" }}', CTX) == "少"


def test_ternary_with_pipe_in_branch() -> None:
    """分支里也可以带管道。"""
    assert (
        resolve_value('{{ input.style == "写实" ? input.prompt | trim : input.art }}', CTX)
        == "拉布拉多拉拖拉机"
    )


def test_ternary_condition_must_be_boolean() -> None:
    """条件不是比较结果要报错 —— 不能写 {{ input.x ? a : b }}。"""
    try:
        resolve_value('{{ input.style ? input.real : input.art }}', CTX)
    except FlowExprError as exc:
        assert "条件" in str(exc), str(exc)
    else:
        raise AssertionError("非布尔条件应该报错")


def test_unknown_function_rejected() -> None:
    """不存在的函数要报错，并列出可用的。"""
    try:
        resolve_value("{{ input.prompt | nonexist }}", CTX)
    except FlowExprError as exc:
        assert "nonexist" in str(exc), str(exc)
        assert "trim" in str(exc), str(exc)
    else:
        raise AssertionError("不存在的函数应该报错")


def test_function_arity_checked() -> None:
    """参数个数不对要报错 —— 静态就能看出来，不用真跑。"""
    problems = expr_problems("{{ input.prompt | truncate }}")
    assert problems, "truncate 少一个参数应该被静态检查抓到"
    assert "参数" in problems[0], problems[0]


def test_collect_refs_through_pipe() -> None:
    """校验靠它列引用 —— 管道后面的路径也要算进去。"""
    assert collect_refs("{{ input.prompt | trim | truncate(5) }}") == ["input.prompt"]


def test_collect_refs_through_ternary() -> None:
    """条件里的三个路径都要算进去。"""
    refs = collect_refs('{{ input.style == "写实" ? input.real : input.art }}')
    assert refs == ["input.style", "input.real", "input.art"]


def test_collect_refs_through_branch_reference() -> None:
    """分支里引用上游步骤 —— 依赖关系要能算出来。"""
    refs = collect_refs('{{ input.count > 1 ? steps.gen.outputs : steps.edit.outputs }}')
    assert "steps.gen.outputs" in refs
    assert "steps.edit.outputs" in refs


def test_collect_base_refs_strips_index() -> None:
    """方括号下标里的变量名不是流程输入 —— 校验时不能去查它。"""
    refs = collect_base_refs("{{ input.prompts[index] | upper }}")
    assert "input.prompts" in refs
    assert "index" not in refs


def test_expr_problems_empty_for_valid() -> None:
    assert expr_problems("{{ input.prompt | trim }}") == []
    assert expr_problems('{{ input.style == "写实" ? input.real : input.art }}') == []
    assert expr_problems("纯字面量") == []


def test_expr_problems_reports_syntax_error() -> None:
    problems = expr_problems("{{ input.prompt | }}")
    assert problems, "管道后面缺函数名应该被静态检查抓到"


def test_expr_problems_reports_unknown_function() -> None:
    problems = expr_problems("{{ input.prompt | nonexist }}")
    assert problems
    assert "nonexist" in problems[0]


def test_no_code_execution() -> None:
    """**最重要的一条**：表达式不能执行任意代码。

    表达式将来是 Agent 生成的 —— 任意代码执行路径不可接受。
    """
    for expr in (
        "{{ __import__('os').system('echo pwned') }}",
        "{{ input.prompt.__class__ }}",
        "{{ ().__class__.__bases__ }}",
        "{{ open('/etc/passwd') }}",
        "{{ eval('1+1') }}",
    ):
        try:
            resolve_value(expr, CTX)
        except FlowExprError:
            continue
        raise AssertionError(f"{expr} 应当被拒绝")


def test_function_whitelist_is_fixed() -> None:
    """白名单是封闭集合 —— 改它要改代码，不是配置。"""
    assert "trim" in FUNCTION_NAMES
    assert "truncate" in FUNCTION_NAMES
    for forbidden in ("eval", "exec", "open", "import", "getattr", "compile"):
        assert forbidden not in FUNCTION_NAMES


def test_literal_string_with_chinese() -> None:
    """中文不能被转义搞乱（unicode_escape 会把 UTF-8 当 latin-1）。"""
    assert resolve_value('{{ input.style == "写实" ? "对" : "错" }}', CTX) == "对"


def test_interpolation_with_pipe() -> None:
    """文本里夹着带管道的引用 —— 结果是字符串。"""
    assert (
        resolve_value("提示词：{{ input.prompt | trim }} 结束", CTX)
        == "提示词：拉布拉多拉拖拉机 结束"
    )


def test_whole_reference_with_pipe_keeps_type() -> None:
    """整体引用带管道 —— 结果仍然是原始类型（数字不是字符串）。"""
    value = resolve_value("{{ input.count }}", CTX)
    assert isinstance(value, int)
    value2 = resolve_value("{{ input.tags | len }}", CTX)
    assert isinstance(value2, int)


def test_today_and_now() -> None:
    """日期函数 —— 只检查能跑通且形状对。"""
    today = resolve_value('{{ today("%Y-%m-%d") }}', CTX)
    assert len(today) == 10 and "-" in today
    now = resolve_value("{{ now() }}", CTX)
    assert "T" in now or " " in now


def main() -> int:
    tests = [
        test_pipe_trim,
        test_pipe_chain,
        test_pipe_truncate_length,
        test_pipe_upper_lower,
        test_pipe_on_list_output,
        test_pipe_default_fallback,
        test_pipe_replace,
        test_pipe_join_split,
        test_pipe_contains,
        test_pipe_index_of,
        test_compare_ternary,
        test_ternary_else_branch,
        test_ternary_numeric_compare,
        test_ternary_with_pipe_in_branch,
        test_ternary_condition_must_be_boolean,
        test_unknown_function_rejected,
        test_function_arity_checked,
        test_collect_refs_through_pipe,
        test_collect_refs_through_ternary,
        test_collect_refs_through_branch_reference,
        test_collect_base_refs_strips_index,
        test_expr_problems_empty_for_valid,
        test_expr_problems_reports_syntax_error,
        test_expr_problems_reports_unknown_function,
        test_no_code_execution,
        test_function_whitelist_is_fixed,
        test_literal_string_with_chinese,
        test_interpolation_with_pipe,
        test_whole_reference_with_pipe_keeps_type,
        test_today_and_now,
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
