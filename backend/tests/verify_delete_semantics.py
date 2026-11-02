"""Verify that template disable and delete remain distinct operations."""

import live_guard
live_guard.require_live_ok()

import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_env import API_BASE_URL  # noqa: E402


BASE = API_BASE_URL


def req(path, method="GET", data=None, token=None):
    request = urllib.request.Request(BASE + path, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(request, body) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def seed_task(template_id: str, owner_id: str) -> str:
    from app.database import async_session
    from app.models.models import Task

    task_id = uuid.uuid4().hex
    async with async_session() as database:
        database.add(Task(
            id=task_id,
            template_id=template_id,
            created_by=owner_id,
            batch_id=uuid.uuid4().hex,
            status="done",
            progress=100.0,
            params={},
            result={},
            error_msg="",
        ))
        await database.commit()
    return task_id


def main() -> int:
    ok = True

    def report(label, condition, detail=""):
        nonlocal ok
        ok &= bool(condition)
        print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    status, login = req(
        "/auth/login",
        "POST",
        {"username": "admin", "password": "admin123"},
    )
    if status != 200 or not login.get("access_token"):
        print(f"登录失败：HTTP {status} {login}")
        return 1
    token = login["access_token"]
    owner_id = login["user"]["id"]

    cleanup_templates: list[str] = []
    cleanup_tasks: list[str] = []
    try:
        reusable_name = f"删除语义-{uuid.uuid4().hex[:8]}"
        print("=== 1. 创建 → 删除 → 名称应可复用 ===")
        status, template = req(
            "/templates",
            "POST",
            {"name": reusable_name, "is_active": True},
            token=token,
        )
        report("创建模板", status == 200, f"status={status}")
        if status != 200:
            return 1
        first_id = template["id"]
        cleanup_templates.append(first_id)

        status, _ = req(f"/templates/{first_id}", "DELETE", token=token)
        report("删除模板", status == 200, f"status={status}")
        if status == 200:
            cleanup_templates.remove(first_id)

        status, templates = req("/templates?include_inactive=true", token=token)
        names = [item["name"] for item in templates] if status == 200 else []
        report("删除后从列表消失", status == 200 and reusable_name not in names)

        status, replacement = req(
            "/templates",
            "POST",
            {"name": reusable_name, "is_active": True},
            token=token,
        )
        report("名称可重新使用", status == 200, f"status={status}")
        if status == 200:
            cleanup_templates.append(replacement["id"])

        print("\n=== 2. 停用 ≠ 删除：停用后名称仍被占用 ===")
        disabled_name = f"停用语义-{uuid.uuid4().hex[:8]}"
        status, disabled = req(
            "/templates",
            "POST",
            {"name": disabled_name, "is_active": True},
            token=token,
        )
        report("创建模板 M", status == 200)
        if status != 200:
            return 1
        disabled_id = disabled["id"]
        cleanup_templates.append(disabled_id)

        status, _ = req(
            f"/templates/{disabled_id}",
            "PUT",
            {"is_active": False},
            token=token,
        )
        report("停用模板 M", status == 200)
        status, templates = req("/templates?include_inactive=true", token=token)
        report(
            "停用后仍出现在管理列表",
            status == 200 and disabled_name in [item["name"] for item in templates],
        )
        status, _ = req("/templates", "POST", {"name": disabled_name}, token=token)
        report("停用后名称仍被占用(应 400)", status == 400, f"status={status}")

        print("\n=== 3. 有任务记录的模板不允许删除 ===")
        used_name = f"使用中语义-{uuid.uuid4().hex[:8]}"
        status, used = req(
            "/templates",
            "POST",
            {"name": used_name, "is_active": True},
            token=token,
        )
        report("创建使用中模板", status == 200, f"status={status}")
        if status != 200:
            return 1
        used_id = used["id"]
        cleanup_templates.append(used_id)
        used_task_id = asyncio.run(seed_task(used_id, owner_id))
        cleanup_tasks.append(used_task_id)

        status, body = req(f"/templates/{used_id}", "DELETE", token=token)
        report(
            "带任务的模板删除被拒(应 400)",
            status == 400,
            f"status={status} detail={body.get('detail', '')[:40]}",
        )
        status, _ = req(f"/templates/{used_id}", token=token)
        report("被拒后模板仍在", status == 200)
    finally:
        print("\n=== 4. 清理 ===")
        for task_id in cleanup_tasks:
            status, _ = req(f"/tasks/{task_id}", "DELETE", token=token)
            report(f"清理任务 {task_id[:8]}", status in {200, 404}, f"status={status}")
        for template_id in cleanup_templates:
            status, _ = req(f"/templates/{template_id}", "DELETE", token=token)
            report(
                f"清理模板 {template_id[:8]}",
                status in {200, 404},
                f"status={status}",
            )

    print(f"\n结果: {'[OK] 删除/停用语义正确' if ok else '[FAIL] 语义有问题'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
