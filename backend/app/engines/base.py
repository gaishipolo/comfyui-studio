"""生成引擎抽象接口

Provider 插件标准（多引擎）：一家供应商一个 GenerationProvider 子类，
「我会什么」声明在 capabilities（建模板时界面读它生成表单），
「怎么调」写在 build_payload / submit / get_status 里（纯代码，任意转换都能表达）。
开发规范见 docs/provider-dev-guide.md，示例见 examples/provider_template.py。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class FieldSpec:
    """字段声明 —— 能力入参和引擎配置项（config_schema）共用同一结构

    name 就是 API 参数名（表单字段名默认同名，值域保持一致）；
    options 是 enum 选项，值直接用 API 原生值（如 size: "512*512"）——
    值形态差异在选项里消化，不需要任何转换层。
    """
    name: str
    label: str
    # 元素类型（不是控件）：string / int / float / bool / enum /
    # image / audio / video（后三个是文件类，走上传控件）
    type: str = "string"
    required: bool = False
    default: Any = None
    options: list[str] = field(default_factory=list)
    description: str = ""
    # 基数：true = 这个参数是列表，表单值是数组（顺序即提交顺序）。
    # **与 type 正交**：任何类型都能是列表（audios 列表、参考图列表、多值枚举…），
    # 不要再用 audio/audio2/audio3 这种槽位去硬凑列表 —— 那是把平台的表达力缺口
    # 转嫁给用户。文件类的列表 = 一个框可多选；标量类的列表 = 可增删的多行输入
    multiple: bool = False


@dataclass
class CapabilitySpec:
    """能力声明 —— Provider 对外暴露「我会什么、每个能力收哪些参数」"""
    kind: str            # text_to_image / image_to_image / text_to_video ...
    label: str
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass
class GenerationResult:
    """生成结果"""
    success: bool = False
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    outputs: list[dict] = field(default_factory=list)  # [{"filename": ..., "subfolder": ..., "type": ...}]
    error: str = ""
    raw: dict = field(default_factory=dict)


class GenerationProvider(ABC):
    """生成引擎统一接口（Provider 插件基类）"""

    # ── 类声明：插件管理页与模板配置读这些 ─────────────────
    name: str = ""                                  # 注册名（唯一，如 "wanx"）
    label: str = ""                                 # 展示名（如 "通义万相"）
    capabilities: dict[str, CapabilitySpec] = {}    # 能力表：kind -> 声明
    config_schema: list[FieldSpec] = []             # 需要的配置项（base_url/api_key/...）

    def __init__(self, config: dict | None = None):
        # 运行配置由插件管理 API 存 DB、factory 注入 —— 机密不进上传的代码文件
        self.config = config or {}
        # 最近一次提交的实际请求体（审计快照，task_service 提交后取走）。
        # 子类在 submit 里自愿填写；太大（如整个 ComfyUI 工作流）就不填
        self.last_request: Any = None

    def build_payload(self, capability: str, params: dict) -> dict:
        """表单值 → API 请求体。默认透传 params（字段名=API 参数名即够用）。
        需要改名/拼接/嵌套时子类重写，几行 Python 的事。"""
        return dict(params)

    @abstractmethod
    async def submit(self, ctx: dict, params: dict) -> str:
        """提交生成任务，返回引擎侧任务 ID。

        ctx 由 task_service 统一组装：
          _type          场景分类（模板 category）
          _capability    能力 kind（模板 capability）
          _template_name 模板名（报错信息用）
          _workflow      工作流 JSON（ComfyUI 用，其他引擎忽略）
          _param_mapping 表单字段→节点路径映射（ComfyUI 用）
        """
        ...

    @abstractmethod
    async def get_status(self, task_id: str) -> GenerationResult:
        """查询任务状态和结果"""
        ...

    @abstractmethod
    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        """下载生成结果文件"""
        ...

    async def upload_input(self, data: bytes, filename: str) -> str:
        """上传输入文件（可选能力，默认不支持）"""
        raise NotImplementedError(f"{type(self).__name__} 不支持上传输入文件")

    async def cancel(self, provider_task_id: str) -> bool:
        """取消引擎侧任务（可选能力，默认不支持）。

        要同时覆盖「还没开跑（在引擎队列里排队）」和「正在跑」两种情况。
        返回 True 表示引擎侧确认已取消，False 表示不支持或没取消掉。
        注意区分：任务早已跑完时返回 False 是正常的，不代表出错。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持取消任务")

    @abstractmethod
    async def health_check(self) -> bool:
        """引擎健康检查"""
        ...
