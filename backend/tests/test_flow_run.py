"""流程编排端到端：跑通「拉布拉多拉拖拉机 → 换成边牧」这条链。

这条链刻意选了两个**不同引擎**的节点（ComfyUI 出图 → 供应商 API 图生图），
因为步骤间传产物要跨引擎：上游产物是我们自己存储里的文件，下游引擎不认，
必须读字节、送进下游引擎、换回引擎侧名字。这是最容易出错的地方。

假 Provider 记录下第二次收到的字节，断言它等于第一步产出的 PNG ——
如果产物没传过去、或者传的是原图，这里都会红。
"""
import asyncio
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import Template, User  # noqa: E402


def make_png(rgb: tuple[int, int, int] = (200, 180, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), rgb).save(buf, "PNG")
    return buf.getvalue()


# 第一步产出的图。与 make_png() 的默认值一致 —— 断言「下游收到的是不是这张图」
# 就靠比对字节。
GEN_PNG = make_png()


class GenProvider:
    """第一步的节点：文生图，产出一张 PNG。"""

    name = "flowtest_gen"
    label = "流程测试-出图"
    capabilities = {}
    config_schema = []
    max_concurrency = 4

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        return "gen-task-1"

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
        return f"uploaded:{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class EditProvider:
    """第二步的节点：图生图。记录收到的参考图字节 —— 这是验证的关键。"""

    name = "flowtest_edit"
    label = "流程测试-改图"
    capabilities = {}
    config_schema = []
    max_concurrency = 4
    received: list[bytes] = []
    last_params: dict = {}

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build_payload(self, capability: str, params: dict) -> dict:
        type(self).last_params = dict(params)
        return dict(params)

    async def submit(self, ctx: dict, params: dict) -> str:
        type(self).last_params = dict(params)
        return "edit-task-1"

    async def get_status(self, task_id: str):
        from app.engines.base import GenerationResult, TaskStatus

        return GenerationResult(
            status=TaskStatus.DONE,
            progress=100.0,
            outputs=[{"filename": "edit.png", "subfolder": "", "type": "output"}],
        )

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return make_png((20, 20, 20))

    async def upload_input(self, data: bytes, filename: str) -> str:
        type(self).received.append(data)
        return f"wbinput:edited/{filename}"

    async def cancel(self, provider_task_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


async def scenario() -> None:
    from app.engines import factory
    from app.main import app as fastapi_app
    from app.services import flow_service, task_service

    print("[diag] imports done", flush=True)
    with tempfile.TemporaryDirectory(prefix="flow-run-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 存储指向临时目录：filestore 层照常工作，但不碰真实 storage
        original_storage = settings.STORAGE_DIR
        settings.STORAGE_DIR = root / "storage"

        async def override_get_db():
            async with sessions() as session:
                yield session

        fastapi_app.dependency_overrides[get_db] = override_get_db
        # 执行器与任务服务都用这个 session 工厂（它们自己开短会话）
        original_session = task_service.async_session
        original_flow_session = flow_service.async_session
        task_service.async_session = sessions
        flow_service.async_session = sessions

        factory.register_provider(GenProvider.name, GenProvider)
        factory.register_provider(EditProvider.name, EditProvider)
        EditProvider.received = []
        EditProvider.last_params = {}
        print("[diag] providers registered", flush=True)

        checks: list[tuple[str, bool, str]] = []

        def check(label: str, cond: bool, detail: str = "") -> None:
            checks.append((label, bool(cond), detail))
            print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if not cond else ""), flush=True)

        try:
            async with sessions() as db:
                db.add(
                    User(
                        id="u1",
                        username="operator",
                        password_hash=hash_password("pass1234"),
                        display_name="运营",
                        role="admin",
                    )
                )
                db.add(
                    Template(
                        id="tpl-gen",
                        name="流程测试-出图",
                        description="",
                        category="image",
                        engine=GenProvider.name,
                        input_schema={
                            "fields": [
                                {"name": "prompt", "label": "提示词", "type": "textarea", "required": True},
                                {"name": "width", "label": "宽", "type": "select", "required": False,
                                 "options": [512, 1024], "default": 1024},
                            ]
                        },
                        param_mapping={},
                        output_config={"nodes": []},
                        is_active=True,
                    )
                )
                db.add(
                    Template(
                        id="tpl-edit",
                        name="流程测试-改图",
                        description="",
                        category="image",
                        engine=EditProvider.name,
                        input_schema={
                            "fields": [
                                {"name": "prompt", "label": "提示词", "type": "textarea", "required": True},
                                {"name": "image", "label": "参考图", "type": "file_upload",
                                 "required": True, "accept": "image"},
                            ]
                        },
                        param_mapping={},
                        output_config={"nodes": []},
                        is_active=True,
                    )
                )
                await db.commit()

            transport = httpx.ASGITransport(app=fastapi_app)
            print("[diag] client start", flush=True)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                print("[diag] posting login", flush=True)
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "operator", "password": "pass1234"},
                )
                check("登录成功", login.status_code == 200, login.text[:120])
                headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

                # ── 能力清单 ──
                caps = await client.get("/api/flows/capabilities", headers=headers)
                cap_ids = [c["id"] for c in caps.json()] if caps.status_code == 200 else []
                check("能力清单能列出节点", "tpl-gen" in cap_ids and "tpl-edit" in cap_ids, str(cap_ids))
                gen_cap = next((c for c in caps.json() if c["id"] == "tpl-gen"), None)
                check(
                    "能力清单带字段定义",
                    bool(gen_cap) and any(f["name"] == "prompt" for f in gen_cap["fields"]),
                    str(gen_cap),
                )

                # ── 建流程：拉布拉多换边牧 ──
                flow_doc = {
                    "name": "拉布拉多换边牧",
                    "description": "文生图后按要求改图",
                    "input_schema": {
                        "fields": [
                            {"name": "prompt", "label": "初始画面", "type": "textarea", "required": True},
                            {"name": "edit_prompt", "label": "修改要求", "type": "textarea", "required": True},
                        ]
                    },
                    "outputs": [{"name": "final_image", "from": "edit", "pick": "all"}],
                    "steps": [
                        {
                            "id": "gen",
                            "type": "template",
                            "template_id": "tpl-gen",
                            "params": {"prompt": "{{ input.prompt }}", "width": "1024"},
                        },
                        {
                            "id": "edit",
                            "type": "template",
                            "template_id": "tpl-edit",
                            "params": {
                                "prompt": "{{ input.edit_prompt }}",
                                "image": "{{ steps.gen.outputs }}",
                            },
                        },
                    ],
                }
                created = await client.post("/api/flows", json=flow_doc, headers=headers)
                check("建流程成功", created.status_code == 200, f"{created.status_code} {created.text[:200]}")
                flow_id = created.json().get("id") if created.status_code == 200 else ""

                # ── 校验 ──
                bad = await client.post(
                    "/api/flows/validate",
                    json={
                        "steps": [
                            {"id": "a", "template_id": "tpl-gen", "params": {"nope": "{{ input.prompt }}"}},
                            {"id": "b", "template_id": "tpl-edit", "params": {"image": "{{ steps.zzz.outputs }}"}},
                        ],
                        "input_schema": {"fields": [{"name": "prompt", "label": "p", "type": "textarea"}]},
                    },
                    headers=headers,
                )
                body = bad.json() if bad.status_code == 200 else {}
                check("校验挡下不存在的字段", body.get("ok") is False and any(
                    "不存在" in e for e in body.get("errors", [])
                ), str(body.get("errors")))
                check("校验挡下不存在的上游步骤", any(
                    "zzz" in e for e in body.get("errors", [])
                ), str(body.get("errors")))
                check(
                    "校验给出依赖分层（互不依赖 → 同层）",
                    body.get("layers") == [["a", "b"]],
                    str(body.get("layers")),
                )

                # ── 跑 ──
                run = await client.post(
                    f"/api/flows/{flow_id}/run",
                    json={
                        "prompt": "拉布拉多拉拖拉机",
                        "edit_prompt": "将拉布拉多换成边牧",
                    },
                    headers=headers,
                )
                check("提交运行成功", run.status_code == 200, f"{run.status_code} {run.text[:200]}")
                run_id = run.json().get("id") if run.status_code == 200 else ""

                final = {}
                for _ in range(120):
                    await asyncio.sleep(0.25)
                    probe = await client.get(f"/api/flows/runs/{run_id}", headers=headers)
                    if probe.status_code != 200:
                        break
                    final = probe.json()
                    if final.get("status") in ("done", "error", "cancelled"):
                        break

                check("流程跑完且成功", final.get("status") == "done",
                      f"status={final.get('status')} err={final.get('error_msg')}")
                check("两步都记在运行里", len(final.get("steps") or []) == 2, str(final.get("steps")))

                # ── 关键断言：第二步收到的是第一步的产物 ──
                check("下游引擎收到了参考图", len(EditProvider.received) == 1,
                      f"received={len(EditProvider.received)}")
                if EditProvider.received:
                    check(
                        "收到的字节就是第一步的产出",
                        EditProvider.received[0] == GEN_PNG,
                        f"len={len(EditProvider.received[0])} expect={len(GEN_PNG)}",
                    )
                check(
                    "下游拿到的是引擎侧名字不是路径",
                    str(EditProvider.last_params.get("image", "")).startswith("wbinput:"),
                    str(EditProvider.last_params.get("image")),
                )
                check(
                    "两个提示词各归各位",
                    EditProvider.last_params.get("prompt") == "将拉布拉多换成边牧",
                    str(EditProvider.last_params.get("prompt")),
                )

        finally:
            task_service.async_session = original_session
            flow_service.async_session = original_flow_session
            settings.STORAGE_DIR = original_storage
            factory.unregister_provider(GenProvider.name)
            factory.unregister_provider(EditProvider.name)
            fastapi_app.dependency_overrides.pop(get_db, None)
            # SQLite 连接池还攥着 db 文件，不释放的话临时目录清不掉（Windows 上
            # 会报 PermissionError: 另一个程序正在使用此文件）
            await engine.dispose()

    failures = [c for c in checks if not c[1]]
    print(f"\nRESULT: {len(checks) - len(failures)} passed / {len(failures)} failed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    import httpx  # noqa: E402

    asyncio.run(scenario())
