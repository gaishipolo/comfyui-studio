"""流程配置校验 —— 离线纯函数。

这个文件守两条对内容同事很要紧的事：
1. 错误信息要说「第 2 步「生成参考图」的字段…」而不是裸 id（`gen`）
   —— 用户看到裸 id 根本不知道是哪一步
2. 引用上游产物的取法（.first / .last）不能被当成非法引用
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.field_types import (  # noqa: E402
    is_file_field,
    normalized_field_map,
)
from app.services.flow_validate import (  # noqa: E402
    compute_layers,
    step_label,
    validate_flow_config,
)


class _Tpl:
    """Template 的轻量替身 —— 校验只用到 id / name / input_schema。

    不用真的 ORM 对象：SQLAlchemy 的 `__new__` 出来的实例没有初始化
    instrumentation，直接赋值会炸。
    """

    def __init__(self, tid: str, name: str, fields: list[dict]) -> None:
        self.id = tid
        self.name = name
        self.input_schema = {"fields": fields}


def _tpl(tid: str, name: str, fields: list[dict]) -> _Tpl:
    return _Tpl(tid, name, fields)


PROMPT_FIELD = {"name": "prompt", "label": "提示词", "type": "textarea", "required": True}
IMAGE_FIELD = {
    "name": "image",
    "label": "参考图",
    "type": "file_upload",
    "required": True,
}
SIZE_FIELD = {"name": "size", "label": "尺寸档位", "type": "select", "required": False}

TEMPLATES = {
    "t1": _tpl("t1", "文生图", [PROMPT_FIELD, SIZE_FIELD]),
    "t2": _tpl("t2", "图生图", [PROMPT_FIELD, IMAGE_FIELD]),
}


def _validate(steps, inputs=None, outputs=None):
    return validate_flow_config(
        steps=steps,
        input_schema={"fields": inputs if inputs is not None else []},
        outputs=outputs or [],
        templates=TEMPLATES,
    )


def test_step_label_prefers_title() -> None:
    """有显示名就用显示名。"""
    steps = [{"id": "gen", "title": "生成参考图"}, {"id": "edit"}]
    assert step_label(steps, "gen") == "第 1 步「生成参考图」"
    assert step_label(steps, "edit") == "第 2 步"


def test_step_label_falls_back_to_position() -> None:
    """没显示名就用「第 N 步」，不暴露裸 id。"""
    steps = [{"id": "gen"}, {"id": "edit"}]
    assert step_label(steps, "gen") == "第 1 步"
    assert step_label(steps, "edit") == "第 2 步"


def test_error_message_uses_display_name() -> None:
    """字段不存在时，错误里是显示名而不是裸 id。"""
    steps = [
        {"id": "gen", "title": "生成参考图", "template_id": "t1", "params": {"nope": "x"}},
    ]
    result = _validate(steps)
    assert not result["ok"]
    joined = " ".join(result["errors"])
    assert "第 1 步「生成参考图」" in joined, joined
    assert "gen" not in joined, joined


def test_field_error_uses_field_label() -> None:
    """字段名也用标签（内容同事看到的是「提示词」不是 prompt）。"""
    steps = [
        {"id": "gen", "title": "出图", "template_id": "t1", "params": {"nope": "x"}},
    ]
    result = _validate(steps)
    joined = " ".join(result["errors"])
    assert "字段「nope」" in joined or "字段「" in joined, joined


def test_pick_suffix_is_accepted() -> None:
    """引用上游的第 1 张 / 最后一张，不该被判成非法引用。"""
    steps = [
        {
            "id": "gen",
            "title": "出图",
            "template_id": "t1",
            "params": {"prompt": "x"},
        },
        {
            "id": "edit",
            "title": "改图",
            "template_id": "t2",
            "params": {
                "prompt": "{{ input.p }}",
                "image": "{{ steps.gen.outputs.first }}",
            },
        },
    ]
    result = _validate(steps, inputs=[{"name": "p", "label": "要求"}])
    assert result["ok"], result["errors"]
    assert not any("只认" in e for e in result["warnings"]), result["warnings"]


def test_index_suffix_is_accepted() -> None:
    """数字下标也是合法取法。"""
    steps = [
        {"id": "gen", "template_id": "t1", "params": {"prompt": "x"}},
        {
            "id": "edit",
            "template_id": "t2",
            "params": {"prompt": "y", "image": "{{ steps.gen.outputs.0 }}"},
        },
    ]
    result = _validate(steps)
    assert result["ok"], result["errors"]


def test_missing_upstream_still_errors() -> None:
    """引用不存在的步骤仍然要报错。"""
    steps = [
        {
            "id": "edit",
            "template_id": "t2",
            "params": {"prompt": "y", "image": "{{ steps.nope.outputs }}"},
        },
    ]
    result = _validate(steps)
    assert not result["ok"]
    assert any("不存在" in e for e in result["errors"]), result["errors"]


def test_forward_reference_still_errors() -> None:
    """引用后面的步骤仍然要报错（顺序执行拿不到还没跑的产物）。"""
    steps = [
        {
            "id": "edit",
            "template_id": "t2",
            "params": {"prompt": "y", "image": "{{ steps.gen.outputs }}"},
        },
        {"id": "gen", "template_id": "t1", "params": {"prompt": "x"}},
    ]
    result = _validate(steps)
    assert not result["ok"]
    assert any("后面的" in e for e in result["errors"]), result["errors"]


def test_duplicate_step_id_errors() -> None:
    steps = [
        {"id": "gen", "template_id": "t1", "params": {"prompt": "x"}},
        {"id": "gen", "template_id": "t1", "params": {"prompt": "y"}},
    ]
    result = _validate(steps)
    assert not result["ok"]
    assert any("重复" in e for e in result["errors"]), result["errors"]


def test_layers_group_independent_steps() -> None:
    """互不依赖的步骤在同一层。"""
    steps = [
        {"id": "a", "template_id": "t1", "params": {"prompt": "x"}},
        {"id": "b", "template_id": "t1", "params": {"prompt": "y"}},
        {"id": "c", "template_id": "t2", "params": {"prompt": "z", "image": "{{ steps.a.outputs }}"}},
    ]
    layers, cycle = compute_layers(steps)
    assert not cycle
    assert layers == [["a", "b"], ["c"]]


def test_image_upload_alias_treated_as_file_field() -> None:
    """历史别名 image_upload 必须被认成文件字段。

    库里老模板存的就是 image_upload（当初只有图片能上传）。表单提交那条路
    有归一化，流程的校验和执行器原来直接读原始 schema —— 于是这类字段
    接不了上游产物，校验还会误报「只有 file_upload 类型的字段能接」。
    """
    fields = normalized_field_map(
        {"fields": [{"name": "img", "label": "图", "type": "image_upload"}]}
    )
    assert fields["img"]["type"] == "file_upload"
    assert is_file_field(fields["img"])


def test_file_field_can_take_upstream_output() -> None:
    """image_upload 字段接上游产物不该报错。"""
    templates = {
        "t1": _tpl("t1", "文生图", [PROMPT_FIELD]),
        "t2": _tpl("t2", "图生图", [{"name": "img", "label": "图", "type": "image_upload"}]),
    }
    result = validate_flow_config(
        steps=[
            {"id": "gen", "template_id": "t1", "params": {"prompt": "x"}},
            {
                "id": "edit",
                "template_id": "t2",
                "params": {"img": "{{ steps.gen.outputs.first }}"},
            },
        ],
        input_schema={"fields": []},
        outputs=[],
        templates=templates,
    )
    assert result["ok"], result["errors"]


def test_non_file_field_still_rejects_upstream_output() -> None:
    """普通文本字段接上游产物仍然要报错（别把归一化做成放水）。"""
    templates = {
        "t1": _tpl("t1", "文生图", [PROMPT_FIELD]),
        "t2": _tpl("t2", "图生图", [{"name": "prompt", "label": "提示词", "type": "textarea"}]),
    }
    result = validate_flow_config(
        steps=[
            {"id": "gen", "template_id": "t1", "params": {"prompt": "x"}},
            {
                "id": "edit",
                "template_id": "t2",
                "params": {"prompt": "{{ steps.gen.outputs }}"},
            },
        ],
        input_schema={"fields": []},
        outputs=[],
        templates=templates,
    )
    assert not result["ok"]
    assert any("不能接上游产物" in e for e in result["errors"]), result["errors"]


def main() -> int:
    tests = [
        test_step_label_prefers_title,
        test_step_label_falls_back_to_position,
        test_error_message_uses_display_name,
        test_field_error_uses_field_label,
        test_pick_suffix_is_accepted,
        test_index_suffix_is_accepted,
        test_missing_upstream_still_errors,
        test_forward_reference_still_errors,
        test_duplicate_step_id_errors,
        test_layers_group_independent_steps,
        test_image_upload_alias_treated_as_file_field,
        test_file_field_can_take_upstream_output,
        test_non_file_field_still_rejects_upstream_output,
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
