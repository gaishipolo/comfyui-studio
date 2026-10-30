"""用量统计 API — 任务数/存储占用/生成耗时（仅 admin，只统计不拦截）

时间区间语义与素材筛选一致（纯日期含当天全天），复用 app.timeutils.parse_day。
数据量是内部工具规模，SQL 查出来 Python 聚合，不搞数据库方言函数。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.models import Asset, Task, User
from app.schemas.schemas import UsageByUser, UsageResponse
from app.auth import get_current_user
from app.timeutils import parse_day
from app.access import is_admin

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    date_from: str | None = Query(None, description="起始日期（含当天）"),
    date_to: str | None = Query(None, description="截止日期（纯日期含当天全天）"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按时间段统计每人用量。仅管理员可看（普通用户看不到别人的量，索性整个端点收口）。

    口径：
    - 任务数按 status 分列（成功/失败/取消/总），归属走 task.created_by
    - 平均耗时只算 done 任务的 finished_at - started_at；没有完成任务返回 null
    - 存储/素材数走 assets join tasks，归属同样走 task.created_by
    """
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可查看用量统计")

    # 时间区间（不传 = 不限）
    task_conds = []
    asset_conds = []
    if date_from:
        start = parse_day(date_from)
        task_conds.append(Task.created_at >= start)
        asset_conds.append(Asset.created_at >= start)
    if date_to:
        end = parse_day(date_to, end=True)
        task_conds.append(Task.created_at <= end)
        asset_conds.append(Asset.created_at <= end)

    # 任务：查全量行（含 started_at/finished_at 算耗时），Python 聚合
    t_rows = (
        await db.execute(
            select(Task.created_by, Task.status, Task.started_at, Task.finished_at).where(
                *task_conds
            )
        )
    ).all()

    # 素材：join 任务表拿归属（素材本身没有归属字段）
    a_rows = (
        await db.execute(
            select(Task.created_by, Asset.file_size)
            .join(Task, Asset.task_id == Task.id)
            .where(*asset_conds)
        )
    ).all()

    # ── 聚合 ──────────────────────────────────────────────
    per_user: dict[str, dict] = {}

    def bucket(uid: str) -> dict:
        return per_user.setdefault(
            uid,
            {
                "task_total": 0,
                "task_done": 0,
                "task_error": 0,
                "task_cancelled": 0,
                "durations": [],
                "storage_bytes": 0,
                "asset_count": 0,
            },
        )

    for uid, status, started, finished in t_rows:
        b = bucket(uid or "")
        b["task_total"] += 1
        if status == "done":
            b["task_done"] += 1
            # 耗时只算真正跑完的（started/finished 都有值）
            if started is not None and finished is not None:
                b["durations"].append((finished - started).total_seconds())
        elif status == "error":
            b["task_error"] += 1
        elif status == "cancelled":
            b["task_cancelled"] += 1

    for uid, fsize in a_rows:
        b = bucket(uid or "")
        b["storage_bytes"] += int(fsize or 0)
        b["asset_count"] += 1

    # ── 补用户名/显示名（无主任务归到「无主数据」）──────────
    user_ids = {uid for uid in per_user if uid}
    users = {}
    if user_ids:
        u_rows = (
            await db.execute(
                select(User.id, User.username, User.display_name, User.role).where(
                    User.id.in_(user_ids)
                )
            )
        ).all()
        users = {r[0]: {"username": r[1], "display_name": r[2], "role": r[3]} for r in u_rows}

    def to_row(uid: str, agg: dict) -> UsageByUser:
        info = users.get(uid, {})
        durations = agg["durations"]
        return UsageByUser(
            user_id=uid,
            username=info.get("username", "无主数据" if not uid else ""),
            display_name=info.get("display_name", ""),
            role=info.get("role", ""),
            task_total=agg["task_total"],
            task_done=agg["task_done"],
            task_error=agg["task_error"],
            task_cancelled=agg["task_cancelled"],
            avg_duration_seconds=(
                sum(durations) / len(durations) if durations else None
            ),
            storage_bytes=agg["storage_bytes"],
            asset_count=agg["asset_count"],
        )

    by_user = [to_row(uid, agg) for uid, agg in sorted(per_user.items())]

    # 合计行：直接把各行加起来；平均耗时按「全部完成任务的时长」重算，
    # 不是各行平均值的平均（后者会被小样本行拉偏）
    all_durations = [d for agg in per_user.values() for d in agg["durations"]]
    totals = UsageByUser(
        user_id="",
        username="合计",
        task_total=sum(r.task_total for r in by_user),
        task_done=sum(r.task_done for r in by_user),
        task_error=sum(r.task_error for r in by_user),
        task_cancelled=sum(r.task_cancelled for r in by_user),
        avg_duration_seconds=(
            sum(all_durations) / len(all_durations) if all_durations else None
        ),
        storage_bytes=sum(r.storage_bytes for r in by_user),
        asset_count=sum(r.asset_count for r in by_user),
    )

    return UsageResponse(
        date_from=date_from or "",
        date_to=date_to or "",
        by_user=by_user,
        totals=totals,
    )
