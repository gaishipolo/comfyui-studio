"""端到端验证：带管道/条件的表达式在真流程里跑通。

**不是单测能覆盖的**：单测证明表达式语言本身对；这里证明的是
「流程执行器 → 表达式求值 → 节点参数」这条链真的接上了。

用假 Provider（不花钱、不依赖 ComfyUI）：出文本 + 吃文本参数。
断言的是**下游实际收到的参数值**（不是表达式字符串）。
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 必须在 import app 之前设好环境
_TMP = Path(tempfile.mkdtemp(prefix="flowexpr-e2e-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'e2e.db'}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ.setdefault("SECRET_KEY", "e2e-secret")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "e2e-admin-pw")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus  # noqa: E402
from app.engines.factory import register_provider  # noqa: E402
from app.models.models import Flow, FlowRun, Template, User  # noqa: E402
from app.services.flow_service import submit_flow  # noqa: E402

RECEIVED: list[dict] = []


class FakeTextProvider(GenerationProvider):
    """吃文本参数的假节点。"""

    name = "fake_text"
    label = "假文本节点"

    async def submit(self, ctx: dict, params: dict) -> str:
        RECEIVED.append(dict(params))
        return f"fake-text-{len(RECEIVED)}"

    async def get_status(self, task_id: str) -> GenerationResult:
        return GenerationResult(success=True, status=TaskStatus.DONE, progress=100)

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return b""

    async def health_check(self) -> bool:
        return True


async def main() -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    register_provider("fake_text", FakeTextProvider)

    async with Session() as db:
        user = User(
            username="e2e", password_hash="x", role="admin", display_name="E2E"
        )
        db.add(user)
        await db.flush()

        tpl = Template(
            name="假文本节点",
            description="测试用",
            category="text",
            engine="fake_text",
            capability="text_generate",
            input_schema={
                "fields": [
                    {"name": "text", "label": "文字", "type": "text"},
                ]
            },
            output_config={"nodes": []},
            is_active=True,
        )
        db.add(tpl)
        await db.flush()

        flow = Flow(
            name="表达式验证流程",
            description="带管道与条件",
            category="text",
            input_schema={
                "fields": [
                    {"name": "raw", "label": "原始提示词", "type": "textarea"},
                    {"name": "style", "label": "画风", "type": "text"},
                    {"name": "clean", "label": "写实提示词", "type": "textarea"},
                    {"name": "art", "label": "油画提示词", "type": "textarea"},
                ]
            },
            outputs=[],
            steps=[
                {
                    "id": "s1",
                    "title": "截断提示词",
                    "type": "template",
                    "template_id": tpl.id,
                    "params": {"text": "{{ input.raw | trim | truncate(20) }}"},
                },
                {
                    "id": "s2",
                    "title": "按风格取词",
                    "type": "template",
                    "template_id": tpl.id,
                    "params": {
                        "text": '{{ input.style == "写实" ? input.clean : input.art }}'
                    },
                },
            ],
            status="active",
            created_by=user.id,
        )
        db.add(flow)
        await db.commit()
        flow_id = flow.id
        user_id = user.id

    params = {
        "raw": "  一只拉布拉多拉拖拉机在田野里奔跑，很长很长的描述  ",
        "style": "写实",
        "clean": "写实风格的光影",
        "art": "油画风格的笔触",
    }
    async with Session() as db:
        flow_row = await db.get(Flow, flow_id)
        user_row = await db.get(User, user_id)
        run = await submit_flow(db, flow_row, params, user_row)
        run_id = run.id

    # 等它跑完
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    final = None
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        async with Session() as db:
            row = await db.get(FlowRun, run_id)
            if row and row.status in ("done", "error", "cancelled"):
                final = row
                break
    if final is None:
        print("[FAIL] 流程超时")
        await engine.dispose()
        shutil.rmtree(_TMP, ignore_errors=True)
        return 1
    if final.status != "done":
        print(f"[FAIL] 流程状态 {final.status}：{final.error_msg}")
        await engine.dispose()
        shutil.rmtree(_TMP, ignore_errors=True)
        return 1

    ok = True
    if len(RECEIVED) != 2:
        print(f"[FAIL] 期望收到 2 次调用，实际 {len(RECEIVED)}")
        ok = False
    else:
        # 两步之间没有依赖 → **并行跑，顺序不保证**。
        # 所以按内容匹配，不按调用顺序断言。
        texts = [r.get("text") for r in RECEIVED]
        print(f"  收到 text = {texts!r}")
        # 管道：去空格 + 截断 20 字
        expected_first = "一只拉布拉多拉拖拉机在田野里奔跑，很长很长的描述".strip()[:20]
        if expected_first not in texts:
            print(f"[FAIL] 管道结果不对：{texts!r} 里没有 {expected_first!r}")
            ok = False
        # 条件：写实 → clean
        if "写实风格的光影" not in texts:
            print(f"[FAIL] 条件结果不对：{texts!r} 里没有「写实风格的光影」")
            ok = False
        if "油画风格的笔触" in texts:
            print(f"[FAIL] 条件分支走错了：不该出现「油画风格的笔触」")
            ok = False

    print("RESULT:", "PASS" if ok else "FAIL")
    await engine.dispose()
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
