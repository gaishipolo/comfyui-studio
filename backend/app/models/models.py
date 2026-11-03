"""SQLAlchemy 数据模型"""
import json
import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Float, DateTime, ForeignKey, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base
from app.timeutils import utcnow


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
    # 改密/重置密码时 +1，让旧 JWT 立即失效（JWT 无状态，靠版本号对账）
    token_version: Mapped[int] = mapped_column(Integer, default=0, comment="令牌版本，改密递增以吊销旧 token")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    template: Mapped["Template"] = relationship(back_populates="tasks")
    assets: Mapped[list["Asset"]] = relationship(back_populates="task")


class Asset(Base):
    """素材 —— 既可以是生成产物，也可以是用户直接上传的文件。

    身份由 `id` 决定。早期版本用 (task_id, file_name) 二元组定位素材，
    导致「素材必须属于某个任务」成了硬约束，用户上传的文件无处安放。
    现在 `task_id` 降级为**来源标记**：指向产出它的生成任务，上传素材为 NULL。

    归属走 `owner_id`（权威字段），不再借道 task.created_by —— 否则归属判定
    要写成「有任务查任务、没任务查自己」两套逻辑。
    """
    __tablename__ = "assets"
    __table_args__ = (
        # 列表的主查询是「我的素材按时间倒序」，复合索引直接覆盖。
        Index("ix_assets_owner_created", "owner_id", "created_at"),
        # 文件名唯一性从 DB 约束降级为应用层保证（unique_asset_name）：
        # task_id 可空后，SQLite 唯一索引里 NULL 互不相等，旧约束形同虚设。
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    owner_id: Mapped[str] = mapped_column(
        String(32), default="", comment="归属人 ID（权限判定只看这一列）"
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id"), nullable=True,
        comment="来源任务；用户上传的素材为空",
    )

    file_path: Mapped[str] = mapped_column(String(512), comment="存储路径")
    file_name: Mapped[str] = mapped_column(String(256), comment="原始文件名")
    file_size: Mapped[int] = mapped_column(Integer, default=0, comment="文件大小（字节）")
    mime_type: Mapped[str] = mapped_column(String(64), default="image/png", comment="MIME 类型")

    tags: Mapped[list] = mapped_column(JSONStore, default=list, comment="标签列表")
    meta: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="元数据（生成参数快照）")
    # 用户隔离（2026-10-29）：默认仅作者（走 task.created_by）与管理员可见；
    # is_shared=True 后全团队可见可下载，但**不可改**（打标签/删除/共享开关仍是作者或管理员）
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否共享给团队")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped["Task | None"] = relationship(back_populates="assets")


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
