"""流程表达式求值 —— 离线纯函数。

三条底线：
1. 不用 eval：只做受限的路径解析，取值只走字典下标，不碰 getattr
2. 整体引用保留原始类型（上游产物是列表就该拿到列表，不能变成字符串）
3. 引用不存在要**报错**，不能静默返回 None —— 静默传个 None 给引擎，
   失败会发生在引擎侧，排查成本高一个数量级
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.flow_expr import (  # noqa: E402
    FlowExprError,
    collect_refs,
    resolve_value,
)


CTX = {
    "input": {
        "prompt": "拉布拉多拉拖拉机",
        "count": 3,
        "empty": "",
    },
    "steps": {
        "gen": {
            "outputs": [
                {"asset_id": "a1", "filename": "a.png"},
                {"asset_id": "a2", "filename": "b.png"},
            ]
        }
    },
}


def test_literal_passthrough() -> None:
    """非字符串原样返回；不含 {{}} 的字符串也是字面量。"""
    assert resolve_value("2K", CTX) == "2K"
    assert resolve_value(1024, CTX) == 1024
    assert resolve_value(True, CTX) is True
    assert resolve_value(["a", "b"], CTX) == ["a", "b"]
    assert resolve_value(None, CTX) is None


def test_whole_reference_keeps_type() -> None:
    """整个取值就是一个 {{...}} → 返回原始类型。"""
    assert resolve_value("{{ input.prompt }}", CTX) == "拉布拉多拉拖拉机"
    assert resolve_value("{{ input.count }}", CTX) == 3
    outs = resolve_value("{{ steps.gen.outputs }}", CTX)
    assert isinstance(outs, list) and len(outs) == 2
    assert outs[0]["asset_id"] == "a1"


def test_interpolation() -> None:
    """文本里夹着 {{...}} → 字符串插值。"""
    assert resolve_value("画一只{{ input.prompt }}的狗", CTX) == "画一只拉布拉多拉拖拉机的狗"
    assert resolve_value("{{ input.prompt }}，共 {{ input.count }} 张", CTX) == "拉布拉多拉拖拉机，共 3 张"


def test_missing_reference_raises() -> None:
    """引用不存在必须报错，不能静默给 None。"""
    for expr in ("{{ input.nope }}", "{{ steps.zzz.outputs }}", "{{ steps.gen.nope }}"):
        try:
            resolve_value(expr, CTX)
        except FlowExprError:
            continue
        raise AssertionError(f"{expr} 应当报错")


def test_bad_path_rejected() -> None:
    """路径形状不对要报错 —— 不能去试 getattr/属性链。"""
    for expr in ("{{ __class__ }}", "{{ input.__class__ }}", "{{ a b }}", "{{ 1abc }}"):
        try:
            resolve_value(expr, CTX)
        except FlowExprError:
            continue
        raise AssertionError(f"{expr} 应当被拒")


def test_list_into_text_rejected() -> None:
    """把列表插进句子没有意义，明确报错而不是产出不可预期的文本。"""
    try:
        resolve_value("前缀 {{ steps.gen.outputs }}", CTX)
    except FlowExprError:
        return
    raise AssertionError("列表插进文本应当报错")


def test_collect_refs() -> None:
    """校验时用它列引用 —— 得能同时认出整体引用和夹在文本里的。"""
    assert collect_refs("{{ input.prompt }}") == ["input.prompt"]
    assert collect_refs("a {{ input.prompt }} b {{ steps.gen.outputs }}") == [
        "input.prompt",
        "steps.gen.outputs",
    ]
    assert collect_refs("纯字面量") == []
    assert collect_refs(1024) == []


def test_list_first_and_last() -> None:
    """从上游产物里取特定一张。

    ComfyUI 一个工作流常出好几张图，下游只要其中一张时必须能指名 ——
    否则只能全喂过去，然后报「字段只接一张图，但上游产出了 N 张」。
    """
    assert resolve_value("{{ steps.gen.outputs.first }}", CTX) == {
        "asset_id": "a1",
        "filename": "a.png",
    }
    assert resolve_value("{{ steps.gen.outputs.last }}", CTX) == {
        "asset_id": "a2",
        "filename": "b.png",
    }
    assert resolve_value("{{ steps.gen.outputs.1 }}", CTX) == {
        "asset_id": "a2",
        "filename": "b.png",
    }


def test_list_index_out_of_range() -> None:
    """下标越界要报错，不能静默返回 None。"""
    try:
        resolve_value("{{ steps.gen.outputs.9 }}", CTX)
    except FlowExprError as exc:
        assert "越界" in str(exc), str(exc)
    else:
        raise AssertionError("越界应该报错")


def test_list_pick_on_empty_list() -> None:
    """上游没有产物时取 first 要报错 —— 空列表取不到东西。"""
    empty = {"steps": {"gen": {"outputs": []}}}
    try:
        resolve_value("{{ steps.gen.outputs.first }}", empty)
    except FlowExprError as exc:
        assert "没有产物" in str(exc), str(exc)
    else:
        raise AssertionError("空列表取 first 应该报错")


def test_list_rejects_unknown_key() -> None:
    """列表上不能按名字取 —— 只认 first / last / 数字下标。"""
    try:
        resolve_value("{{ steps.gen.outputs.middle }}", CTX)
    except FlowExprError as exc:
        assert "first" in str(exc), str(exc)
    else:
        raise AssertionError("未知列表键应该报错")


def test_whole_reference_with_pick_keeps_type() -> None:
    """整体引用 .first 保留原始类型（拿到的是 dict，不是字符串）。"""
    value = resolve_value("{{ steps.gen.outputs.first }}", CTX)
    assert isinstance(value, dict)
    assert value["asset_id"] == "a1"


def main() -> int:
    tests = [
        test_literal_passthrough,
        test_whole_reference_keeps_type,
        test_interpolation,
        test_missing_reference_raises,
        test_bad_path_rejected,
        test_list_into_text_rejected,
        test_collect_refs,
        test_list_first_and_last,
        test_list_index_out_of_range,
        test_list_pick_on_empty_list,
        test_list_rejects_unknown_key,
        test_whole_reference_with_pick_keeps_type,
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
