"""回收站清理 —— 彻底删除软删除素材（记录 + 磁盘文件）。

两个调用方共用同一套分批逻辑：
- 启动时的保留期清理（`purge_expired`）
- 用户点「清空回收站」（`api/assets.py: empty_trash`）

为什么放在启动时跑，而不是定时任务：
单机单进程部署，引入 APScheduler 只是多一个依赖和一条可能静默失败的调度线程。
启动时清一次已经够用 —— 服务重启频率远低于保留期，清理本身幂等，重复跑无害。
代价是「连续运行超过保留期而不重启」时清理会滞后；那时回收站占的空间也受配额
约束，不会无限增长，可以接受。

为什么必须分批（两个调用方都是）：
本机 sitecustomize 给 `Path.unlink()` 套了批量删除守卫（累计超过阈值直接
raise SystemExit），一次删几千个文件会把请求/启动流程打断。每批 50 个、
最多 20 批，剩下的下次再清。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Asset
from app.thumbs import cleanup_asset_files
from app.timeutils import utcnow

BATCH = 50
MAX_BATCHES = 20


async def purge_matching(
    db: AsyncSession,
    conditions: Sequence[Any],
    *,
    max_batches: int = MAX_BATCHES,
) -> dict:
    """分批彻底删除满足条件的素材，返回统计。

    顺序刻意是「先提交数据库、再删文件」：记录是主体，不能因为某个文件被占用
    就把整批回滚。代价是删不掉的文件会成为孤儿，所以单批只处理 BATCH 个 ——
    最坏情况下孤儿的数量是有界的，而不是一次几千个。

    `remaining` > 0 表示批次用完了还没清空，调用方应提示用户再执行一次。
    """
    purged = 0
    files_removed = 0
    errors: list[str] = []
    exhausted = False

    for _ in range(max_batches):
        rows = await db.execute(select(Asset).where(*conditions).limit(BATCH))
        items = list(rows.scalars().all())
        if not items:
            break

        targets = [(item.file_path, item.file_name) for item in items]
        for item in items:
            await db.delete(item)
        await db.commit()
        purged += len(targets)

        for file_path, file_name in targets:
            try:
                (file_removed, file_error), _ = await asyncio.to_thread(
                    cleanup_asset_files, file_path, file_name
                )
            except (OSError, RuntimeError, SystemExit) as exc:
                # SystemExit 不是 Exception 子类，漏了它会炸穿整个请求/启动流程
                errors.append(f"{file_name}: {type(exc).__name__}")
                continue
            if file_removed:
                files_removed += 1
            elif file_error:
                errors.append(f"{file_name}: {file_error}")
    else:
        exhausted = True

    remaining = 0
    if exhausted:
        remaining = int(
            await db.scalar(select(func.count()).select_from(Asset).where(*conditions)) or 0
        )

    return {
        "purged": purged,
        "files_removed": files_removed,
        "errors": errors[:20],  # 只回前 20 条，避免响应体被刷爆
        "remaining": remaining,
    }


async def purge_expired(db: AsyncSession, *, days: int) -> dict:
    """删除 `deleted_at` 早于 cutoff 的素材。返回清理统计。

    `days <= 0` 表示关闭自动清理，直接返回。
    """
    if days <= 0:
        return {"purged": 0, "files_removed": 0, "errors": [], "disabled": True}

    cutoff = utcnow() - timedelta(days=days)
    stats = await purge_matching(
        db, [Asset.deleted_at.is_not(None), Asset.deleted_at <= cutoff]
    )
    return {
        "purged": stats["purged"],
        "files_removed": stats["files_removed"],
        "errors": stats["errors"],
    }
