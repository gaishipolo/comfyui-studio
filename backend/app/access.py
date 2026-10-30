"""数据可见性 / 可改性判定 —— 用户隔离的唯一规则出处（2026-10-29）

规则（产品拍板：默认隔离 + 手动共享）：
- 任务：创建者本人或管理员可见、可操作（取消/重试/删除）。
  created_by 为空视为无主，仅管理员可见。
- 素材：素材本身没有归属字段，归属走 task.created_by。
  默认仅本人/管理员可见；is_shared=True 后全团队**可见可下载**，
  但**不可改** —— 打标签、删除、共享开关仍是作者本人或管理员的权利
  （共享的是「看」的权限，不是共同所有）。

用法约定：
- 读接口（列表/详情/下载）不可见时返回 **404**（不泄露「存在但无权」）
- 写接口（标签/删除/分享/取消/重试）越权时返回 **403**（保留现有文案风格）
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Task, User


def is_admin(user: User) -> bool:
    return user.role == "admin"


def can_view_task(user: User, owner_id: str) -> bool:
    """任务可见性：本人或管理员；无主任务（owner_id 空）仅管理员"""
    return is_admin(user) or (bool(owner_id) and owner_id == user.id)


def can_modify_task(user: User, owner_id: str) -> bool:
    """任务可操作性（取消/重试/删除）：与可见性同界"""
    return can_view_task(user, owner_id)


def can_view_asset(user: User, owner_id: str, is_shared: bool) -> bool:
    """素材可见性：本人、管理员，或已共享给团队的"""
    return is_admin(user) or bool(is_shared) or (bool(owner_id) and owner_id == user.id)


def can_modify_asset(user: User, owner_id: str) -> bool:
    """素材可改性（打标签/删除/共享开关）：仅本人或管理员。
    注意**不看 is_shared** —— 共享出去的素材别人只能看，不能动。"""
    return is_admin(user) or (bool(owner_id) and owner_id == user.id)


# ── 归属查询助手（显式 select，禁用懒加载 —— async 会话里抛 MissingGreenlet）──


async def task_owner_map(db: AsyncSession, task_ids: set[str]) -> dict[str, str]:
    """task_id → created_by。素材的归属要过任务表，一次批量查完。"""
    ids = {t for t in task_ids if t}
    if not ids:
        return {}
    rows = await db.execute(select(Task.id, Task.created_by).where(Task.id.in_(ids)))
    return {r[0]: r[1] for r in rows}


async def creator_display_names(db: AsyncSession, user_ids: set[str]) -> dict[str, str]:
    """user_id → 显示名（显示名为空回退用户名）。查不到的不进结果。"""
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    rows = await db.execute(
        select(User.id, User.display_name, User.username).where(User.id.in_(ids))
    )
    return {r[0]: (r[1] or r[2] or "") for r in rows}
