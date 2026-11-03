"""T5 回归：整应用 ASGI 冒烟 —— 登录 → 建模板 → 提交任务 → 列素材。

此前没有任何进程内测试挂起整个 FastAPI app：路由前缀拼错、查询参数名写错
只能靠 live 脚本发现。这里用真实 app.main:app（含中间件与全部路由）跑一遍
最小业务闭环，DB 与存储指向临时目录，不连引擎。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import User  # noqa: E402


async def scenario() -> None:
    from app.main import app as fastapi_app
    from app.api import tasks as tasks_api
    from app.services import task_service

    with tempfile.TemporaryDirectory(prefix="app-smoke-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}"
        )
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async def override_get_db():
            async with sessions() as session:
                yield session

        fastapi_app.dependency_overrides[get_db] = override_get_db

        # 提交任务会触发后台执行协程，这里把它挡在“排队前”，
        # 免得测试去连真实引擎（tasks_api 在导入时已绑定 spawn_task，
        # 只改 task_service.spawn_task 不生效）
        spawned: list[str] = []

        async def fake_spawn(task_id: str, resume: bool = False) -> bool:
            spawned.append(task_id)
            return True

        original_spawn = tasks_api.spawn_task
        original_session = task_service.async_session
        tasks_api.spawn_task = fake_spawn
        task_service.async_session = sessions

        try:
            async with sessions() as db:
                db.add(User(
                    id="u-admin",
                    username="admin",
                    password_hash=hash_password("admin123"),
                    display_name="管理员",
                    role="admin",
                ))
                await db.commit()

            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                # ① 登录
                response = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "admin123"},
                )
                assert response.status_code == 200, response.text
                token = response.json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                # ② 建模板
                response = await client.post(
                    "/api/templates",
                    headers=headers,
                    json={
                        "name": "冒烟模板",
                        "description": "",
                        "category": "image",
                        "icon": "",
                        "input_schema": {
                            "fields": [
                                {
                                    "name": "prompt",
                                    "label": "提示词",
                                    "type": "textarea",
                                    "required": True,
                                },
                                {
                                    "name": "seed",
                                    "label": "种子",
                                    "type": "random_number",
                                    "min": 0,
                                    "max": 1000,
                                },
                            ]
                        },
                        "engine": "comfyui",
                        "capability": "",
                        "workflow_file": "",
                        "param_mapping": {},
                        "output_config": {"nodes": []},
                        "is_active": True,
                        "sort_order": 0,
                    },
                )
                assert response.status_code == 200, response.text
                template = response.json()
                assert template["name"] == "冒烟模板", template
                template_id = template["id"]

                # ③ 模板列表能查到
                response = await client.get("/api/templates", headers=headers)
                assert response.status_code == 200, response.text
                assert any(item["id"] == template_id for item in response.json())

                # ④ 提交任务
                response = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={
                        "template_id": template_id,
                        "params": {"prompt": "一只猫"},
                        "batch_count": 1,
                    },
                )
                assert response.status_code == 200, response.text
                tasks = response.json()
                assert len(tasks) == 1, tasks
                task_id = tasks[0]["id"]
                assert tasks[0]["status"] == "pending", tasks[0]
                assert spawned == [task_id], spawned

                # ⑤ 任务列表能查到，且带创建人
                response = await client.get("/api/tasks", headers=headers)
                assert response.status_code == 200, response.text
                listed = response.json()
                assert any(item["id"] == task_id for item in listed), listed

                # ⑥ 单查任务
                response = await client.get(f"/api/tasks/{task_id}", headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["id"] == task_id

                # ⑦ 任务素材列表（此时还没有产物，应为空列表而不是 404）
                response = await client.get(
                    f"/api/tasks/{task_id}/assets", headers=headers
                )
                assert response.status_code == 200, response.text
                assert response.json() == [], response.text

                # ⑧ 素材库列表
                response = await client.get("/api/assets", headers=headers)
                assert response.status_code == 200, response.text
                assert isinstance(response.json(), list)

                # ⑨ 无 token 的请求被拒
                response = await client.get("/api/tasks")
                assert response.status_code == 401, response.status_code

                # ⑩ 未知 /api 路径返回 JSON 404（不被 SPA 兜底吞成 HTML）
                response = await client.get("/api/does-not-exist", headers=headers)
                assert response.status_code == 404, response.status_code
                assert response.headers["content-type"].startswith("application/json"), (
                    response.headers.get("content-type")
                )

                # ⑪ 随机数字段填 -1：提交时逐任务展开成真实种子。
                # 批量 3 个必须各不同，否则「随机」等于复制；落库的也不能
                # 还是 -1，否则任务详情看不到实际用了什么、事后也复现不了。
                response = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={
                        "template_id": template_id,
                        "params": {"prompt": "一只猫", "seed": -1},
                        "batch_count": 3,
                    },
                )
                assert response.status_code == 200, response.text
                batch = response.json()
                assert len(batch) == 3, batch
                seeds = [item["params"]["seed"] for item in batch]
                assert all(isinstance(s, int) and 0 <= s <= 1000 for s in seeds), seeds
                assert len(set(seeds)) == 3, f"批量任务复用了同一个种子: {seeds}"

                response = await client.get(f"/api/tasks/{batch[0]['id']}", headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["params"]["seed"] == seeds[0], response.text
        finally:
            tasks_api.spawn_task = original_spawn
            task_service.async_session = original_session
            fastapi_app.dependency_overrides = {}
            await engine.dispose()


def main() -> int:
    try:
        asyncio.run(scenario())
    except BaseException as exc:
        print(f"[FAIL] test_app_asgi_smoke: {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 1 failed")
        return 1
    print("[PASS] test_app_asgi_smoke")
    print("\nRESULT: 1 passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
