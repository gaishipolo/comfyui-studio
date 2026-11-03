"""Pydantic Schema — API 序列化"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# 表单字段允许的控件类型（与前端 TemplateField.type 对齐）
# 注意：type 只描述「用什么控件」，不描述「几个值」—— 基数看 multiple，
# 元素类型看能力声明（string/int/bool/enum/image/audio/video）。
# image_upload 是历史名字（当初只有图片能上传），读取时归一成 file_upload。
_FIELD_TYPES = {
    "text", "textarea", "number", "select", "file_upload", "image_upload",
    # 数字控件 + 「随机」按钮（典型用途 seed）。值仍是数字，落库后按数字下发
    "random_number",
}
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
        # 数值范围：只对数值类控件有意义（随机数靠它定取值区间）。
        # 注意 bool 是 int 的子类，必须显式排除，否则 True 会变成 min=1。
        if field_type in ("number", "random_number"):
            for key in ("min", "max"):
                raw_bound = item.get(key)
                if isinstance(raw_bound, (int, float)) and not isinstance(raw_bound, bool):
                    field[key] = raw_bound
        # 基数：任何类型都可以是列表（音频列表、参考图列表、甚至多行文本）。
        # 之前把 multiple 限定在 image_upload 上，导致「列表参数」只能靠
        # audio/audio2/audio3 这种槽位硬凑 —— 那正是这次要拆掉的东西
        if item.get("multiple"):
            field["multiple"] = True
        fields.append(field)

    return {**value, "fields": fields}


# ── 模板 ──────────────────────────────────────────

class TemplateBase(BaseModel):
    name: str = Field(..., max_length=128)
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
    # 来源任务：生成素材指向产出它的任务，用户上传的素材为 None
    task_id: str | None = None
    file_path: str
    file_name: str
    file_size: int
    mime_type: str
    tags: list
    meta: dict
    is_shared: bool = False  # 已共享给团队（共享≠共同所有：别人只能看/下载）
    created_by: str = ""  # 归属人 id（assets.owner_id）
    creator_name: str = ""  # 归属人显示名（展示「来自谁」）
    created_at: datetime
    # 回收站时间：非空表示已软删除（只在回收站列表里出现）
    deleted_at: datetime | None = None

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


# ── 模板导出 / 导入 ──────────────────────────────

class TemplateImportPayload(BaseModel):
    """模板导出文件的内容。

    `workflow` 内嵌时导出文件是自包含的 —— 换台机器导入即可用，
    不用再去翻原实例的工作流目录。
    """
    format: str = ""
    version: int = 0
    template: dict = Field(default_factory=dict)
    workflow: dict | None = None


class TemplateImportResponse(BaseModel):
    """导入结果。warnings 是「导进来了，但你可能想知道」的事 ——
    比如本机没装那个引擎、工作流没跟着文件走、重名被改了。"""
    template: TemplateResponse
    warnings: list[str] = Field(default_factory=list)


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
    # 存储口径：storage_bytes 含回收站（文件还在磁盘上，与配额口径一致），
    # trash_* 是其中的回收站部分 —— 让管理员明白「删了为什么没释放空间」。
    storage_bytes: int = 0
    asset_count: int = 0
    trash_storage_bytes: int = 0
    trash_asset_count: int = 0


class UsageResponse(BaseModel):
    """用量统计响应（仅 admin）—— 时间区间回显 + 每人一行 + 合计"""
    date_from: str = ""
    date_to: str = ""
    by_user: list[UsageByUser] = []
    totals: UsageByUser = UsageByUser()


# ───────────────────────────── 流程编排 ─────────────────────────────
#
# 流程 = 节点（Template）的有序编排。系统的两层配置都是**数据不是代码**，
# 这是将来能接 Agent 的前提 —— Agent 只能写数据。
#
# steps 的形状：
#   { id, type: "template", template_id, params: { 字段: 字面量 或 "{{...}}" } }
# params 的取值是表达式：
#   "{{ input.xxx }}"          流程输入的字段
#   "{{ steps.s1.outputs }}"   上游步骤的产物（列表）
# 字段名必须是目标节点 input_schema 里存在的字段 —— 校验在 validate 接口做。


class FlowStep(BaseModel):
    """流程里的一步。绑定按 id 引用（不引用下标），插入/排序不会打断引用。"""

    id: str = Field(min_length=1, max_length=64, description="步骤别名，流程内唯一")
    # 显示名 —— 给内容同事看的。id 是内部标识（引用靠它），改名会断链，
    # 所以界面让人改 title，id 由系统生成且不可编辑。
    title: str = Field(default="", max_length=64, description="步骤显示名（给人看的）")
    type: str = Field(default="template", description="步骤类型：template（第一刀只支持这个）")
    template_id: str = Field(default="", description="节点（Template）ID")
    flow_id: str = Field(default="", description="子流程 ID（type=flow 时用）")
    count: int = Field(default=1, ge=1, le=999, description="循环次数（type=repeat 时用）")
    params: dict = Field(default_factory=dict, description="参数取值：字面量或 {{...}} 表达式")

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("步骤 id 不能为空")
        return cleaned


class FlowOutput(BaseModel):
    """流程输出声明。显式命名，父流程按 name 引用。"""

    name: str = Field(min_length=1, max_length=64)
    from_step: str = Field(default="", alias="from", description="来源步骤 id")
    pick: str = Field(default="all", description="取哪部分：all/first/last")

    model_config = {"populate_by_name": True}


class FlowBase(BaseModel):
    """流程配置的公共字段。"""

    name: str = Field(min_length=1, max_length=128, description="流程名称")
    description: str = Field(default="", description="流程说明")
    category: str = Field(default="image", max_length=64, description="分类")
    icon: str = Field(default="", max_length=256, description="图标 URL 或 emoji")
    input_schema: dict = Field(default_factory=dict, description="流程输入字段定义")
    outputs: list[FlowOutput] = Field(default_factory=list, description="流程输出声明")
    steps: list[FlowStep] = Field(default_factory=list, description="步骤列表")
    status: str = Field(default="active", description="draft/active/archived")

    @field_validator("input_schema")
    @classmethod
    def _normalize_schema(cls, v: Any) -> dict:
        return normalize_input_schema(v)


class FlowCreate(FlowBase):
    pass


class FlowUpdate(BaseModel):
    """局部更新 —— 没传的字段不动。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    category: str | None = Field(default=None, max_length=64)
    icon: str | None = Field(default=None, max_length=256)
    input_schema: dict | None = None
    outputs: list[FlowOutput] | None = None
    steps: list[FlowStep] | None = None
    status: str | None = None

    @field_validator("input_schema")
    @classmethod
    def _normalize_schema(cls, v: Any) -> dict | None:
        return None if v is None else normalize_input_schema(v)


class FlowResponse(FlowBase):
    id: str
    version: int = 1
    created_by: str = ""
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FlowRunResponse(BaseModel):
    """一次流程运行。"""

    id: str
    flow_id: str
    flow_name: str = ""
    status: str = "running"
    progress: float = 0.0
    params: dict = Field(default_factory=dict)
    steps: list = Field(default_factory=list)
    error_msg: str = ""
    created_at: datetime
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class FlowValidateRequest(BaseModel):
    """校验一份流程配置（不落库）。编排器保存前、Agent 生成后都走这里。"""

    steps: list[FlowStep] = Field(default_factory=list)
    input_schema: dict = Field(default_factory=dict)
    outputs: list[FlowOutput] = Field(default_factory=list)


class FlowValidateResponse(BaseModel):
    ok: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    layers: list[list[str]] = Field(default_factory=list, description="依赖分层，同层可并行")


class NodeCapability(BaseModel):
    """能力清单里的一项 —— 一个可用节点。"""

    id: str
    name: str
    description: str = ""
    category: str = "image"
    engine: str = ""
    capability: str = ""
    fields: list[dict] = Field(default_factory=list)
