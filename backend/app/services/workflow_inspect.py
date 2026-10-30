"""工作流解析 —— 把 ComfyUI 工作流 JSON 拆成界面可读的三份清单

ComfyUI 的 API 格式是 ``{节点ID: {class_type, inputs, _meta}}``：

- ``inputs`` 里的**字面量**（str/int/float/bool）才是能注入的参数，
  例如 ``76.inputs.value`` 就是那句提示词。
- ``inputs`` 里的**列表**（``[节点ID, 输出序号]``）是节点之间的连线，
  属于图的结构，不是可调参数 —— 所以不进「可注入输入」清单，
  列出来只会让人以为能改。
- ``_meta.title`` 是用户在 ComfyUI 里给节点起的名字（「Prompt」「加载CLIP」），
  比 ``class_type``（``PrimitiveStringMultiline``）好懂得多，界面上优先用它。

节点 ID 可能带冒号（实测真有 ``119:75``），排序和路径拼接都不能假设是纯数字。
"""
from __future__ import annotations

# 出图节点：ComfyUI history 的 outputs 里会冒出来的节点。
# 靠 class_type 白名单 + 「有 images 输入」兜底，
# 后者能接住自定义/第三方输出节点（SaveImage 的输入槽就叫 images）。
OUTPUT_CLASS_TYPES = {
    "SaveImage",
    "PreviewImage",
    "SaveImageWebsocket",
    "SaveImageExtended",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
    "SaveVideo",
    "SaveAudio",
    "SaveAudioMP3",
    "SaveAudioOpus",
    "VHS_VideoCombine",
    "ETN_SaveImage",
    "Image Save",
}

_VALUE_PREVIEW_LIMIT = 48


def _node_sort_key(node_id: str):
    """按自然顺序排节点：纯数字段按数字比，其余按字符串比，兼容 '119:75' 这种复合 ID。"""
    key = []
    for part in str(node_id).split(":"):
        try:
            key.append((0, int(part), ""))
        except ValueError:
            key.append((1, 0, part))
    return key


def _is_literal(value) -> bool:
    return isinstance(value, (str, int, float, bool))


def _preview(value) -> str:
    """只给下拉框当标签用，长文本截断 —— 完整值在工作流文件里，不需要走接口。"""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > _VALUE_PREVIEW_LIMIT:
        return text[: _VALUE_PREVIEW_LIMIT - 1] + "…"
    return text


def _title_of(node: dict, fallback: str) -> str:
    meta = node.get("_meta")
    if isinstance(meta, dict):
        title = meta.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return fallback


def is_output_node(node: dict) -> bool:
    """这个节点会不会产出结果（图片/音频/视频）？"""
    if node.get("class_type") in OUTPUT_CLASS_TYPES:
        return True
    return isinstance(node.get("inputs"), dict) and "images" in node["inputs"]


def inspect_workflow(workflow: dict) -> dict:
    """解析工作流，返回三份清单。

    Returns
    -------
    {"nodes": [...], "inputs": [...], "outputs": [...]}

    Raises
    ------
    ValueError
        不是 ComfyUI 节点字典，或一个节点都没有。
    """
    if not isinstance(workflow, dict):
        raise ValueError("工作流格式不正确：顶层应是节点字典")
    if not workflow:
        raise ValueError("工作流里没有节点")

    nodes: list[dict] = []
    inputs: list[dict] = []
    outputs: list[dict] = []

    for node_id in sorted(workflow.keys(), key=_node_sort_key):
        node = workflow[node_id]
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type", "")
        title = _title_of(node, class_type or "未命名节点")
        nodes.append({"id": str(node_id), "class_type": class_type, "title": title})

        raw_inputs = node.get("inputs")
        if isinstance(raw_inputs, dict):
            for input_name, value in raw_inputs.items():
                if not _is_literal(value):
                    continue  # 连线或缺省值，不是可注入参数
                inputs.append(
                    {
                        "path": f"{node_id}.inputs.{input_name}",
                        "node_id": str(node_id),
                        "node_title": title,
                        "class_type": class_type,
                        "input_name": input_name,
                        "current_value": _preview(value),
                    }
                )

        if is_output_node(node):
            outputs.append({"node_id": str(node_id), "class_type": class_type, "title": title})

    return {"nodes": nodes, "inputs": inputs, "outputs": outputs}
