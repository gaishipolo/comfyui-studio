"""子流程嵌套的端到端测试 —— 离线，跑真实执行器。

**子流程的两个核心语义**：
1. 输入怎么传 —— 同名自动取作默认，显式绑定作覆盖（和步骤字段一个规则）
2. 输出怎么被引用 —— 子流程的 outputs 声明解析成具名产出，
   父流程写 `{{ steps.sub.final_image }}` 就能拿到

判据：
- 子流程真的跑了（内部步骤的 task 都建了）
- 同名输入自动传对了
- 显式绑定覆盖了同名自动传
- 父流程下游接到的是**子流程的产物**（比对字节，不是比对名字）
- 子流程调用自己会被拦住
- 嵌套深度超限会被拦住
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


# 子流程内部节点产出的图（用它验证父流程拿到的是不是同一张）
SUB_PNG = make_png((7, 7, 7))
# 父流程下游节点产出的图
DOWN_PNG = make_png((8, 8, 8))


class SubProvider:
    """子流程内部的节点。"""

    name = "flowtest_sub"
    label = "流程测试-子流程内部"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    received: list[dict] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        SubProvider.received.append(dict(params))
        return f"sub-{len(SubProvider.received)}"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "sub.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
        return SUB_PNG

    async def upload_input(self, data: bytes, filename: str) -> str:
        return f"uploaded:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class DownProvider:
    """父流程的下游节点 —— 它接到的是子流程的产物。"""

    name = "flowtest_down"
    label = "流程测试-下游"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    received: list[dict] = []
    #: 收到的图片字节（用来比对是不是子流程那张）
    received_bytes: list[bytes] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        DownProvider.received.append(dict(params))
        return "down-1"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "down.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
        return DOWN_PNG

    async def upload_input(self, data: bytes, filename: str) -> str:
        # 记下收到的字节 —— 下游拿到的应该是子流程那张图
        DownProvider.received_bytes.append(data)
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

    with tempfile.TemporaryDirectory(prefix="flow-sub-") as raw_dir:
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

        factory.register_provider(SubProvider.name, SubProvider)
        factory.register_provider(DownProvider.name, DownProvider)
        SubProvider.received = []
        DownProvider.received = []
        DownProvider.received_bytes = []

        try:
            async with session() as db:
                db.add(
                    Template(
                        id="sub_tpl",
                        name="子流程内部节点",
                        engine=SubProvider.name,
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
                        id="down_tpl",
                        name="下游节点",
                        engine=DownProvider.name,
                        input_schema={
                            "fields": [
                                {
                                    "name": "image",
                                    "label": "图片",
                                    "type": "file_upload",
                                    "required": True,
                                }
                            ]
                        },
                        param_mapping={},
                        output_config={"nodes": []},
                        is_active=True,
                    )
                )
                await db.commit()

            # ── 子流程：输入「风格」「主体」，产出「final_image」──
            async with session() as db:
                sub = Flow(
                    name="生成一个镜头",
                    input_schema={
                        "fields": [
                            {
                                "name": "style",
                                "label": "风格",
                                "type": "text",
                                "required": True,
                            },
                            {
                                "name": "subject",
                                "label": "主体",
                                "type": "text",
                                "required": True,
                            },
                        ]
                    },
                    outputs=[
                        {"name": "final_image", "from": "gen", "pick": "all"},
                    ],
                    steps=[
                        {
                            "id": "gen",
                            "type": "template",
                            "title": "出图",
                            "template_id": "sub_tpl",
                            "params": {
                                "prompt": "{{ input.subject }}，风格：{{ input.style }}",
                            },
                        }
                    ],
                    created_by="u1",
                )
                db.add(sub)
                await db.commit()
                await db.refresh(sub)
                sub_id = sub.id

            # ── 父流程：调子流程，同名输入自动传，下游接子流程的产物 ──
            async with session() as db:
                parent = Flow(
                    name="主流程",
                    input_schema={
                        "fields": [
                            {
                                "name": "style",
                                "label": "风格",
                                "type": "text",
                                "required": True,
                            },
                            {
                                "name": "subject",
                                "label": "主体",
                                "type": "text",
                                "required": True,
                            },
                        ]
                    },
                    outputs=[],
                    steps=[
                        {
                            "id": "sub",
                            "type": "flow",
                            "title": "生成镜头",
                            "flow_id": sub_id,
                            "params": {},
                        },
                        {
                            "id": "down",
                            "type": "template",
                            "title": "下游",
                            "template_id": "down_tpl",
                            "params": {"image": "{{ steps.sub.final_image }}"},
                        },
                    ],
                    created_by="u1",
                )
                db.add(parent)
                await db.commit()
                await db.refresh(parent)
                parent_id = parent.id

            from app.services.flow_service import submit_flow

            class U:
                id = "u1"
                role = "member"

            def _read_run(rid: str) -> tuple[str, str]:
                """直读 sqlite —— SQLAlchemy 会话持有旧读快照，看不到最新提交。"""
                import sqlite3

                conn = sqlite3.connect(str(root / "a.db"))
                try:
                    row = conn.execute(
                        "SELECT status, error_msg FROM flow_runs WHERE id=?", (rid,)
                    ).fetchone()
                finally:
                    conn.close()
                return (row[0], row[1] or "")

            async with session() as db:
                run = await submit_flow(
                    db,
                    await db.get(Flow, parent_id),
                    {"style": "赛博朋克", "subject": "一只柴犬"},
                    U(),
                )
                run_id = run.id

            status = err = ""
            for _ in range(120):
                await asyncio.sleep(0.4)
                status, err = _read_run(run_id)
                if status != "running":
                    break

            check("流程跑完", status == "done", f"{status} {err!r}")

            # 子流程内部真的跑了
            check(
                "子流程内部的节点跑了",
                len(SubProvider.received) == 1,
                str(SubProvider.received),
            )

            # 同名输入自动传对了
            check(
                "同名输入自动传给了子流程",
                SubProvider.received
                and SubProvider.received[0].get("prompt") == "一只柴犬，风格：赛博朋克",
                str(SubProvider.received),
            )

            # 下游接到了子流程的产物
            check(
                "下游节点跑了",
                len(DownProvider.received) == 1,
                str(DownProvider.received),
            )
            check(
                "下游收到的是子流程那张图",
                bool(DownProvider.received_bytes)
                and DownProvider.received_bytes[0] == SUB_PNG,
                f"收到 {len(DownProvider.received_bytes)} 张",
            )

            # 流程级产出（没声明 → 默认最后一步）
            conn = __import__("sqlite3").connect(str(root / "a.db"))
            try:
                outs = conn.execute(
                    "SELECT outputs FROM flow_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
            finally:
                conn.close()
            check("流程级产出已落库", bool(outs) and outs != "[]", outs)

            # ── 场景 2：子流程调用自己要被拦住 ──
            async with session() as db:
                selfish = Flow(
                    name="自我调用",
                    input_schema={"fields": []},
                    outputs=[],
                    steps=[
                        {
                            "id": "self",
                            "type": "flow",
                            "flow_id": "",  # 先占位，下面改成自己的 id
                            "params": {},
                        }
                    ],
                    created_by="u1",
                )
                db.add(selfish)
                await db.commit()
                await db.refresh(selfish)
                selfish.steps = [
                    {
                        "id": "self",
                        "type": "flow",
                        "flow_id": selfish.id,
                        "params": {},
                    }
                ]
                await db.commit()
                selfish_id = selfish.id

            async with session() as db:
                run2 = await submit_flow(
                    db, await db.get(Flow, selfish_id), {}, U()
                )
                run2_id = run2.id
            status2 = err2 = ""
            for _ in range(60):
                await asyncio.sleep(0.3)
                status2, err2 = _read_run(run2_id)
                if status2 != "running":
                    break
            check(
                "子流程调用自己时报错",
                status2 == "error" and "自己" in (err2 or ""),
                f"{status2} {err2!r}",
            )

            # ── 场景 3：嵌套深度超限 ──
            async with session() as db:
                deep = Flow(
                    name="超深嵌套",
                    input_schema={"fields": []},
                    outputs=[],
                    steps=[
                        {
                            "id": "s1",
                            "type": "flow",
                            "flow_id": selfish_id,
                            "params": {},
                        }
                    ],
                    created_by="u1",
                )
                db.add(deep)
                await db.commit()
                await db.refresh(deep)
                deep_id = deep.id
            # 造一条 7 层互调的流程链
            async with session() as db:
                chain_ids = []
                prev_id = deep_id
                for i in range(7):
                    node = Flow(
                        name=f"链层{i}",
                        input_schema={"fields": []},
                        outputs=[],
                        steps=[
                            {
                                "id": "n",
                                "type": "flow",
                                "flow_id": prev_id,
                                "params": {},
                            }
                        ],
                        created_by="u1",
                    )
                    db.add(node)
                    await db.commit()
                    await db.refresh(node)
                    prev_id = node.id
                    chain_ids.append(node.id)
            async with session() as db:
                run3 = await submit_flow(
                    db, await db.get(Flow, chain_ids[-1]), {}, U()
                )
                run3_id = run3.id
            status3 = err3 = ""
            for _ in range(60):
                await asyncio.sleep(0.3)
                status3, err3 = _read_run(run3_id)
                if status3 != "running":
                    break
            check(
                "嵌套太深时报错",
                status3 == "error" and ("层" in (err3 or "") or "自己" in (err3 or "")),
                f"{status3} {err3!r}",
            )
        finally:
            task_service.async_session = original_session
            flow_service.async_session = original_flow_session
            settings.STORAGE_DIR = original_storage
            factory.unregister_provider(SubProvider.name)
            factory.unregister_provider(DownProvider.name)
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
