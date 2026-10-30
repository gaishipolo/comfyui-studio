"""SQLAlchemy 数据模型"""
import json
import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


def gen_uuid() -> str:
    return uuid.uuid4().hex


class JSONStore(TypeDecorator):
    """JSON 列，非 ASCII 字符按原样存库，便于 SQL LIKE 搜索。

    背景：默认 json.dumps(ensure_ascii=True) 会把中文转成 \\uXXXX，
    导致 `CAST(meta AS VARCHAR) LIKE '%酷女孩%'` 永远搜不到中文内容。
    这里改用 ensure_ascii=False，库里存真实字符，LIKE 检索即可命中。

    注意 impl 必须是 Text 而非 JSON —— impl=JSON 会再序列化一次造成双重编码。
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)

    # 不覆写 python_type：JSON 值可能是 dict 也可能是 list，无法给出单一类型。
    # 返回元组会违反 SQLAlchemy 契约（要求单个 type），issubclass/部分库集成会炸。


class User(Base):
    """用户"""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, comment="用户名")
    password_hash: Mapped[str] = mapped_column(String(128), comment="密码哈希")
    display_name: Mapped[str] = mapped_column(String(64), default="", comment="显示名")
    role: Mapped[str] = mapped_column(String(16), default="member", comment="角色：admin/member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Template(Base):
    """AI 场景模板"""
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, comment="场景名称")
    description: Mapped[str] = mapped_column(Text, default="", comment="场景描述")
    category: Mapped[str] = mapped_column(String(64), default="image", comment="分类：image/text/video")
    icon: Mapped[str] = mapped_column(String(256), default="", comment="图标 URL 或 emoji")

    # 输入表单 Schema（JSON）
    input_schema: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="表单字段定义")

    # 引擎 + 工作流映射
    engine: Mapped[str] = mapped_column(String(32), default="comfyui", comment="生成引擎")
    capability: Mapped[str] = mapped_column(String(64), default="", comment="能力 kind（text_to_image 等，取自引擎 capabilities 声明）")
    workflow_file: Mapped[str] = mapped_column(String(256), default="", comment="工作流 JSON 文件名")
    param_mapping: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="参数映射：表单字段 -> ComfyUI 节点路径")

    # 输出定义
    output_config: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="输出节点配置")

    # 状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="排序")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tasks: Mapped[list["Task"]] = relationship(back_populates="template")


class Task(Base):
    """生成任务"""
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    template_id: Mapped[str] = mapped_column(ForeignKey("templates.id"), comment="关联模板")
    created_by: Mapped[str] = mapped_column(String(32), default="", index=True, comment="提交用户 ID")
    batch_id: Mapped[str] = mapped_column(String(32), default="", index=True, comment="批量提交组 ID")

    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True,
        comment="状态：pending/running/done/error"
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0, comment="进度 0~100")

    # 用户提交的参数
    params: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="用户输入参数")

    # ComfyUI 侧
    provider_task_id: Mapped[str] = mapped_column(String(64), default="", comment="引擎侧任务 ID（ComfyUI prompt_id）")

    # 结果
    result: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="结果信息")
    error_msg: Mapped[str] = mapped_column(Text, default="", comment="错误信息")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    template: Mapped["Template"] = relationship(back_populates="tasks")
    assets: Mapped[list["Asset"]] = relationship(back_populates="task")


class Asset(Base):
    """生成素材"""
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), comment="关联任务")

    file_path: Mapped[str] = mapped_column(String(512), comment="存储路径")
    file_name: Mapped[str] = mapped_column(String(256), comment="原始文件名")
    file_size: Mapped[int] = mapped_column(Integer, default=0, comment="文件大小（字节）")
    mime_type: Mapped[str] = mapped_column(String(64), default="image/png", comment="MIME 类型")

    tags: Mapped[list] = mapped_column(JSONStore, default=list, comment="标签列表")
    meta: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="元数据（生成参数快照）")
    # 用户隔离（2026-10-29）：默认仅作者（走 task.created_by）与管理员可见；
    # is_shared=True 后全团队可见可下载，但**不可改**（打标签/删除/共享开关仍是作者或管理员）
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否共享给团队")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped["Task"] = relationship(back_populates="assets")


class EnginePlugin(Base):
    """引擎插件元数据（多引擎）—— 代码文件在 backend/plugins/，这里记账

    上传 = 服务端执行任意 Python，只给 admin 且留审计（uploaded_by/sha256）。
    config 是运行配置（base_url/api_key 等），存 DB 不进代码文件，换 key 不用重传。
    """
    __tablename__ = "engine_plugins"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True, comment="引擎注册名（Provider.name）")
    label: Mapped[str] = mapped_column(String(128), default="", comment="展示名")
    filename: Mapped[str] = mapped_column(String(256), comment="代码文件名（plugins/ 下）")
    sha256: Mapped[str] = mapped_column(String(64), default="", comment="代码哈希（审计）")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="启用状态")
    last_error: Mapped[str] = mapped_column(Text, default="", comment="最近加载错误（成功则清空）")
    config: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="运行配置（注入 Provider.config）")
    uploaded_by: Mapped[str] = mapped_column(String(32), default="", comment="上传人 user id（审计；空=手工放置）")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
