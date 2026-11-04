"""LLM 编排通道：需求 → 配置 → 草稿。

**这里测的不是「模型聪不聪明」，而是「模型写错了系统能不能兜住」**：
- 输出带代码围栏 / 前后有解释文字 → 能解析
- 配置引用了不存在的节点 → 把错误回喂，第二次改对
- 改不对 → 有限次后放弃，**不落库**
- 没配模型 → 明确说「部署没配」，而不是让人一直改需求
- 生成成功 → 落成 **draft**，绝不自动 active

跑法：直接 python tests/test_agent_llm.py（自包含，不联网）。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="agent-llm-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 't.db'}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ.setdefault("SECRET_KEY", "agent-llm-test-secret")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "agent-llm-test-pw")
os.environ["LLM_BASE_URL"] = "https://llm.example.com/v1"
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MODEL"] = "test-model"
os.environ["LLM_MAX_ATTEMPTS"] = "3"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.models import Template, User  # noqa: E402
from app.services import agent_llm  # noqa: E402

TOKEN = ""
TEMPLATE_ID = "t1"
# 规格只用于提示词，这里给最小骨架即可 —— 真规格由 agent_spec 生成，
# 它本身已有测试（test_agent_flow）
SPEC = {"nodes": [], "expressions": {}, "rules": []}


def _template() -> Template:
    return Template(
        id=TEMPLATE_ID,
        name="文生图",
        description="测试节点",
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


def _templates() -> dict:
    return {TEMPLATE_ID: _template()}


def _config(template_id: str = TEMPLATE_ID, name: str = "换主体") -> dict:
    return {
        "name": name,
        "description": "把参考图里的主体换掉",
        "category": "image",
        "icon": "",
        "input_schema": {
            "fields": [
                {"name": "prompt", "label": "提示词", "type": "textarea", "required": True}
            ]
        },
        "outputs": [{"name": "image", "from": "s1", "pick": "all"}],
        "steps": [
            {
                "id": "s1",
                "title": "出图",
                "type": "template",
                "template_id": template_id,
                "params": {"prompt": "{{ input.prompt | trim }}"},
            }
        ],
        "note": "一步出图，提示词由运营填",
    }


def _transport(replies: list[str]):
    """按顺序回放的假模型。记录每次请求，供断言自修提示词。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        text = replies.pop(0)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": text}}]},
        )

    return httpx.MockTransport(handler), calls


def _run(requirement: str, replies: list[str], **kwargs):
    transport, calls = _transport(replies)
    result = asyncio.run(
        agent_llm.generate_flow_draft(
            requirement,
            SPEC,
            _templates(),
            {},
            transport=transport,
            **kwargs,
        )
    )
    return result, calls


# ── JSON 提取 ──────────────────────────────────────────────


def test_extract_plain_json() -> None:
    obj = agent_llm.extract_json_object(json.dumps(_config(), ensure_ascii=False))
    assert obj["name"] == "换主体"


def test_extract_fenced_json() -> None:
    """模型最常见的输出：```json 围栏。不该因此判失败。"""
    text = "```json\n" + json.dumps(_config(), ensure_ascii=False) + "\n```"
    assert agent_llm.extract_json_object(text)["steps"], "围栏里的 JSON 没解析出来"


def test_extract_json_with_chatter() -> None:
    """前后加一句「好的，配置如下」也要能解析。"""
    text = "好的，配置如下：\n" + json.dumps(_config(), ensure_ascii=False) + "\n以上。"
    assert agent_llm.extract_json_object(text)["name"] == "换主体"


def test_extract_wrapped_flow() -> None:
    """有的模型会包一层 {"flow": {...}}，顺手剥掉。"""
    text = json.dumps({"flow": _config()}, ensure_ascii=False)
    assert agent_llm.extract_json_object(text)["steps"], "没剥掉 flow 外层"


def test_extract_rejects_non_json() -> None:
    try:
        agent_llm.extract_json_object("抱歉，我无法完成这个请求。")
    except ValueError as exc:
        assert "JSON" in str(exc), str(exc)
    else:
        raise AssertionError("非 JSON 输出应该报错")


# ── 生成主链路 ────────────────────────────────────────────


def test_generate_ok_first_try() -> None:
    result, calls = _run("生成一张图", [json.dumps(_config(), ensure_ascii=False)])
    assert result.ok, result.errors
    assert result.config is not None
    assert len(result.attempts) == 1, result.attempts
    assert result.attempts[0]["stage"] == "ok"
    assert result.layers == [["s1"]], result.layers
    # 系统提示词里要带规格，否则模型只能瞎猜
    assert "编排规格" in calls[0]["messages"][0]["content"]
    assert calls[0]["model"] == "test-model"


def test_generate_repairs_invalid_config() -> None:
    """第一次引用了不存在的节点 → 错误回喂 → 第二次改对。"""
    bad = _config(template_id="不存在的节点")
    result, calls = _run(
        "生成一张图",
        [json.dumps(bad, ensure_ascii=False), json.dumps(_config(), ensure_ascii=False)],
    )
    assert result.ok, result.errors
    assert len(result.attempts) == 2, result.attempts
    assert result.attempts[0]["stage"] == "validate"
    assert any("不存在" in e for e in result.attempts[0]["errors"]), result.attempts[0]
    # 第二次请求里必须真的把校验错误告诉了模型
    repair_text = calls[1]["messages"][-1]["content"]
    assert "没通过校验" in repair_text, repair_text
    assert "不存在" in repair_text, repair_text


def test_generate_repairs_schema_error() -> None:
    """缺 name → pydantic 先拦住，也要回喂自修。"""
    bad = _config()
    bad.pop("name")
    result, _calls = _run(
        "生成一张图",
        [json.dumps(bad, ensure_ascii=False), json.dumps(_config(), ensure_ascii=False)],
    )
    assert result.ok, result.errors
    assert result.attempts[0]["stage"] == "schema", result.attempts


def test_generate_repairs_bad_json() -> None:
    """第一次吐了段废话 → 第二次给 JSON。"""
    result, _calls = _run(
        "生成一张图", ["我不太确定你要什么。", json.dumps(_config(), ensure_ascii=False)]
    )
    assert result.ok, result.errors
    assert result.attempts[0]["stage"] == "json", result.attempts


def test_generate_gives_up_without_persisting() -> None:
    """一直改不对 → 有限次后返回失败（调用方据此不落库）。"""
    bad = json.dumps(_config(template_id="不存在"), ensure_ascii=False)
    result, calls = _run("生成一张图", [bad, bad], max_attempts=2)
    assert not result.ok
    assert len(result.attempts) == 2, result.attempts
    assert len(calls) == 2, "不应该无限重试"
    assert result.errors, "失败要带原因"


def test_generate_empty_steps_rejected() -> None:
    """空步骤是合法 JSON 但不是一条流程 —— 语义层要拦住。"""
    empty = _config()
    empty["steps"] = []
    result, _calls = _run(
        "生成一张图", [json.dumps(empty, ensure_ascii=False), json.dumps(_config(), ensure_ascii=False)]
    )
    assert result.ok, result.errors
    assert result.attempts[0]["stage"] == "semantic", result.attempts


def test_generate_unconfigured_raises() -> None:
    """没配模型要说清楚是部署问题，不要静默失败。"""
    base, model = agent_llm.settings.LLM_BASE_URL, agent_llm.settings.LLM_MODEL
    agent_llm.settings.LLM_BASE_URL = ""
    try:
        try:
            asyncio.run(
                agent_llm.generate_flow_draft("生成一张图", SPEC, _templates(), {})
            )
        except agent_llm.AgentLLMError as exc:
            assert "配置" in str(exc), str(exc)
        else:
            raise AssertionError("未配置模型时应该抛 AgentLLMError")
    finally:
        agent_llm.settings.LLM_BASE_URL = base
        agent_llm.settings.LLM_MODEL = model


# ── API：生成后只能落草稿 ──────────────────────────────────


def setup() -> None:
    global TOKEN

    async def _build():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(
                User(
                    username="llm_tester",
                    password_hash=hash_password("LlmTest123!"),
                    role="admin",
                    display_name="LLM 测试",
                    is_active=True,
                )
            )
            db.add(_template())
            await db.commit()

    asyncio.run(_build())
    client = TestClient(_app())
    resp = client.post(
        "/api/auth/login", json={"username": "llm_tester", "password": "LlmTest123!"}
    )
    assert resp.status_code == 200, resp.text
    TOKEN = resp.json()["access_token"]


def _app():
    from app.main import app

    return app


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_status_reports_unconfigured() -> None:
    base, model = agent_llm.settings.LLM_BASE_URL, agent_llm.settings.LLM_MODEL
    agent_llm.settings.LLM_BASE_URL = ""
    try:
        body = TestClient(_app()).get("/api/flows/agent/status", headers=_headers()).json()
        assert body == {"configured": False, "model": ""}, body
    finally:
        agent_llm.settings.LLM_BASE_URL = base
        agent_llm.settings.LLM_MODEL = model


def test_generate_without_model_returns_message() -> None:
    """未配置时接口要回 ok=false + 人话，而不是 500。"""
    base = agent_llm.settings.LLM_BASE_URL
    agent_llm.settings.LLM_BASE_URL = ""
    try:
        resp = TestClient(_app()).post(
            "/api/flows/agent/generate",
            headers=_headers(),
            json={"requirement": "生成一张图"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False, body
        assert "配置" in body["message"], body
    finally:
        agent_llm.settings.LLM_BASE_URL = base


def _fake_generate(*_args, **_kwargs):
    """替身模型：直接给一份合法配置，用来验证落库规则。"""

    async def _inner():
        return agent_llm.GenerationResult(
            ok=True,
            config=_config(),
            note="一步出图",
            layers=[["s1"]],
            attempts=[{"attempt": 1, "stage": "ok", "errors": []}],
            model="fake-model",
            message="ok",
        )

    return _inner()


def test_generate_saves_as_draft_and_suffixes_name() -> None:
    """生成 → draft；连续两次同名 → 自动加后缀，不覆盖上一条。"""
    from unittest.mock import patch

    import app.api.flows as flows_api

    client = TestClient(_app())
    with patch.object(flows_api.agent_llm, "generate_flow_draft", _fake_generate):
        first = client.post(
            "/api/flows/agent/generate",
            headers=_headers(),
            json={"requirement": "把参考图的主体换掉"},
        )
        second = client.post(
            "/api/flows/agent/generate",
            headers=_headers(),
            json={"requirement": "把参考图的主体换掉"},
        )

    assert first.status_code == 200, first.text
    b1, b2 = first.json(), second.json()
    assert b1["ok"] and b2["ok"], (b1, b2)
    assert b1["name"] == "换主体", b1
    assert b2["name"] == "换主体 · 2", b2

    got = client.get(f"/api/flows/{b1['flow_id']}", headers=_headers()).json()
    assert got["status"] == "draft", "生成结果不能自动上线"
    assert "fake-model" in got["agent_note"], got["agent_note"]
    assert "换主体" in got["name"]


def test_generate_failure_does_not_persist() -> None:
    """模型没生成出合法配置 → 一条草稿都不留。"""
    from unittest.mock import patch

    import app.api.flows as flows_api

    async def _fail(*_args, **_kwargs):
        return agent_llm.GenerationResult(
            ok=False,
            errors=["第 1 步引用的节点不存在：x"],
            attempts=[{"attempt": 1, "stage": "validate", "errors": ["节点不存在"]}],
            model="fake-model",
            message="试了 3 次仍没生成出合法配置",
        )

    client = TestClient(_app())
    before = client.get("/api/flows", headers=_headers()).json()
    with patch.object(flows_api.agent_llm, "generate_flow_draft", _fail):
        body = client.post(
            "/api/flows/agent/generate",
            headers=_headers(),
            json={"requirement": "随便来一条"},
        ).json()
    after = client.get("/api/flows", headers=_headers()).json()

    assert body["ok"] is False, body
    assert body["errors"], body
    assert len(after) == len(before), "失败不应该落库"


def main() -> int:
    setup()
    tests = [
        test_extract_plain_json,
        test_extract_fenced_json,
        test_extract_json_with_chatter,
        test_extract_wrapped_flow,
        test_extract_rejects_non_json,
        test_generate_ok_first_try,
        test_generate_repairs_invalid_config,
        test_generate_repairs_schema_error,
        test_generate_repairs_bad_json,
        test_generate_gives_up_without_persisting,
        test_generate_empty_steps_rejected,
        test_generate_unconfigured_raises,
        test_status_reports_unconfigured,
        test_generate_without_model_returns_message,
        test_generate_saves_as_draft_and_suffixes_name,
        test_generate_failure_does_not_persist,
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
