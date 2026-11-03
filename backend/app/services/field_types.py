"""节点字段类型的归一 —— 流程侧共用。

**为什么需要**：模板的 `input_schema` 是自由 JSON，库里可能存着历史别名
`image_upload`（当初只有图片能上传）。表单提交那条路有
`schemas.normalize_input_schema` 做归一，但**流程的校验和执行器是直接读
库里的原始 schema** —— 于是 `image_upload` 字段在流程里既接不了上游产物，
校验还会误报「只有 file_upload 类型的字段能接」。

修法不是在这两处各打一个补丁，而是让流程侧统一走一个函数：字段类型只有
一种判断口径。将来再加别名（比如 `video_upload`），改这一处就够。
"""
from app.schemas.schemas import normalize_input_schema

# 会被当成文件字段的历史别名
_FILE_ALIASES = ("image_upload", "file_upload")


def normalized_field_map(input_schema) -> dict:
    """归一后的字段表：{字段名: 字段定义}。

    字段定义里的 `type` 已经是归一后的值（`image_upload` → `file_upload`），
    并补齐了 label / required / accept 等缺省键。
    """
    if isinstance(input_schema, dict):
        raw = input_schema.get("fields", [])
    elif isinstance(input_schema, list):
        raw = input_schema
    else:
        raw = []
    normalized = normalize_input_schema({"fields": raw})
    return {
        str(f.get("name")): f
        for f in normalized.get("fields", [])
        if isinstance(f, dict) and f.get("name")
    }


def is_file_field(spec: dict) -> bool:
    """这个字段是不是「接素材」的字段（图片 / 文件）。"""
    return bool(spec) and spec.get("type") in _FILE_ALIASES
