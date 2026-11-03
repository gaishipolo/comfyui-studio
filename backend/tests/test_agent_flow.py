"""Agent 编排接口：spec → draft → trial → approve。

**这条链是 Agent 落地的骨架**，四步缺一不可：
1. spec  —— Agent 知道能拼什么、怎么写合法（**规格从代码生成**，不手写）
2. draft —— 提交配置，走**和人编排同一道校验门**，落成 draft
3. trial —— 试跑，真的出图但**不进正式素材库**
4. approve —— 人审批后才 active，Agent 不能自己上线

**最要紧的两条断言**：
- Agent 的配置和人手工编的配置**走同一个校验门**（不给特权）
- 试跑产物**真的不进正式素材库**（默认列表里看不到）

跑法：直接 python tests/test_agent_flow.py（自包含，不起外部服务）。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="agent-flow-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 't.db'}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ.setdefault("SECRET_KEY", "agent-test-secret")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "agent-test-pw")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.models import Asset, Flow, Template, User  # noqa: E402

TOKEN = ""
FLOW_ID = ""
TEMPLATE_ID = ""


def setup() -> None:
    """建库、造用户与节点、登录拿 token。"""
    global TOKEN, TEMPLATE_ID

    async def _build():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            user = User(
                username="agent_tester",
                password_hash=hash_password("AgentTest123!"),
                role="admin",
                display_name="Agent 测试",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            tpl = Template(
                name="测试出图节点",
                description="Agent 测试用",
                category="image",
                engine="fake",
                capability="text_to_image",
                input_schema={
                    "fields": [
                        {"name": "prompt", "label": "提示词", "type": "textarea", "required": True},
                        {"name": "image", "label": "参考图", "type": "file_upload"},
                    ]
                },
                output_config={"nodes": []},
                is_active=True,
            )
            db.add(tpl)
            await db.commit()
            return user.id, tpl.id

    user_id, TEMPLATE_ID = asyncio.run(_build())

    client = TestClient(_app())
    resp = client.post(
        "/api/auth/login",
        json={"username": "agent_tester", "password": "AgentTest123!"},
    )
    assert resp.status_code == 200, resp.text
    TOKEN = resp.json()["access_token"]


def _app():
    """每次调用都拿当前 app（环境变量在 import 前设好了）。"""
    from app.main import app

    return app


def _client() -> TestClient:
    return TestClient(_app())


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_spec_lists_nodes_and_functions() -> None:
    """规格里要有可用节点、可用函数、表达式语法。"""
    c = _client()
    resp = c.get("/api/flows/agent/spec", headers=_headers())
    assert resp.status_code == 200, resp.text
    spec = resp.json()

    node_ids = [n["id"] for n in spec["nodes"]]
    assert TEMPLATE_ID in node_ids, "规格里没有测试节点"

    node = next(n for n in spec["nodes"] if n["id"] == TEMPLATE_ID)
    field_names = [f["name"] for f in node["fields"]]
    assert "prompt" in field_names, field_names
    # 文件字段要标出来 —— Agent 才知道谁能接上游产物
    image_field = next(f for f in node["fields"] if f["name"] == "image")
    assert image_field["accepts_output"] is True, image_field

    fns = [f["name"] for f in spec["expressions"]["functions"]]
    assert "trim" in fns and "truncate" in fns, fns
    assert spec["expressions"]["syntax"], "规格里没有表达式语法"
    assert spec["rules"], "规格里没有规则说明"


def test_spec_is_not_hardcoded() -> None:
    """规格是从代码生成的 —— 节点清单必须等于库里 active 的节点数。"""
    c = _client()
    spec = c.get("/api/flows/agent/spec", headers=_headers()).json()

    async def _count():
        from sqlalchemy import select

        from app.models.models import Template as Tpl

        engine = create_async_engine(os.environ["DATABASE_URL"])
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            rows = (
                await db.execute(select(Tpl).where(Tpl.is_active.is_(True)))
            ).scalars().all()
            await engine.dispose()
            return len(rows)

    assert len(spec["nodes"]) == asyncio.run(_count()), (
        "规格里的节点数与库里 active 的节点数不一致 —— 规格可能是手写的"
    )


def test_draft_valid_config_becomes_draft() -> None:
    """合法配置 → 落成 draft（不是 active）。"""
    c = _client()
    resp = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={
            "name": "Agent 生成的流程",
            "description": "测试",
            "input_schema": {
                "fields": [
                    {"name": "prompt", "label": "提示词", "type": "textarea"},
                    {"name": "style", "label": "画风", "type": "text"},
                ]
            },
            "outputs": [{"name": "final", "from": "s1", "pick": "all"}],
            "steps": [
                {
                    "id": "s1",
                    "title": "出图",
                    "type": "template",
                    "template_id": TEMPLATE_ID,
                    "params": {"prompt": "{{ input.prompt | trim }}"},
                }
            ],
            "note": "这是 Agent 写的说明",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    assert body["flow_id"], body

    # 落库的状态必须是 draft
    got = c.get(f"/api/flows/{body['flow_id']}", headers=_headers()).json()
    assert got["status"] == "draft", got["status"]
    assert got["agent_note"] == "这是 Agent 写的说明", got


def test_draft_invalid_config_rejected() -> None:
    """非法配置 → 不落库，带错误回来。"""
    c = _client()
    resp = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={
            "name": "非法配置",
            "steps": [
                {
                    "id": "s1",
                    "type": "template",
                    "template_id": "不存在的节点",
                    "params": {"prompt": "x"},
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False, body
    assert body["errors"], "应该带错误信息"
    assert not body["flow_id"], "校验不过就不该落库"


def test_draft_same_name_rejected() -> None:
    """同名流程不覆盖 —— Agent 反复提交不能把别人在用的流程冲掉。"""
    c = _client()
    payload = {
        "name": "重名测试",
        "steps": [
            {
                "id": "s1",
                "type": "template",
                "template_id": TEMPLATE_ID,
                "params": {"prompt": "x"},
            }
        ],
    }
    first = c.post("/api/flows/agent/draft", headers=_headers(), json=payload).json()
    assert first["ok"] is True, first
    second = c.post("/api/flows/agent/draft", headers=_headers(), json=payload).json()
    assert second["ok"] is False, second
    assert "已存在" in " ".join(second["errors"]), second


def test_draft_same_validation_gate_as_manual() -> None:
    """Agent 走的是**同一道校验门** —— 不给特权。

    验证方式：拿一份人编排器会判错的配置，Agent 提交也必须被拒。
    """
    c = _client()
    # 人编排器的 validate 与 Agent 的 draft 都拒这个配置
    bad_steps = [
        {
            "id": "s1",
            "type": "template",
            "template_id": TEMPLATE_ID,
            "params": {"prompt": "{{ input.不存在的字段 }}"},
        }
    ]
    manual = c.post(
        "/api/flows/validate",
        headers=_headers(),
        json={"steps": bad_steps, "input_schema": {"fields": []}, "outputs": []},
    ).json()
    agent = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={"name": "同门验证", "steps": bad_steps},
    ).json()
    assert manual["ok"] is False, manual
    assert agent["ok"] is False, agent
    # 两边都要报出同一个问题
    assert any("不存在的字段" in e for e in manual["errors"]), manual
    assert any("不存在的字段" in e for e in agent["errors"]), agent


def test_approve_activates_draft() -> None:
    """审批 → active。"""
    c = _client()
    flow_id = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={
            "name": "待审批流程",
            "steps": [
                {
                    "id": "s1",
                    "type": "template",
                    "template_id": TEMPLATE_ID,
                    "params": {"prompt": "x"},
                }
            ],
        },
    ).json()["flow_id"]

    resp = c.post(
        f"/api/flows/{flow_id}/approve",
        headers=_headers(),
        json={"approve": True, "note": "看着没问题"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.json()

    got = c.get(f"/api/flows/{flow_id}", headers=_headers()).json()
    assert got["status"] == "active", got
    assert got["review_note"] == "看着没问题", got
    assert got["reviewed_by"], "审批人没记上"
    assert got["reviewed_at"], "审批时间没记上"


def test_reject_archives_draft() -> None:
    """驳回 → archived（留档但不可用）。"""
    c = _client()
    flow_id = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={
            "name": "待驳回流程",
            "steps": [
                {
                    "id": "s1",
                    "type": "template",
                    "template_id": TEMPLATE_ID,
                    "params": {"prompt": "x"},
                }
            ],
        },
    ).json()["flow_id"]

    resp = c.post(
        f"/api/flows/{flow_id}/approve",
        headers=_headers(),
        json={"approve": False, "note": "节点选错了"},
    )
    assert resp.status_code == 200, resp.text
    got = c.get(f"/api/flows/{flow_id}", headers=_headers()).json()
    assert got["status"] == "archived", got
    assert got["review_note"] == "节点选错了", got


def test_approve_non_draft_rejected() -> None:
    """已经是 active 的流程不需要再批 —— 不能重复批。"""
    c = _client()
    flow_id = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={
            "name": "重复审批流程",
            "steps": [
                {
                    "id": "s1",
                    "type": "template",
                    "template_id": TEMPLATE_ID,
                    "params": {"prompt": "x"},
                }
            ],
        },
    ).json()["flow_id"]
    c.post(
        f"/api/flows/{flow_id}/approve",
        headers=_headers(),
        json={"approve": True, "note": ""},
    )
    again = c.post(
        f"/api/flows/{flow_id}/approve",
        headers=_headers(),
        json={"approve": True, "note": "再批一次"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["ok"] is False, again.json()


def test_trial_marks_run_as_trial() -> None:
    """试跑标记落在运行记录上。"""
    c = _client()
    flow_id = c.post(
        "/api/flows/agent/draft",
        headers=_headers(),
        json={
            "name": "试跑流程",
            "steps": [
                {
                    "id": "s1",
                    "type": "template",
                    "template_id": TEMPLATE_ID,
                    "params": {"prompt": "x"},
                }
            ],
        },
    ).json()["flow_id"]

    resp = c.post(f"/api/flows/{flow_id}/trial", headers=_headers(), json={})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["id"]

    async def _check():
        from sqlalchemy import select

        from app.models.models import FlowRun

        engine = create_async_engine(os.environ["DATABASE_URL"])
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            row = await db.get(FlowRun, run_id)
            await engine.dispose()
            return row

    row = asyncio.run(_check())
    assert row is not None, "运行记录不见了"
    assert row.is_trial is True, "试跑标记没落上"


def test_trial_assets_hidden_from_library() -> None:
    """试跑产物**不进正式素材库** —— 默认列表里看不到。"""
    c = _client()

    # 直接造一条试跑素材与一条正式素材
    async def _seed():
        from sqlalchemy import select

        engine = create_async_engine(os.environ["DATABASE_URL"])
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            user = (
                await db.execute(select(User).where(User.username == "agent_tester"))
            ).scalars().first()
            trial = Asset(
                owner_id=user.id,
                file_path="trial/x.png",
                file_name="trial.png",
                file_size=10,
                mime_type="image/png",
                tags=["image"],
                meta={},
                is_trial=True,
            )
            formal = Asset(
                owner_id=user.id,
                file_path="formal/x.png",
                file_name="formal.png",
                file_size=10,
                mime_type="image/png",
                tags=["image"],
                meta={},
                is_trial=False,
            )
            db.add(trial)
            db.add(formal)
            await db.commit()
            await engine.dispose()

    asyncio.run(_seed())

    default_list = c.get("/api/assets", headers=_headers()).json()
    names = [a["file_name"] for a in default_list]
    assert "formal.png" in names, "正式素材应该能看到"
    assert "trial.png" not in names, f"试跑产物不该出现在默认素材库：{names}"

    only_trial = c.get("/api/assets?trial=only", headers=_headers()).json()
    only_names = [a["file_name"] for a in only_trial]
    assert "trial.png" in only_names, f"trial=only 应该能看到试跑产物：{only_names}"
    assert "formal.png" not in only_names, only_names

    include_all = c.get("/api/assets?trial=include", headers=_headers()).json()
    all_names = [a["file_name"] for a in include_all]
    assert "trial.png" in all_names and "formal.png" in all_names, all_names


def main() -> int:
    setup()
    tests = [
        test_spec_lists_nodes_and_functions,
        test_spec_is_not_hardcoded,
        test_draft_valid_config_becomes_draft,
        test_draft_invalid_config_rejected,
        test_draft_same_name_rejected,
        test_draft_same_validation_gate_as_manual,
        test_approve_activates_draft,
        test_reject_archives_draft,
        test_approve_non_draft_rejected,
        test_trial_marks_run_as_trial,
        test_trial_assets_hidden_from_library,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
