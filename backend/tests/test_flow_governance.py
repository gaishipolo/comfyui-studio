"""流程的服务端治理 —— 界面之外那道门。

编排器保存前会校验，但接口不能假设「调用方一定校验过」：直接调 API、
脚本、Agent 都可能绕过界面。这里守四件事：

1. 非法配置进不了库（创建 / 修改都拦）
2. 草稿不能靠 PUT 把自己改成 active（审批留痕不能是摆设）
3. 被别的流程当子流程调用时不能删（否则留下改不回来的断链）
4. 改坏配置时原流程不受影响（校验先于写入）

跑法：python tests/test_flow_governance.py（自包含，不联网）。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="flow-gov-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 't.db'}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ.setdefault("SECRET_KEY", "flow-gov-secret")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "flow-gov-pw")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.models import Template, User  # noqa: E402

TOKEN = ""


def _app():
    from app.main import app

    return app


def _client() -> TestClient:
    return TestClient(_app())


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _step(template_id: str = "gov_tpl") -> dict:
    return {
        "id": "s1",
        "title": "出图",
        "type": "template",
        "template_id": template_id,
        "params": {"prompt": "{{ input.prompt }}"},
    }


def _inputs() -> dict:
    return {
        "fields": [
            {"name": "prompt", "label": "提示词", "type": "textarea", "required": True}
        ]
    }


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
                    username="gov_tester",
                    password_hash=hash_password("GovTest123!"),
                    role="admin",
                    display_name="治理测试",
                    is_active=True,
                )
            )
            db.add(
                Template(
                    id="gov_tpl",
                    name="治理测试节点",
                    category="image",
                    engine="fake",
                    capability="text_to_image",
                    input_schema=_inputs(),
                    output_config={"nodes": []},
                    is_active=True,
                )
            )
            await db.commit()

    asyncio.run(_build())
    resp = _client().post(
        "/api/auth/login",
        json={"username": "gov_tester", "password": "GovTest123!"},
    )
    assert resp.status_code == 200, resp.text
    TOKEN = resp.json()["access_token"]


def test_create_rejects_invalid_config() -> None:
    """引用了不存在的节点 → 400，不落库。"""
    c = _client()
    before = len(c.get("/api/flows", headers=_headers()).json())
    resp = c.post(
        "/api/flows",
        headers=_headers(),
        json={
            "name": "坏配置",
            "steps": [_step("no_such_template")],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "节点不存在" in resp.json()["detail"], resp.json()
    after = c.get("/api/flows", headers=_headers()).json()
    assert len(after) == before, "校验失败不该落库"


def test_create_accepts_empty_flow() -> None:
    """新建空白流程仍然可以 —— 编排是从空开始的。"""
    resp = _client().post(
        "/api/flows",
        headers=_headers(),
        json={"name": "空白流程", "steps": [], "input_schema": {"fields": []}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps"] == []


def test_update_rejects_invalid_config_without_writing() -> None:
    """改坏配置 → 400，原流程保持不变（校验先于写入）。"""
    c = _client()
    created = c.post(
        "/api/flows",
        headers=_headers(),
        json={"name": "修改保护", "steps": [_step()], "input_schema": _inputs()},
    ).json()
    resp = c.put(
        f"/api/flows/{created['id']}",
        headers=_headers(),
        json={"steps": [_step("ghost")]},
    )
    assert resp.status_code == 400, resp.text
    got = c.get(f"/api/flows/{created['id']}", headers=_headers()).json()
    assert got["steps"][0]["template_id"] == "gov_tpl", got["steps"]


def test_draft_cannot_self_activate() -> None:
    """草稿不能用 PUT 把自己改成 active —— 必须走审批。"""
    c = _client()
    created = c.post(
        "/api/flows",
        headers=_headers(),
        json={
            "name": "待审批草稿",
            "status": "draft",
            "steps": [_step()],
            "input_schema": _inputs(),
        },
    ).json()
    assert created["status"] == "draft", created

    resp = c.put(
        f"/api/flows/{created['id']}",
        headers=_headers(),
        json={"status": "active", "steps": [_step()]},
    )
    assert resp.status_code == 200, resp.text
    got = c.get(f"/api/flows/{created['id']}", headers=_headers()).json()
    assert got["status"] == "draft", f"草稿被 PUT 直接上线了：{got['status']}"


def test_delete_referenced_subflow_blocked() -> None:
    """被父流程引用时不能删；先删父流程，再删子流程。"""
    c = _client()
    child = c.post(
        "/api/flows",
        headers=_headers(),
        json={"name": "子流程-被引用", "steps": [_step()], "input_schema": _inputs()},
    ).json()
    parent = c.post(
        "/api/flows",
        headers=_headers(),
        json={
            "name": "父流程-引用子流程",
            "steps": [
                {"id": "sub", "type": "flow", "flow_id": child["id"], "params": {}}
            ],
            "input_schema": _inputs(),
        },
    ).json()

    blocked = c.delete(f"/api/flows/{child['id']}", headers=_headers())
    assert blocked.status_code == 409, blocked.text
    assert "正在调用" in blocked.json()["detail"], blocked.json()

    assert c.delete(f"/api/flows/{parent['id']}", headers=_headers()).status_code == 200
    assert c.delete(f"/api/flows/{child['id']}", headers=_headers()).status_code == 200


def main() -> int:
    setup()
    tests = [
        test_create_rejects_invalid_config,
        test_create_accepts_empty_flow,
        test_update_rejects_invalid_config_without_writing,
        test_draft_cannot_self_activate,
        test_delete_referenced_subflow_blocked,
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
