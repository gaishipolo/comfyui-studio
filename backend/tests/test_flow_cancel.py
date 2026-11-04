"""流程运行取消 —— 运营侧的刹车。

流程卡在某一步（引擎不返回、一步几分钟的长任务）时，界面上得有能被停下来的
办法。这里钉住四件事：

1. 取消后运行状态是 cancelled，**不是 error**（人工取消不是失败）
2. 已经跑完的步骤进度不会被抹掉（跑到第二步才取消，第一步的记录还在）
3. 在飞的任务真的被撤掉，不会在后台继续烧引擎
4. 取消之后不再往下走（后续层不提交）
5. 已经结束的运行不能再取消（否则界面会显示一个假状态）

跑法：python tests/test_flow_cancel.py（自包含，不联网、不碰真库）。
"""
import asyncio
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402


def make_png(color=(3, 4, 5)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


class PicProvider:
    """出图节点：立刻成功。"""

    name = "cancel_pic"
    label = "取消测试-出图"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    submits: list[dict] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        type(self).submits.append(dict(params))
        return f"pic-{len(type(self).submits)}"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "pic.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return make_png()

    async def upload_input(self, data: bytes, filename: str) -> str:
        return f"up:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class StuckProvider:
    """卡住的节点：submit 一直不返回，模拟引擎那边没动静。

    取消要看的就是这段时间 —— 任务没有 provider_task_id，
    撤销靠 execute_task 在 submit 返回后自己补刀（见 TaskService.cancel_task）。
    """

    name = "cancel_stuck"
    label = "取消测试-卡住"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    submits = 0
    gate: asyncio.Event | None = None

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @classmethod
    def reset(cls) -> None:
        cls.submits = 0
        cls.gate = asyncio.Event()

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        cls = type(self)
        cls.submits += 1
        assert cls.gate is not None
        try:
            await asyncio.wait_for(cls.gate.wait(), timeout=30)
        except asyncio.TimeoutError:
            # 测试没放行也要让协程收尾，别把整个进程挂住
            pass
        return f"stuck-{cls.submits}"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(status=TaskStatus.RUNNING, progress=10.0, outputs=[])

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return make_png()

    async def upload_input(self, data: bytes, filename: str) -> str:
        return f"up:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class U:
    id = "u1"
    role = "admin"


async def _wait_for(session, predicate, timeout: float = 20.0):
    """轮询数据库直到 predicate(值) 为真。返回最后一次读到的值。"""
    from app.models.models import FlowRun

    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        async with session() as db:
            async with db.begin():
                value = await predicate(db)
        if value is not None and value is not False:
            return value
    return value


async def scenario() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if not cond else ""), flush=True)

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.config import settings
    from app.database import Base
    from app.engines import factory
    from app.models.models import Flow, FlowRun, Task, Template
    from app.services import flow_service, task_service
    from app.services.flow_service import cancel_run, submit_flow

    with tempfile.TemporaryDirectory(prefix="flow-cancel-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'c.db').as_posix()}")
        session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        original_task_session = task_service.async_session
        original_flow_session = flow_service.async_session
        original_storage = settings.STORAGE_DIR
        task_service.async_session = session
        flow_service.async_session = session
        settings.STORAGE_DIR = root / "storage"

        factory.register_provider(PicProvider.name, PicProvider)
        factory.register_provider(StuckProvider.name, StuckProvider)
        PicProvider.submits = []
        StuckProvider.reset()

        try:
            async with session() as db:
                for tid, name, prov in (
                    ("cancel_pic_tpl", "出图节点", PicProvider.name),
                    ("cancel_stuck_tpl", "卡住节点", StuckProvider.name),
                ):
                    db.add(
                        Template(
                            id=tid,
                            name=name,
                            engine=prov,
                            input_schema={
                                "fields": [
                                    {
                                        "name": "prompt",
                                        "label": "提示词",
                                        "type": "textarea",
                                        "required": True,
                                    },
                                    # 参考图字段是「把两步串成前后依赖」的手段：
                                    # 只靠 prompt 引用输入的话三步互不依赖，会被
                                    # 算成同一层并行跑，测不到「取消后不再往下走」
                                    {
                                        "name": "image",
                                        "label": "参考图",
                                        "type": "file_upload",
                                        "accept": "image",
                                    },
                                ]
                            },
                            param_mapping={},
                            output_config={"nodes": []},
                            is_active=True,
                        )
                    )
                await db.commit()

            # 三段链：出图 → 卡住 → 出图。卡住那段就是我们取消的时刻。
            async with session() as db:
                flow = Flow(
                    name="取消测试",
                    input_schema={
                        "fields": [
                            {
                                "name": "prompt",
                                "label": "提示词",
                                "type": "textarea",
                                "required": True,
                            }
                        ]
                    },
                    steps=[
                        {
                            "id": "s1",
                            "type": "template",
                            "template_id": "cancel_pic_tpl",
                            "count": 1,
                            "params": {"prompt": "{{ input.prompt }}"},
                        },
                        {
                            "id": "s2",
                            "type": "template",
                            "template_id": "cancel_stuck_tpl",
                            "count": 1,
                            "params": {
                                "prompt": "{{ input.prompt }}",
                                "image": "{{ steps.s1.outputs }}",
                            },
                        },
                        {
                            "id": "s3",
                            "type": "template",
                            "template_id": "cancel_pic_tpl",
                            "count": 1,
                            "params": {
                                "prompt": "第三步不该跑",
                                "image": "{{ steps.s2.outputs }}",
                            },
                        },
                    ],
                    created_by="u1",
                )
                db.add(flow)
                await db.commit()
                await db.refresh(flow)
                run = await submit_flow(db, flow, {"prompt": "小猫"}, U)
                run_id = run.id

            # ── 等第一步跑完、第二步卡在 submit ─────────────────
            async def _stuck_started(db):
                from sqlalchemy import select

                rows = (
                    (
                        await db.execute(
                            select(Task).where(Task.flow_run_id == run_id, Task.step_id == "s2")
                        )
                    )
                    .scalars()
                    .all()
                )
                return rows[0] if rows else None

            stuck_task = await _wait_for(session, _stuck_started, timeout=20)
            check("第二步确实起来了（否则后面测的是别的路径）", stuck_task is not None)

            # 取消
            async with session() as db:
                cancelled = await cancel_run(db, run_id)
            check("取消接口返回的运行状态是 cancelled", cancelled.status == "cancelled", cancelled.status)

            # ── 等执行协程收尾 ──────────────────────────────────
            async def _cancelled_and_settled(db):
                from sqlalchemy import select

                r = await db.get(FlowRun, run_id)
                tasks = (
                    (await db.execute(select(Task).where(Task.flow_run_id == run_id)))
                    .scalars()
                    .all()
                )
                if r is None:
                    return None
                still_running = [t for t in tasks if t.status in ("pending", "running")]
                # 状态要停在 cancelled，且没有任务还在跑
                if r.status == "cancelled" and not still_running:
                    return r
                return None

            settled = await _wait_for(session, _cancelled_and_settled, timeout=25)

            async with session() as db:
                final = await db.get(FlowRun, run_id)
                from sqlalchemy import select

                tasks = (
                    (await db.execute(select(Task).where(Task.flow_run_id == run_id)))
                    .scalars()
                    .all()
                )

            check("取消后运行停在 cancelled，没被改写成 error", final.status == "cancelled", final.status)
            check("取消不写错误文案（不是失败）", not final.error_msg, final.error_msg)
            check(
                "已经跑完的第一步记录还在",
                any(s.get("step_id") == "s1" and s.get("status") == "done" for s in (final.steps or [])),
                str(final.steps),
            )
            check("进度没有被清零", final.progress >= 33.0, f"{final.progress}")
            check(
                "卡住的任务被撤掉了",
                any(t.step_id == "s2" and t.status == "cancelled" for t in tasks),
                str([(t.step_id, t.status) for t in tasks]),
            )
            check(
                "取消之后没再往下跑（第三步没提交）",
                all(t.step_id != "s3" for t in tasks),
                str([(t.step_id, t.status) for t in tasks]),
            )
            check("收尾时没有任务还在跑", not any(t.status in ("pending", "running") for t in tasks))
            check("调度协程确实走到了终态（不是超时兜底）", settled is not None)

            # ── 已结束的运行不能再取消 ──────────────────────────
            try:
                async with session() as db:
                    await cancel_run(db, run_id)
                check("已取消的运行再取消会报错", False, "没有报错")
            except ValueError:
                check("已取消的运行再取消会报错", True)

            try:
                async with session() as db:
                    await cancel_run(db, "no-such-run")
                check("取消不存在的运行会报错", False, "没有报错")
            except LookupError:
                check("取消不存在的运行会报错", True)

        finally:
            # 放行卡住的协程，让它们自己收尾，别把进程挂到超时
            if StuckProvider.gate is not None:
                StuckProvider.gate.set()
            await asyncio.sleep(0.5)
            task_service.async_session = original_task_session
            flow_service.async_session = original_flow_session
            settings.STORAGE_DIR = original_storage
            await engine.dispose()

    return checks


def main() -> int:
    checks = asyncio.run(scenario())
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\nRESULT: {passed} passed / {len(checks) - passed} failed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
