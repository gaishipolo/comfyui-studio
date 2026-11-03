"""存储配额 —— 只统计素材文件大小。

两个刻意的口径决定：

1. **回收站里的素材计入配额**。软删除只打时间戳，磁盘文件还在。如果删除即
   释放配额，用户会以为「删了就腾出空间」，实际磁盘一点没变；管理员看用量
   统计时也会对不上。释放配额只有两条路：彻底删除，或超过保留期被自动清理。

2. **管理员豁免**。管理员是管系统的人，被自己的配额锁住会导致连调整配额
   都做不了（先要删素材才能进管理页）。豁免只针对准入检查，用量统计照常
   统计管理员的占用。

检查点只有两个（都在写操作入口）：
- 上传素材：能预知大小，精确判断
- 提交生成任务：产物大小未知，只能判断「当前是否已超限」

不做「落盘时超限就丢产物」——用户白等几分钟换一个失败，比多占几百 MB 更糟。
配额是准入控制，不是硬限流。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import is_admin
from app.config import settings
from app.models.models import Asset, User


def default_quota_bytes() -> int:
    """新用户的默认配额（字节）。0 = 不限。

    只给**新建**用户用。存量用户升级后不动 —— 突然把人的上传掐了比磁盘
    慢慢涨更难受。管理员本来就豁免，设了也不影响。
    """
    gb = float(settings.DEFAULT_STORAGE_QUOTA_GB or 0)
    if gb <= 0:
        return 0
    return int(gb * 1024**3)


def human_bytes(size: int) -> str:
    """人类可读大小。只用于错误提示，精度到 0.1 位即可。"""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


async def used_bytes(db: AsyncSession, owner_id: str) -> int:
    """某人已占用的字节数。**不过滤 deleted_at** —— 回收站里的文件仍在磁盘上。"""
    if not owner_id:
        return 0
    total = await db.scalar(
        select(func.coalesce(func.sum(Asset.file_size), 0)).where(Asset.owner_id == owner_id)
    )
    return int(total or 0)


async def ensure_quota(db: AsyncSession, user: User, incoming: int = 0) -> None:
    """准入检查：配额够就静默返回，不够抛 403。

    `incoming` 是本次要新增的字节数。上传素材时传真实文件大小；提交生成任务时
    传 0 —— 引擎还没跑，产物多大不知道。
    """
    if is_admin(user):
        return

    quota = int(user.storage_quota_bytes or 0)
    if quota <= 0:
        return  # 0 = 不限

    used = await used_bytes(db, user.id)
    need = max(0, int(incoming))
    if used + need <= quota:
        return

    detail = (
        f"存储配额不足：已用 {human_bytes(used)} / 上限 {human_bytes(quota)}"
    )
    if need:
        detail += f"，本次需要 {human_bytes(need)}"
    detail += "。请先在素材库清理文件（彻底删除才释放配额），或联系管理员调整上限。"
    raise HTTPException(status_code=403, detail=detail)


async def usage_summary(db: AsyncSession, user: User) -> dict:
    """给前端展示的配额概览。管理员不设上限时 quota=0。"""
    used = await used_bytes(db, user.id)
    quota = 0 if is_admin(user) else int(user.storage_quota_bytes or 0)
    return {
        "used_bytes": used,
        "quota_bytes": quota,
        "unlimited": quota <= 0,
        "percent": (round(used / quota * 100, 1) if quota > 0 else None),
    }
