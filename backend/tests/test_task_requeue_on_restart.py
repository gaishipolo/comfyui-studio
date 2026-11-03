"""重启收拾遗留任务：排队中的该重排，提交到一半的该标失败。

后台执行是进程内的 asyncio 协程，进程一没它们就永远停在原状态。启动时
`reap_stale_tasks` 必须把遗留任务分三类处置，分错任一类的代价都不小：

1. **pending 且没有 provider_task_id** —— 只是排在并发闸门后面，协程随进程
   一起没了。重新排队即可，**不能标失败**：一次部署重启让几十个排队任务
   集体作废，用户得一个个手点重试，是集体投诉级别的体验问题。
2. **running 且没有 provider_task_id** —— submit 请求已经发出、但还没拿到
   引擎侧 ID，无法确认对方收没收到。重提可能让同一张图跑两遍白烧显存，
   所以保守标失败，把决定权交回用户。
3. **有 provider_task_id** —— 引擎侧还在跑或已经跑完，接回去继续等（resume），
   绝不能重新 submit。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.models import Task, Template  # noqa: E402
from app.services import task_service as ts  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))


_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
engine = create_async_engine(f"sqlite+aiosqlite:///{_tmp.name}")
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# task_service 里 `from app.database import async_session` 是它自己命名空间的绑定，
# 改这个属性就能让 reap_stale_tasks 走测试库
ts.async_session = Session

TEMPLATE_ID = "tpl-reap"
SPAWNED: list[tuple[str, bool]] = []


async def fresh_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as db:
        db.add(
            Template(
                id=TEMPLATE_ID,
                name="收拾模板",
                engine="fake",
                workflow_file="",
                category="image",
                input_schema={"fields": []},
                param_mapping={},
                output_config={},
                is_active=True,
            )
        )
        await db.commit()


async def add_task(task_id: str, status: str, provider_task_id: str = "") -> None:
    async with Session() as db:
        db.add(
            Task(
                id=task_id,
                template_id=TEMPLATE_ID,
                status=status,
                provider_task_id=provider_task_id,
                params={},
            )
        )
        await db.commit()


async def reload(task_id: str) -> Task | None:
    """开新会话读**已提交**的最终状态，读完脱会话，避免 MissingGreenlet。"""
    async with Session() as db:
        task = await db.get(Task, task_id)
        if task is not None:
            db.expunge(task)
        return task


async def fake_spawn(task_id: str, resume: bool = False) -> bool:
    SPAWNED.append((task_id, resume))
    return True


async def run_reap() -> dict:
    SPAWNED.clear()
    original = ts.spawn_task
    ts.spawn_task = fake_spawn  # type: ignore[assignment]
    try:
        return await ts.TaskService.reap_stale_tasks()
    finally:
        ts.spawn_task = original  # type: ignore[assignment]


async def test_pending_is_requeued_not_failed() -> None:
    await fresh_db()
    await add_task("t-pending", "pending")

    outcome = await run_reap()
    task = await reload("t-pending")

    check(
        "排队中的任务被重新排队（requeued）",
        outcome["requeued"] == ["t-pending"],
        f"requeued={outcome['requeued']} failed={outcome['failed']}",
    )
    check("排队中的任务没被标失败", outcome["failed"] == [], f"failed={outcome['failed']}")
    check(
        "状态保持 pending，等 execute_task 自己推进",
        task is not None and task.status == "pending",
        f"status={getattr(task, 'status', None)}",
    )
    check("没有错误信息写进去", task is not None and not task.error_msg, f"error={getattr(task, 'error_msg', '')}")
    check(
        "重排用的是「重新提交」而不是「接回」",
        SPAWNED == [("t-pending", False)],
        f"spawned={SPAWNED}",
    )


async def test_running_without_provider_id_fails() -> None:
    await fresh_db()
    await add_task("t-half", "running")

    outcome = await run_reap()
    task = await reload("t-half")

    check(
        "提交到一半的任务标失败（避免重复烧 GPU）",
        outcome["failed"] == ["t-half"],
        f"failed={outcome['failed']} requeued={outcome['requeued']}",
    )
    check(
        "状态是 error 且带可读原因",
        task is not None and task.status == "error" and "重启" in (task.error_msg or ""),
        f"status={getattr(task, 'status', None)} msg={getattr(task, 'error_msg', '')}",
    )
    check("失败的任务不重新 spawn", SPAWNED == [], f"spawned={SPAWNED}")


async def test_submitted_task_is_resumed() -> None:
    await fresh_db()
    await add_task("t-running", "running", provider_task_id="p-1")
    await add_task("t-pend-sub", "pending", provider_task_id="p-2")

    outcome = await run_reap()
    running = await reload("t-running")
    pending = await reload("t-pend-sub")

    check(
        "有 provider_task_id 的都接回去",
        sorted(outcome["resumed"]) == ["t-pend-sub", "t-running"],
        f"resumed={outcome['resumed']}",
    )
    check("接回的不进重排队列", outcome["requeued"] == [], f"requeued={outcome['requeued']}")
    check("接回的不判失败", outcome["failed"] == [], f"failed={outcome['failed']}")
    check(
        "接回用的是 resume=True（绝不重新 submit）",
        all(resume for _, resume in SPAWNED) and len(SPAWNED) == 2,
        f"spawned={SPAWNED}",
    )
    check(
        "接回不改写终态之外的元数据",
        running is not None and running.provider_task_id == "p-1" and pending is not None,
        f"running={getattr(running, 'provider_task_id', None)} pending={getattr(pending, 'provider_task_id', None)}",
    )


async def test_terminal_tasks_untouched() -> None:
    await fresh_db()
    await add_task("t-done", "done")
    await add_task("t-error", "error")
    await add_task("t-cancelled", "cancelled")

    outcome = await run_reap()

    check(
        "终态任务一条都不碰",
        outcome["resumed"] == [] and outcome["requeued"] == [] and outcome["failed"] == [],
        f"{outcome}",
    )
    for task_id, expected in (
        ("t-done", "done"),
        ("t-error", "error"),
        ("t-cancelled", "cancelled"),
    ):
        task = await reload(task_id)
        check(
            f"{expected} 状态没被改写",
            task is not None and task.status == expected,
            f"status={getattr(task, 'status', None)}",
        )


async def test_switch_off_restores_old_behaviour() -> None:
    """关掉开关后，排队中的任务回到「标失败」的旧行为。"""
    await fresh_db()
    await add_task("t-off", "pending")

    original = settings.RESUME_PENDING_ON_STARTUP
    settings.RESUME_PENDING_ON_STARTUP = False
    try:
        outcome = await run_reap()
    finally:
        settings.RESUME_PENDING_ON_STARTUP = original

    check(
        "关掉开关后排队任务标失败",
        outcome["failed"] == ["t-off"] and outcome["requeued"] == [],
        f"{outcome}",
    )


async def main() -> int:
    await test_pending_is_requeued_not_failed()
    await test_running_without_provider_id_fails()
    await test_submitted_task_is_resumed()
    await test_terminal_tasks_untouched()
    await test_switch_off_restores_old_behaviour()

    failed = [r for r in _results if not r[1]]
    print(f"\nRESULT: {len(_results) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
