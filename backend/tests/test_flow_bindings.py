"""自动接上（同名绑定）—— 参数不配也能跑。

**这套东西解决什么**：节点参数不配也能跑，是「不用教用户写表达式」的根本。
但「不配 = 接什么」必须有唯一答案，否则界面显示的和实际跑的不是一回事。

覆盖三块：
1. 规则本身（按标签同名接，标签空时看内部标识）
2. 端到端：流程输入「提示词」→ 节点字段「提示词」自动接上；
   上一步产出名「参考图」→ 下游「参考图」字段自动接上产物字节
3. 优先级：显式配了的永远压过自动；接不上就不接（用节点默认值）

跑法：python tests/test_flow_bindings.py（自包含，不联网、不碰真库）。
"""
import asyncio
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402


def make_png(color=(9, 9, 9)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


GEN_PNG = make_png()


class GenProvider:
    """出图节点：文生图，产出一张 PNG。"""

    name = "bind_gen"
    label = "绑定测试-出图"
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
        return "gen-1"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "gen.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return GEN_PNG

    async def upload_input(self, data: bytes, filename: str) -> str:
        return f"up:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class EditProvider:
    """图生图节点：记下收到的参考图字节与提示词。"""

    name = "bind_edit"
    label = "绑定测试-改图"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    received: list[dict] = []

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        type(self).received.append(dict(params))
        return "edit-1"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "edit.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return make_png((1, 2, 3))

    async def upload_input(self, data: bytes, filename: str) -> str:
        type(self).received.append({"upload": data})
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


async def _wait_run(session, run_id: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        async with session() as db:
            from app.models.models import FlowRun

            run = await db.get(FlowRun, run_id)
            if run is not None and run.status != "running":
                return run.status, run.error_msg
    return "running", "（测试等待超时）"


async def scenario() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if not cond else ""), flush=True)

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.config import settings
    from app.database import Base
    from app.engines import factory
    from app.models.models import Flow, Template
    from app.services import flow_service, task_service
    from app.services.flow_bindings import match_input, match_upstream, same_name
    from app.services.flow_service import submit_flow

    # ── 规则本身 ────────────────────────────────────────────────
    check("标签相同算同名", same_name("提示词", "prompt", "提示词", "input1"))
    check("标签不同不算同名", not same_name("提示词", "prompt", "画面", "input1"))
    check("标签空时看内部标识", same_name("", "prompt", "", "prompt"))
    check("标签有值时不会退化到内部标识", not same_name("提示词", "prompt", "别的", "prompt"))

    hit = match_input(
        {"name": "prompt", "label": "提示词"},
        [{"name": "input1", "label": "提示词"}],
    )
    check("字段标签能匹配到流程输入", hit is not None and hit["name"] == "input1")
    check(
        "匹配不到时返回 None",
        match_input({"name": "prompt", "label": "风格"}, [{"name": "input1", "label": "提示词"}])
        is None,
    )
    up = match_upstream({"name": "image", "label": "参考图"}, {"参考图": "s1"})
    check(
        "字段标签能匹配到上游产出名",
        up is not None and up["step_id"] == "s1" and up["name"] == "参考图",
        str(up),
    )

    with tempfile.TemporaryDirectory(prefix="flow-bindings-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'b.db').as_posix()}")
        session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        original_task_session = task_service.async_session
        original_flow_session = flow_service.async_session
        original_storage = settings.STORAGE_DIR
        task_service.async_session = session
        flow_service.async_session = session
        settings.STORAGE_DIR = root / "storage"

        factory.register_provider(GenProvider.name, GenProvider)
        factory.register_provider(EditProvider.name, EditProvider)
        GenProvider.submits = []
        EditProvider.received = []

        try:
            async with session() as db:
                db.add(
                    Template(
                        id="bind_gen_tpl",
                        name="出图节点",
                        engine=GenProvider.name,
                        input_schema={
                            "fields": [
                                {
                                    "name": "prompt",
                                    "label": "提示词",
                                    "type": "textarea",
                                    "required": True,
                                },
                                {
                                    "name": "style",
                                    "label": "风格",
                                    "type": "text",
                                    "required": False,
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
                        id="bind_edit_tpl",
                        name="改图节点",
                        engine=EditProvider.name,
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

            # 流程输入只有一个「提示词」；步骤参数全靠自动接
            async with session() as db:
                flow = Flow(
                    name="自动接上",
                    input_schema={
                        "fields": [
                            {
                                "name": "input1",
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
                            "template_id": "bind_gen_tpl",
                            "count": 1,
                            "params": {},
                            "auto_fields": ["prompt"],
                            "output_name": "参考图",
                        },
                        {
                            "id": "s2",
                            "type": "template",
                            "template_id": "bind_edit_tpl",
                            "count": 1,
                            "params": {},
                            "auto_fields": ["prompt", "image"],
                        },
                    ],
                    created_by="u1",
                )
                db.add(flow)
                await db.commit()
                await db.refresh(flow)
                run = await submit_flow(db, flow, {"input1": "一只拉布拉多"}, U)
                run_id = run.id

            status, err = await _wait_run(session, run_id)
            check("流程跑通了", status == "done", f"{status} {err}")

            check(
                "第一步的提示词自动接上了流程输入",
                bool(GenProvider.submits) and GenProvider.submits[0].get("prompt") == "一只拉布拉多",
                str(GenProvider.submits),
            )
            check(
                "接不上的字段不传（用节点默认值）",
                bool(GenProvider.submits) and "style" not in GenProvider.submits[0],
                str(GenProvider.submits),
            )
            check(
                "第二步的提示词也自动接上了流程输入",
                any(r.get("prompt") == "一只拉布拉多" for r in EditProvider.received),
                str(EditProvider.received),
            )
            uploaded = [r["upload"] for r in EditProvider.received if "upload" in r]
            check(
                "第二步的参考图自动接上了上一步的产物字节",
                bool(uploaded) and uploaded[0] == GEN_PNG,
                f"收到 {len(uploaded)} 份上传；全部={EditProvider.received}",
            )

            # ── 显式配置压过自动 ──────────────────────────────────
            GenProvider.submits = []
            EditProvider.received = []
            async with session() as db:
                flow2 = Flow(
                    name="显式优先",
                    input_schema={
                        "fields": [
                            {
                                "name": "input1",
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
                            "template_id": "bind_gen_tpl",
                            "count": 1,
                            # 显式写了「风格」，也声明风格是自动 —— 显式优先
                            "params": {"style": "水彩"},
                            "auto_fields": ["prompt", "style"],
                        },
                    ],
                    created_by="u1",
                )
                db.add(flow2)
                await db.commit()
                await db.refresh(flow2)
                run2 = await submit_flow(db, flow2, {"input1": "一只猫"}, U)

            status2, err2 = await _wait_run(session, run2.id)
            check("第二个流程也跑通了", status2 == "done", f"{status2} {err2}")
            check(
                "显式配置的参数不会被自动覆盖",
                bool(GenProvider.submits) and GenProvider.submits[0].get("style") == "水彩",
                str(GenProvider.submits),
            )
            check(
                "没被显式配置的那个字段仍然自动接上",
                bool(GenProvider.submits) and GenProvider.submits[0].get("prompt") == "一只猫",
                str(GenProvider.submits),
            )
        finally:
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
