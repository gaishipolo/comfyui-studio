"""Pydantic Schema — API 序列化"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# 表单字段允许的控件类型（与前端 TemplateField.type 对齐）
# 注意：type 只描述「用什么控件」，不描述「几个值」—— 基数看 multiple，
# 元素类型看能力声明（string/int/bool/enum/image/audio/video）。
# image_upload 是历史名字（当初只有图片能上传），读取时归一成 file_upload。
_FIELD_TYPES = {"text", "textarea", "number", "select", "file_upload", "image_upload"}
# 上传控件放行的文件种类
_FILE_KINDS = {"image", "audio", "video"}


def normalize_input_schema(value: Any) -> dict:
    """把 input_schema 归一化为前端 TemplateField 契约要求的完整结构。

    input_schema 是自由 JSON，管理员可能手改或直接调 API，字段容易缺键——
    而前端 TemplateField 声明 name/label/type/required 为必填，缺键会导致
    表单渲染出 `required=undefined` 之类的隐性漂移。这里统一补齐默认值，
    没有 name 的字段直接丢弃。

    注意这是**白名单**实现：只保留下面这些键，未知键会被清除。
    这是有意为之（保证输出永远满足 TS 契约），代价是——
    **给 TemplateField 加新属性时，必须同步改这里的 field 组装逻辑**，
    否则新属性会在保存时被静默丢弃。字段清单见 frontend/src/types/index.ts。

    幂等：normalize(normalize(x)) == normalize(x)，重复保存不会累积丢数据。
    """
    if not isinstance(value, dict):
        return {}
    raw_fields = value.get("fields")
    if not isinstance(raw_fields, list):
        return {**value, "fields": []}

    fields = []
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue          # 无名字段无法映射到工作流，丢弃
        name = name.strip()
        field_type = item.get("type")
        if field_type not in _FIELD_TYPES:
            field_type = "text"

        # 上传控件：统一成 file_upload + accept（image/audio/video）。
        # 旧模板里的 image_upload 在这里升级，前端只需认一种类型
        accept = ""
        if field_type in ("image_upload", "file_upload"):
            accept = str(item.get("accept") or "").strip().lower()
            if accept not in _FILE_KINDS:
                accept = "image"
            field_type = "file_upload"

        field = {
            "name": name,
            "label": item.get("label") or name,
            "type": field_type,
            "required": bool(item.get("required", False)),
        }
        if accept:
            field["accept"] = accept
        # 可选键：仅在确有值时保留，避免塞一堆 null
        if item.get("placeholder"):
            field["placeholder"] = str(item["placeholder"])
        if item.get("options"):
            field["options"] = list(item["options"])
        if item.get("default") is not None:
            field["default"] = item["default"]
        if item.get("advanced"):
            field["advanced"] = True
        # 基数：任何类型都可以是列表（音频列表、参考图列表、甚至多行文本）。
        # 之前把 multiple 限定在 image_upload 上，导致「列表参数」只能靠
        # audio/audio2/audio3 这种槽位硬凑 —— 那正是这次要拆掉的东西
        if item.get("multiple"):
            field["multiple"] = True
        fields.append(field)

    return {**value, "fields": fields}


# ── 模板 ──────────────────────────────────────────

class TemplateBase(BaseModel):
    name: str
    description: str = ""
    category: str = "image"
    icon: str = ""
    input_schema: dict = Field(default_factory=dict)
    engine: str = "comfyui"
    capability: str = ""
    workflow_file: str = ""
    param_mapping: dict = Field(default_factory=dict)
    output_config: dict = Field(default_factory=dict)
    is_active: bool = True
    sort_order: int = 0

    @field_validator("input_schema")
    @classmethod
    def _normalize_schema(cls, v: Any) -> dict:
        return normalize_input_schema(v)


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    icon: str | None = None
    input_schema: dict | None = None
    engine: str | None = None
    capability: str | None = None
    workflow_file: str | None = None
    param_mapping: dict | None = None
    output_config: dict | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    @field_validator("input_schema")
    @classmethod
    def _normalize_schema(cls, v: Any) -> dict | None:
        return None if v is None else normalize_input_schema(v)


class TemplateResponse(TemplateBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── 任务 ──────────────────────────────────────────

class TaskCreate(BaseModel):
    template_id: str
    params: dict = Field(default_factory=dict)
    batch_count: int = Field(default=1, ge=1, le=32, comment="批量数量")


class TaskResponse(BaseModel):
    id: str
    template_id: str
    created_by: str = ""
    creator_name: str = ""  # 创建人显示名（admin 看全部任务时用）
    batch_id: str
    status: str
    progress: float
    params: dict
    provider_task_id: str
    result: dict
    error_msg: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    class Config:
        from_attributes = True


# ── 素材 ──────────────────────────────────────────

class AssetResponse(BaseModel):
    id: str
    task_id: str
    file_path: str
    file_name: str
    file_size: int
    mime_type: str
    tags: list
    meta: dict
    is_shared: bool = False  # 已共享给团队（共享≠共同所有：别人只能看/下载）
    created_by: str = ""  # 归属人（走 task.created_by）
    creator_name: str = ""  # 归属人显示名（展示「来自谁」）
    created_at: datetime

    class Config:
        from_attributes = True


class AssetShareRequest(BaseModel):
    """共享开关：true = 共享给团队，false = 收回"""
    shared: bool


class AssetUploadRequest(BaseModel):
    """从素材库选用素材 → 推进生成引擎的请求体"""
    asset_id: str
    engine: str = "comfyui"


# ── 工作流上传 ────────────────────────────────────

class WorkflowNode(BaseModel):
    id: str
    class_type: str
    title: str


class WorkflowInput(BaseModel):
    """一个可注入的工作流输入，path 形如 "76.inputs.value" """
    path: str
    node_id: str
    node_title: str
    class_type: str
    input_name: str
    current_value: str


class WorkflowOutput(BaseModel):
    node_id: str
    class_type: str
    title: str


class WorkflowInventory(BaseModel):
    """工作流里界面需要的三份清单：节点表 / 可注入输入 / 出图节点"""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    inputs: list[WorkflowInput] = Field(default_factory=list)
    outputs: list[WorkflowOutput] = Field(default_factory=list)


class WorkflowUploadResponse(WorkflowInventory):
    filename: str
    node_count: int
    message: str


class WorkflowInventoryResponse(WorkflowInventory):
    filename: str


class UsageByUser(BaseModel):
    """单个用户的用量统计（一个时间段内）"""
    user_id: str = ""
    username: str = ""
    display_name: str = ""
    role: str = ""
    task_total: int = 0
    task_done: int = 0
    task_error: int = 0
    task_cancelled: int = 0
    avg_duration_seconds: float | None = None  # 仅算已完成任务；没有完成任务则 null
    storage_bytes: int = 0
    asset_count: int = 0


class UsageResponse(BaseModel):
    """用量统计响应（仅 admin）—— 时间区间回显 + 每人一行 + 合计"""
    date_from: str = ""
    date_to: str = ""
    by_user: list[UsageByUser] = []
    totals: UsageByUser = UsageByUser()
