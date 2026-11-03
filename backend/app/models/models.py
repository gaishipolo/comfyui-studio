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
    # 存储配额（字节）。0 = 不限。只统计素材文件大小，回收站里的也计入
    # —— 软删除不释放磁盘，配额跟着磁盘走才诚实。
    storage_quota_bytes: Mapped[int] = mapped_column(
        Integer, default=0, comment="存储配额（字节），0 表示不限"
    )
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
    # 流程编排：一个 Task 可能属于某次流程运行的某一步。非流程任务为空。
    flow_run_id: Mapped[str] = mapped_column(
        String(32), default="", index=True, comment="所属流程运行 ID；非流程任务为空"
    )
    step_id: Mapped[str] = mapped_column(
        String(32), default="", comment="流程内的步骤别名（Flow.steps[].id）"
    )
    # 试跑（2026-11-04）：Agent 生成的流程上线前先试跑。
    # 试跑任务不进正式任务列表（素材也不进正式素材库）——
    # 它们是「看看效果」的产物，不是正式产出。
    is_trial: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, comment="试跑任务；正式任务为 0"
    )

    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True,
        # cancelled 是独立终态（不是 error）：用户主动取消和引擎失败要分得开
        comment="状态：pending/running/done/error/cancelled"
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

    # 回收站（2026-11-03）：软删除。非空 = 已进回收站，列表/统计/下载默认过滤掉，
    # 但**磁盘文件仍在**，所以配额照算。只有「彻底删除」才真删文件。
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True, comment="进回收站的时间；NULL 表示正常"
    )

    # 试跑产物（2026-11-04）：Agent 生成的流程要先试跑再上线。
    # 试跑真的调引擎、真的出图，但**不进正式素材库** —— 那是给人看效果的，
    # 不是正式产出。默认从素材列表排除（有开关能翻出来）。
    is_trial: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, comment="试跑产物；正式素材为 0"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped["Task | None"] = relationship(back_populates="assets")


class Flow(Base):
    """流程配置 —— 节点（Template）的有序编排。

    系统只有两层配置，两层都是**数据不是代码**：
      - 节点配置 = Template：引擎 + 能力 + 一张表单
      - 流程配置 = Flow（本类）：节点怎么串、每步吃什么、吐出什么

    「两层都是数据」是这套设计能接 Agent 的根本前提：Agent 只能写数据，
    写不了代码。写出来的配置可以被校验、被试跑、被审批。

    步骤存在 steps（JSON）里，每步形如：
        { id, type: template|flow|repeat, template_id|flow_id, count,
          params: { 字段: 字面量 或 "{{...}}" } }
    绑定按**步骤 id** 引用，不引用数组下标 —— 插入/删除/排序不会打断引用。
    """
    __tablename__ = "flows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, comment="流程名称")
    description: Mapped[str] = mapped_column(Text, default="", comment="流程说明")
    category: Mapped[str] = mapped_column(String(64), default="image", comment="分类")
    icon: Mapped[str] = mapped_column(String(256), default="", comment="图标 URL 或 emoji")

    # 流程级输入：运行台让用户填的那些字段。它**独立于首节点** ——
    # 首节点只要「初始画面」，流程可能还要「镜头数」「每镜头的提示词列表」。
    input_schema: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="流程输入字段定义")
    # 流程级输出：显式声明 [{name, from, pick}]，父流程按 name 引用。
    # 不从尾节点自动推导 —— 尾节点可能产出多张图，流程只该暴露其中一部分。
    outputs: Mapped[list] = mapped_column(JSONStore, default=list, comment="输出声明 [{name, from, pick}]")

    steps: Mapped[list] = mapped_column(JSONStore, default=list, comment="步骤列表")

    # draft = 草稿（Agent 生成的流程落这里，人审批后才 active）
    status: Mapped[str] = mapped_column(String(16), default="active", index=True, comment="draft/active/archived")
    version: Mapped[int] = mapped_column(Integer, default=1, comment="版本号，每次修改 +1")
    created_by: Mapped[str] = mapped_column(String(32), default="", index=True, comment="创建人 user id")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # 审批留痕（2026-11-04）：Agent 生成的流程要过人眼。
    # **谁批的、什么时候批的、说了什么**都得留下 —— 出问题时才知道该找谁。
    # draft 时 reviewed_by 为空；审批后填上。
    reviewed_by: Mapped[str] = mapped_column(
        String(32), default="", comment="审批人 user id；未审批为空"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="审批时间"
    )
    review_note: Mapped[str] = mapped_column(
        Text, default="", comment="审批意见 / 驳回原因"
    )
    # Agent 生成流程时写下的说明：这版是怎么来的、Agent 说了什么。
    # 审批人第一眼看的就是这个。
    agent_note: Mapped[str] = mapped_column(
        Text, default="", comment="Agent 对这份流程的说明"
    )

    # 删除流程时**保留运行记录**（它们是历史，不是流程的一部分）。
    # 不加 passive_deletes 的话，SQLAlchemy 的默认 cascade 会试图把
    # flow_runs.flow_id 置空 —— 而那列是 NOT NULL，删流程直接 500。
    # 这个坑从流程骨架起就埋着，之前没人删过「有运行记录的流程」所以没暴露。
    runs: Mapped[list["FlowRun"]] = relationship(
        back_populates="flow", passive_deletes=True
    )


class FlowRun(Base):
    """流程运行实例 —— 一次「填参数 + 点运行」。

    每一步对应一个普通 Task（tasks 表里 flow_run_id / step_id 指回来），
    所以并发闸门、进度轮询、失败重试、任务列表全部复用现有实现。
    """
    __tablename__ = "flow_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    flow_id: Mapped[str] = mapped_column(ForeignKey("flows.id"), index=True, comment="流程 ID")
    flow_name: Mapped[str] = mapped_column(String(128), default="", comment="流程名快照（流程改名后仍可读）")
    created_by: Mapped[str] = mapped_column(String(32), default="", index=True, comment="提交人 user id")

    status: Mapped[str] = mapped_column(String(16), default="running", index=True, comment="running/done/error/cancelled")
    progress: Mapped[float] = mapped_column(Float, default=0.0, comment="整体进度 0~100")

    params: Mapped[dict] = mapped_column(JSONStore, default=dict, comment="流程输入快照")
    # 每步状态 [{step_id, task_id, status, error}]，运行台据此显示进度
    steps: Mapped[list] = mapped_column(JSONStore, default=list, comment="每步执行状态")
    # 流程级产出 [{name, pick, outputs}]，按流程的 outputs 声明解析。
    # 父流程引用子流程时，子流程的产出名就是这里的 name。
    outputs: Mapped[list] = mapped_column(JSONStore, default=list, comment="流程级产出")

    error_msg: Mapped[str] = mapped_column(Text, default="", comment="失败原因")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 试跑标记（2026-11-04）：这次运行是「试跑」——
    # Agent 生成的流程上线前先跑一遍看看效果。
    # 试跑产物打 is_trial 标记，不进正式素材库；正式运行不受影响。
    is_trial: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, comment="试跑运行；正式运行为 0"
    )

    flow: Mapped["Flow"] = relationship(back_populates="runs")


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
