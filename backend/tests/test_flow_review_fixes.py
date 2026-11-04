"""流程执行器的回归测试 —— 这次 review 抓到的问题逐条钉住。

覆盖的六个真问题：
1. 中文字段名的循环列表来源识别（旧正则只认 ASCII）
2. 嵌套子流程的具名产出引用（旧代码没摊平，`{{ steps.gc.final }}` 取不到）
3. 循环步骤把进度算成 1000%（按任务数算，不是按步骤数）
4. 内部异常让 FlowRun 永远停在 running（spawn_background 只记日志）
5. 同层子流程把整层堵成串行（声明可并行，实际 await 在提交循环里）
6. 默认流程产出按「谁先跑完」而不是「列表最后一步」

跑法：python tests/test_flow_review_fixes.py（自包含，不联网、不碰真库）。
"""
import asyncio
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

PNG = None


def make_png(color=(7, 8, 9)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


class PicProvider:
    """出图节点。记录每次参数与收到的参考图字节。"""

    name = "review_pic"
    label = "review-出图"
    capabilities = {}
    config_schema = []
    max_concurrency = 8

    submits: list[dict] = []
    uploads: list[bytes] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        type(self).submits.append(dict(params))
        return f"pic-{len(type(self).submits)}"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        idx = task_id.rsplit("-", 1)[-1]
        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": f"pic_{idx}.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
        return make_png()

    async def upload_input(self, data: bytes, filename: str) -> str:
        type(self).uploads.append(data)
        return f"up:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class GateProvider:
    """闸门节点 —— 只有 expect 个实例都进了 submit 才放行。

    用来验证「同层子流程和普通节点是不是真并行」：如果串行，
    子流程里的实例会一直等，最后超时失败。
    """

    name = "review_gate"
    label = "review-闸门"
    capabilities = {}
    config_schema = []
    max_concurrency = 8

    expect = 2
    started = 0
    gate: asyncio.Event | None = None

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @classmethod
    def reset(cls, expect: int) -> None:
        cls.expect = expect
        cls.started = 0
        cls.gate = asyncio.Event()

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        cls = type(self)
        cls.started += 1
        if cls.started >= cls.expect and cls.gate is not None:
            cls.gate.set()
        assert cls.gate is not None
        try:
            await asyncio.wait_for(cls.gate.wait(), timeout=6)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("闸门没开：同层步骤没有并行提交") from exc
        return f"gate-{cls.started}"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "gate.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(
        self, filename: str, subfolder: str = "", output_type: str = "output"
    ) -> bytes:
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


async def _wait_run(session, run_id: str, timeout: float = 15.0) -> tuple[str, str, float]:
    """轮询运行记录到终态，返回 (status, error_msg, progress)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        async with session() as db:
            from app.models.models import FlowRun

            run = await db.get(FlowRun, run_id)
            if run is not None and run.status != "running":
                return run.status, run.error_msg, run.progress
    return "running", "（测试等待超时）", 0.0


async def scenario() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.config import settings
    from app.database import Base
    from app.engines import factory
    from app.models.models import Flow, Template
    from app.services import flow_service, task_service
    from app.services.flow_service import _resolve_flow_outputs, submit_flow

    with tempfile.TemporaryDirectory(prefix="flow-review-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'a.db').as_posix()}")
        session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        original_session = task_service.async_session
        original_flow_session = flow_service.async_session
        original_storage = settings.STORAGE_DIR
        task_service.async_session = session
        flow_service.async_session = session
        settings.STORAGE_DIR = root / "storage"

        factory.register_provider(PicProvider.name, PicProvider)
        factory.register_provider(GateProvider.name, GateProvider)
        PicProvider.submits = []
        PicProvider.uploads = []

        try:
            async with session() as db:
                db.add(
                    Template(
                        id="pic_tpl",
                        name="出图节点",
                        engine=PicProvider.name,
                        input_schema={
                            "fields": [
                                {
                                    "name": "prompt",
                                    "label": "提示词",
                                    "type": "textarea",
                                    "required": True,
                                },
                                {
                                    "name": "image",
                                    "label": "参考图",
                                    "type": "file_upload",
                                },
                            ]
                        },
                        param_mapping={},
                        output_config={"nodes": []},
                        is_active=True,
                    )
                )
                db.add(
                    Template(
                        id="gate_tpl",
                        name="闸门节点",
                        engine=GateProvider.name,
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
                await db.commit()

            # ── 1. 中文列表字段的循环 ──────────────────────────
            async with session() as db:
                loop_flow = Flow(
                    name="中文循环",
                    input_schema={
                        "fields": [
                            {
                                "name": "提示词列表",
                                "label": "提示词列表",
                                "type": "textarea",
                                "multiple": True,
                            }
                        ]
                    },
                    steps=[
                        {
                            "id": "shots",
                            "type": "repeat",
                            "template_id": "pic_tpl",
                            "count": 0,
                            "params": {"prompt": "{{ input.提示词列表[index] }}"},
                        }
                    ],
                    created_by="u1",
                )
                db.add(loop_flow)
                await db.commit()
                await db.refresh(loop_flow)
                loop_id = loop_flow.id

            # 进度探针：旧实现会把循环算成 1000%
            progress_seen: list[float] = []
            original_update = flow_service._update_run

            async def spy_update(run_id: str, **kwargs):
                if kwargs.get("progress") is not None:
                    progress_seen.append(float(kwargs["progress"]))
                return await original_update(run_id, **kwargs)

            flow_service._update_run = spy_update
            try:
                PicProvider.submits = []
                async with session() as db:
                    run = await submit_flow(
                        db,
                        await db.get(Flow, loop_id),
                        {"提示词列表": ["甲", "乙", "丙"]},
                        U(),
                    )
                    run_id = run.id
                status, error, progress = await _wait_run(session, run_id)
            finally:
                flow_service._update_run = original_update

            check("中文列表字段的循环跑完", status == "done", f"{status} {error!r}")
            check(
                "循环按列表长度展开 3 次，每次拿到不同的一项",
                sorted(s.get("prompt") for s in PicProvider.submits)
                == sorted(["甲", "乙", "丙"]),
                str(PicProvider.submits),
            )
            check(
                "进度不超过 100%",
                bool(progress_seen) and max(progress_seen) <= 100.0,
                f"max={max(progress_seen) if progress_seen else None}",
            )
            check("最终进度是 100%", abs(progress - 100.0) < 0.01, str(progress))

            # ── 2. 嵌套子流程的具名产出 ────────────────────────
            async with session() as db:
                grand = Flow(
                    name="孙流程",
                    steps=[
                        {
                            "id": "g",
                            "type": "template",
                            "template_id": "pic_tpl",
                            "params": {"prompt": "孙"},
                        }
                    ],
                    outputs=[{"name": "final", "from": "g", "pick": "all"}],
                    created_by="u1",
                )
                db.add(grand)
                await db.commit()
                await db.refresh(grand)

                child = Flow(
                    name="子流程",
                    steps=[
                        {
                            "id": "gc",
                            "type": "flow",
                            "flow_id": grand.id,
                            "params": {},
                        },
                        {
                            "id": "u",
                            "type": "template",
                            "template_id": "pic_tpl",
                            # 关键：引用孙流程的具名产出
                            "params": {
                                "prompt": "用孙流程的产物",
                                "image": "{{ steps.gc.final }}",
                            },
                        },
                    ],
                    created_by="u1",
                )
                db.add(child)
                await db.commit()
                await db.refresh(child)

                parent = Flow(
                    name="父流程",
                    steps=[
                        {"id": "sub", "type": "flow", "flow_id": child.id, "params": {}}
                    ],
                    created_by="u1",
                )
                db.add(parent)
                await db.commit()
                await db.refresh(parent)
                parent_id = parent.id

            PicProvider.uploads = []
            async with session() as db:
                run = await submit_flow(db, await db.get(Flow, parent_id), {}, U())
                run_id = run.id
            status, error, _progress = await _wait_run(session, run_id)
            check("嵌套子流程的具名产出能取到", status == "done", f"{status} {error!r}")
            check("下游收到了孙流程的产物", bool(PicProvider.uploads), str(len(PicProvider.uploads)))

            # ── 3. 同层子流程和普通节点并行 ────────────────────
            async with session() as db:
                gate_child = Flow(
                    name="闸门子流程",
                    steps=[
                        {
                            "id": "g",
                            "type": "template",
                            "template_id": "gate_tpl",
                            "params": {"prompt": "子"},
                        }
                    ],
                    created_by="u1",
                )
                db.add(gate_child)
                await db.commit()
                await db.refresh(gate_child)

                gate_parent = Flow(
                    name="并行父流程",
                    steps=[
                        {
                            "id": "sub",
                            "type": "flow",
                            "flow_id": gate_child.id,
                            "params": {},
                        },
                        {
                            "id": "direct",
                            "type": "template",
                            "template_id": "gate_tpl",
                            "params": {"prompt": "父"},
                        },
                    ],
                    created_by="u1",
                )
                db.add(gate_parent)
                await db.commit()
                await db.refresh(gate_parent)
                gate_parent_id = gate_parent.id

            GateProvider.reset(expect=2)
            started_at = time.monotonic()
            async with session() as db:
                run = await submit_flow(
                    db, await db.get(Flow, gate_parent_id), {}, U()
                )
                run_id = run.id
            status, error, _progress = await _wait_run(session, run_id)
            elapsed = time.monotonic() - started_at
            check("同层子流程与普通节点并行跑完", status == "done", f"{status} {error!r}")
            check(
                "没有被子流程串行堵住（< 5 秒）",
                status == "done" and elapsed < 5.0,
                f"{elapsed:.1f}s",
            )

            # ── 4. 内部异常不能把运行卡在 running ───────────────
            async with session() as db:
                crash_flow = Flow(
                    name="内部异常",
                    steps=[
                        {
                            "id": "g",
                            "type": "template",
                            "template_id": "pic_tpl",
                            "params": {"prompt": "x"},
                        }
                    ],
                    created_by="u1",
                )
                db.add(crash_flow)
                await db.commit()
                await db.refresh(crash_flow)
                crash_id = crash_flow.id

            original_collect = flow_service._collect_outputs

            async def boom(_task_id: str):
                raise RuntimeError("模拟内部错误")

            flow_service._collect_outputs = boom
            try:
                async with session() as db:
                    run = await submit_flow(db, await db.get(Flow, crash_id), {}, U())
                    run_id = run.id
                status, error, _progress = await _wait_run(session, run_id)
            finally:
                flow_service._collect_outputs = original_collect

            check("内部异常落成 error 而不是卡住", status == "error", status)
            check(
                "错误信息说明是内部错误",
                "内部错误" in (error or ""),
                repr(error),
            )

            # ── 5. 默认产出按列表顺序，不按完成顺序 ─────────────
            ctx = {
                "steps": {
                    "b": {"outputs": [{"asset_id": "B"}]},
                    "a": {"outputs": [{"asset_id": "A"}]},
                }
            }
            picked = _resolve_flow_outputs([], ctx, [{"id": "a"}, {"id": "b"}])
            check(
                "默认产出取列表最后一步",
                bool(picked) and picked[0]["outputs"][0]["asset_id"] == "B",
                str(picked),
            )
        finally:
            task_service.async_session = original_session
            flow_service.async_session = original_flow_session
            settings.STORAGE_DIR = original_storage
            factory.unregister_provider(PicProvider.name)
            factory.unregister_provider(GateProvider.name)
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
