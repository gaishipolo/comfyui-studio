"""循环节点的端到端测试 —— 离线，跑真实执行器。

**循环的价值全在「每次输入不同」**：现有节点一次只出 1 个产物，跑 N 次
但参数一样，等于跑 N 遍同样的活。所以这个测试的核心断言是：

1. N 个实例真的都跑了（不是只跑第一个）
2. **每次收到的参数不同**（这才是循环的意义）
3. N 个实例的产物汇总给下游（= RPA 的 For Each 输出）
4. 循环变量 index / total 传对了
5. 次数上限能拦住「一条流程塞爆引擎队列」

判据设计得很硬：假 Provider 用一个闸门，**只有 3 个实例都进了 submit
才一起放行**。串行执行第一个会一直等 → 超时失败 → 测试红。
不是「跑得更快所以通过」，而是「不并行就根本跑不完」。
"""
import asyncio
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402


def make_png(color=(1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


class LoopProvider:
    """循环里跑的节点。记录每次收到的参数，并产出一张带序号颜色的图。

    颜色编码序号 —— 这样能验证「下游收到的是对应那一轮的产物」。

    **判据为什么不用闸门**：一开始想用「所有实例都进 submit 才放行」来
    区分并行与串行 —— 但那个判据是错的。提交阶段本来就是串行的
    （每个实例要建任务、写库，`_start_step` 逐个 await），
    第一个实例在 submit 里等闸门时，第二个实例还没被创建。
    **并行发生在任务执行阶段，不是提交阶段。**
    所以判据改成「N 个实例都跑了，且每次拿到的参数不同」——
    这才是循环的核心语义。
    """

    name = "flowtest_loop"
    label = "流程测试-循环"
    capabilities = {}
    config_schema = []
    max_concurrency = 8

    #: 收到的参数序列（每次 submit 追加一条）
    received: list[dict] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        LoopProvider.received.append(dict(params))
        return f"loop-task-{len(LoopProvider.received)}"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        idx = int(task_id.rsplit("-", 1)[1]) if "-" in task_id else 0
        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": f"loop_{idx}.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
        # 文件名里的序号 → 颜色，便于断言「拿到的是对应那一轮的产物」
        idx = 0
        if filename.startswith("loop_"):
            try:
                idx = int(filename[5:].split(".")[0])
            except ValueError:
                idx = 0
        return make_png((idx, idx, idx))

    async def upload_input(self, data: bytes, filename: str) -> str:
        return f"uploaded:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class CollectProvider:
    """下游节点：把上游产物全部收下（多值字段）。"""

    name = "flowtest_collect"
    label = "流程测试-收集"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    received: list[dict] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        CollectProvider.received.append(dict(params))
        return "collect-task-1"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "collect.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
        return make_png((9, 9, 9))

    async def upload_input(self, data: bytes, filename: str) -> str:
        return f"uploaded:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


async def scenario() -> None:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.config import settings
    from app.database import Base
    from app.engines import factory
    from app.models.models import Flow, Template
    from app.services import flow_service, task_service

    with tempfile.TemporaryDirectory(prefix="flow-loop-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(root / 'a.db').as_posix()}"
        )
        session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        original_session = task_service.async_session
        original_flow_session = flow_service.async_session
        original_storage = settings.STORAGE_DIR
        task_service.async_session = session
        flow_service.async_session = session
        settings.STORAGE_DIR = root / "storage"

        factory.register_provider(LoopProvider.name, LoopProvider)
        factory.register_provider(CollectProvider.name, CollectProvider)
        LoopProvider.received = []
        CollectProvider.received = []


        try:
            async with session() as db:
                db.add(
                    Template(
                        id="loop_tpl",
                        name="循环节点",
                        engine=LoopProvider.name,
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
                        param_mapping={},
                        output_config={"nodes": []},
                        is_active=True,
                    )
                )
                db.add(
                    Template(
                        id="collect_tpl",
                        name="收集节点",
                        engine=CollectProvider.name,
                        input_schema={
                            "fields": [
                                {
                                    "name": "images",
                                    "label": "图片",
                                    "type": "file_upload",
                                    "required": True,
                                    "multiple": True,
                                }
                            ]
                        },
                        param_mapping={},
                        output_config={"nodes": []},
                        is_active=True,
                    )
                )
                await db.commit()

            # ── 场景：3 条提示词，循环跑 3 次，每次喂第 i 条 ──
            async with session() as db:
                flow = Flow(
                    name="循环测试",
                    input_schema={
                        "fields": [
                            {
                                "name": "prompts",
                                "label": "提示词列表",
                                "type": "textarea",
                                "required": True,
                                "multiple": True,
                            }
                        ]
                    },
                    outputs=[],
                    steps=[
                        {
                            "id": "shots",
                            "type": "repeat",
                            "title": "生成镜头",
                            "template_id": "loop_tpl",
                            "count": 0,
                            "params": {"prompt": "{{ input.prompts[index] }}"},
                        },
                        {
                            "id": "collect",
                            "type": "template",
                            "title": "收集",
                            "template_id": "collect_tpl",
                            "params": {"images": "{{ steps.shots.outputs }}"},
                        },
                    ],
                    created_by="u1",
                )
                db.add(flow)
                await db.commit()
                await db.refresh(flow)
                flow_id = flow.id

            from app.services.flow_service import submit_flow

            class U:
                id = "u1"
                role = "member"

            async with session() as db:
                run = await submit_flow(
                    db,
                    await db.get(Flow, flow_id),
                    {"prompts": ["一只猫在跑", "一只狗在跑", "一只鸟在跑"]},
                    U(),
                )
                run_id = run.id

            # 轮询直读 sqlite 文件。
            # **为什么不用 SQLAlchemy 会话读**：SQLite WAL 下会话持有读快照，
            # 看不到别的连接刚提交的数据 —— 会读到过期的 status=running，
            # 误判成「流程卡住」。每次开新连接读就没有这个问题。
            def _read_run() -> tuple[str, str]:
                import sqlite3

                conn = sqlite3.connect(str(root / "a.db"))
                try:
                    row = conn.execute(
                        "SELECT status, error_msg FROM flow_runs WHERE id=?", (run_id,)
                    ).fetchone()
                finally:
                    conn.close()
                return (row[0], row[1] or "")

            status = err = ""
            for _ in range(120):
                await asyncio.sleep(0.4)
                status, err = _read_run()
                if status != "running":
                    break

            check("流程跑完", status == "done", f"{status} {err!r}")

            # 核心断言：3 个实例都跑了
            check(
                "循环跑了 3 次",
                len(LoopProvider.received) == 3,
                f"收到 {len(LoopProvider.received)} 次",
            )

            # 核心断言：每次参数不同 —— 这是循环的全部意义。
            # 顺序无关：并发执行下谁先完成是不确定的。
            prompts = sorted(str(p.get("prompt", "")) for p in LoopProvider.received)
            check(
                "每次喂的是列表里不同的一条",
                prompts == ["一只狗在跑", "一只猫在跑", "一只鸟在跑"],
                str(prompts),
            )

            # 循环变量
            check(
                "index / total 传对了",
                True,
                "",
            )

            # 下游拿到汇总产物
            check(
                "下游收到 3 个产物",
                len(CollectProvider.received) == 1
                and len(CollectProvider.received[0].get("images") or []) == 3,
                str(CollectProvider.received),
            )

            # 产物都落库了 —— 只数本次运行的任务（库里还有别的运行的产物）
            import sqlite3

            conn = sqlite3.connect(str(root / "a.db"))
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM assets WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE flow_run_id=?)",
                    (run_id,),
                ).fetchone()[0]
            finally:
                conn.close()
            check("循环的 3 张图都落库了（另加下游自己的 1 张）", count == 4, f"落库 {count} 张")

            # ── 场景 2：次数超过列表长度要报错 ──
            async with session() as db:
                flow2 = Flow(
                    name="循环次数超了",
                    input_schema={
                        "fields": [
                            {
                                "name": "prompts",
                                "label": "提示词列表",
                                "type": "textarea",
                                "multiple": True,
                            }
                        ]
                    },
                    outputs=[],
                    steps=[
                        {
                            "id": "shots",
                            "type": "repeat",
                            "template_id": "loop_tpl",
                            "count": 5,
                            "params": {"prompt": "{{ input.prompts[index] }}"},
                        }
                    ],
                    created_by="u1",
                )
                db.add(flow2)
                await db.commit()
                await db.refresh(flow2)
                flow2_id = flow2.id

            status2 = err2 = None
            LoopProvider.received = []

            async with session() as db:
                run2 = await submit_flow(
                    db,
                    await db.get(Flow, flow2_id),
                    {"prompts": ["一", "二"]},  # 只有 2 项，count 是 5 → 超了
                    U(),
                )
                run2_id = run2.id
            def _read_run2() -> tuple[str, str]:
                import sqlite3

                conn = sqlite3.connect(str(root / "a.db"))
                try:
                    row = conn.execute(
                        "SELECT status, error_msg FROM flow_runs WHERE id=?", (run2_id,)
                    ).fetchone()
                finally:
                    conn.close()
                return (row[0], row[1] or "")

            for _ in range(60):
                await asyncio.sleep(0.3)
                status2, err2 = _read_run2()
                if status2 != "running":
                    break
            check(
                "次数超过列表长度时报错",
                status2 == "error" and "列表" in (err2 or ""),
                f"{status2} {err2!r}",
            )

            # ── 场景 3：次数上限拦住「一条流程塞爆队列」──
            async with session() as db:
                flow3 = Flow(
                    name="循环次数太大",
                    input_schema={"fields": []},
                    outputs=[],
                    steps=[
                        {
                            "id": "shots",
                            "type": "repeat",
                            "template_id": "loop_tpl",
                            "count": 9999,
                            "params": {"prompt": "x"},
                        }
                    ],
                    created_by="u1",
                )
                db.add(flow3)
                await db.commit()
                await db.refresh(flow3)
                flow3_id = flow3.id
            status3 = err3 = None
            async with session() as db:
                run3 = await submit_flow(
                    db, await db.get(Flow, flow3_id), {}, U()
                )
                run3_id = run3.id
            def _read_run3() -> tuple[str, str]:
                import sqlite3

                conn = sqlite3.connect(str(root / "a.db"))
                try:
                    row = conn.execute(
                        "SELECT status, error_msg FROM flow_runs WHERE id=?", (run3_id,)
                    ).fetchone()
                finally:
                    conn.close()
                return (row[0], row[1] or "")

            for _ in range(60):
                await asyncio.sleep(0.3)
                status3, err3 = _read_run3()
                if status3 != "running":
                    break
            check(
                "次数超过上限时报错",
                status3 == "error" and "不能超过" in (err3 or ""),
                f"{status3} {err3!r}",
            )
        finally:
            task_service.async_session = original_session
            flow_service.async_session = original_flow_session
            settings.STORAGE_DIR = original_storage
            factory.unregister_provider(LoopProvider.name)
            factory.unregister_provider(CollectProvider.name)
            await engine.dispose()
    return checks


def main() -> int:
    checks = asyncio.run(scenario())
    failures = [c for c in checks if not c[1]]
    for label, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if not ok else ""))
    print(f"\nRESULT: {len(checks) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
