"""流程的并行分层执行。

**这个测试的判据设计得很硬**：两个 provider 共用一个「闸门」——只有当
**两个** provider 都进入 submit 才一起放行。

- 顺序执行时：第一个 provider 进入 submit 后一直等，第二个永远不会被
  提交 → 闸门不放行 → 第一个超时失败 → 流程失败。测试红。
- 分层并行时：两个 submit 几乎同时进入，闸门立刻放行 → 都成功 → 流程绿。

所以它不是「跑得更快所以通过」，而是「不并行就根本跑不完」。
"""
import asyncio
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import Template, User  # noqa: E402


def make_png(rgb=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), rgb).save(buf, "PNG")
    return buf.getvalue()


class GateMixin:
    """所有闸门 provider 共享一份状态。"""

    gate_state: dict = {}


def make_gate_provider(name: str, label: str):
    """造一个 provider：进 submit 后等「所有闸门 provider 都进来」才放行。"""

    class GateProvider(GateMixin):
        def __init__(self, config: dict | None = None):
            self.config = config or {}

        def build_payload(self, capability: str, params: dict) -> dict:
            return dict(params)

        async def submit(self, ctx: dict, params: dict) -> str:
            state = GateMixin.gate_state
            state["started"] = state.get("started", 0) + 1
            state.setdefault("entered", []).append(name)
            if state["started"] >= state["expect"]:
                state["gate"].set()
            # 只等一小会儿：串行执行时这里必然超时（测试要红，不能挂死）
            try:
                await asyncio.wait_for(state["gate"].wait(), timeout=6)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"闸门没放开：只有 {state['started']}/{state['expect']} 个 provider 进入 submit"
                    "（串行执行会卡在这里）"
                ) from exc
            state["released"] = state.get("released", 0) + 1
            return f"{name}-task"

        async def get_status(self, task_id: str):
            from app.engines.base import GenerationResult, TaskStatus

            return GenerationResult(
                status=TaskStatus.DONE,
                progress=100.0,
                outputs=[{"filename": f"{name}.png", "subfolder": "", "type": "output"}],
            )

        async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
            return make_png()

        async def upload_input(self, data: bytes, filename: str) -> str:
            return f"u:{filename}"

        async def cancel(self, provider_task_id: str) -> bool:
            return True

        async def health_check(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    GateProvider.name = name
    GateProvider.label = label
    GateProvider.capabilities = {}
    GateProvider.config_schema = []
    GateProvider.max_concurrency = 4
    return GateProvider


async def scenario() -> None:
    from app.engines import factory
    from app.main import app as fastapi_app
    from app.services import flow_service, task_service

    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))

    with tempfile.TemporaryDirectory(prefix="flow-parallel-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        original_storage = settings.STORAGE_DIR
        settings.STORAGE_DIR = root / "storage"

        async def override_get_db():
            async with sessions() as session:
                yield session

        fastapi_app.dependency_overrides[get_db] = override_get_db
        original_session = task_service.async_session
        original_flow_session = flow_service.async_session
        original_timeout = flow_service._STEP_TIMEOUT_SECONDS
        task_service.async_session = sessions
        flow_service.async_session = sessions
        # 测试不能等一小时
        flow_service._STEP_TIMEOUT_SECONDS = 20

        ProviderA = make_gate_provider("parallel_a", "闸门A")
        ProviderB = make_gate_provider("parallel_b", "闸门B")
        factory.register_provider(ProviderA.name, ProviderA)
        factory.register_provider(ProviderB.name, ProviderB)
        GateMixin.gate_state = {
            "started": 0,
            "expect": 2,
            "gate": asyncio.Event(),
            "entered": [],
            "released": 0,
        }

        try:
            async with sessions() as db:
                db.add(
                    User(
                        id="u1",
                        username="parallel",
                        password_hash=hash_password("pass1234"),
                        display_name="并行探针",
                        role="admin",
                    )
                )
                for pid, pname, provider in (
                    ("tpl-a", "闸门节点甲", ProviderA),
                    ("tpl-b", "闸门节点乙", ProviderB),
                ):
                    db.add(
                        Template(
                            id=pid,
                            name=pname,
                            engine=provider.name,
                            category="image",
                            input_schema={
                                "fields": [
                                    {"name": "prompt", "label": "提示词", "type": "textarea", "required": True}
                                ]
                            },
                            param_mapping={},
                            output_config={"nodes": []},
                            is_active=True,
                        )
                    )
                await db.commit()

            import httpx

            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "parallel", "password": "pass1234"},
                )
                headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

                # 两个步骤互不依赖 → 同一层
                created = await client.post(
                    "/api/flows",
                    json={
                        "name": "并行验证",
                        "input_schema": {"fields": [
                            {"name": "prompt", "label": "提示词", "type": "textarea", "required": True}
                        ]},
                        "outputs": [],
                        "steps": [
                            {"id": "a", "type": "template", "template_id": "tpl-a",
                             "params": {"prompt": "{{ input.prompt }}"}},
                            {"id": "b", "type": "template", "template_id": "tpl-b",
                             "params": {"prompt": "{{ input.prompt }}"}},
                        ],
                    },
                    headers=headers,
                )
                check("建流程成功", created.status_code == 200, created.text[:200])
                flow_id = created.json()["id"]

                # 校验给出的分层：两个无依赖 → 同一层
                validated = await client.post(
                    "/api/flows/validate",
                    json={"steps": created.json()["steps"], "input_schema": created.json()["input_schema"]},
                    headers=headers,
                )
                layers = validated.json()["layers"]
                check("两个无依赖步骤在同一层", layers == [["a", "b"]], str(layers))

                started_at = time.monotonic()
                run = await client.post(
                    f"/api/flows/{flow_id}/run",
                    json={"prompt": "两只狗"},
                    headers=headers,
                )
                check("提交运行成功", run.status_code == 200, run.text[:200])
                run_id = run.json()["id"]

                final = {}
                for _ in range(120):
                    await asyncio.sleep(0.25)
                    probe = await client.get(f"/api/flows/runs/{run_id}", headers=headers)
                    final = probe.json()
                    if final.get("status") in ("done", "error", "cancelled"):
                        break
                elapsed = time.monotonic() - started_at

                state = GateMixin.gate_state
                check(
                    "流程跑完且成功",
                    final.get("status") == "done",
                    f"status={final.get('status')} err={final.get('error_msg')}",
                )
                check(
                    "两个 provider 都进了 submit",
                    state["started"] == 2,
                    f"started={state['started']} entered={state['entered']}",
                )
                check(
                    "两个 provider 都被闸门放行",
                    state["released"] == 2,
                    f"released={state['released']}",
                )
                check(
                    "两步都记在运行里",
                    len(final.get("steps") or []) == 2,
                    str(final.get("steps")),
                )
                check(
                    "运行记录里带层号",
                    all("layer" in s for s in (final.get("steps") or [])),
                    str(final.get("steps")),
                )
                check("整体耗时合理", elapsed < 15, f"{elapsed:.1f}s")

        finally:
            task_service.async_session = original_session
            flow_service.async_session = original_flow_session
            flow_service._STEP_TIMEOUT_SECONDS = original_timeout
            settings.STORAGE_DIR = original_storage
            factory.unregister_provider("parallel_a")
            factory.unregister_provider("parallel_b")
            fastapi_app.dependency_overrides.pop(get_db, None)
            await engine.dispose()

    failures = [c for c in checks if not c[1]]
    for label, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if not ok else ""))
    print(f"\nRESULT: {len(checks) - len(failures)} passed / {len(failures)} failed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(scenario())
