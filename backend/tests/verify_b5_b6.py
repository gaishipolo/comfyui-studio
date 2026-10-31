# -*- coding: utf-8 -*-
"""B5/B6/B11 verification with real ComfyUI and deterministic cleanup."""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from test_env import SERVICE_BASE_URL


BASE = SERVICE_BASE_URL
TERMINAL_STATUSES = {"done", "error", "cancelled"}


def req(method, path, token=None, json_body=None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def main() -> int:
    ok = True

    def check(label, condition, detail=""):
        nonlocal ok
        ok &= bool(condition)
        print(f"{'OK  ' if condition else 'FAIL'} {label}" + (f" {detail}" if detail else ""))

    code, login = req(
        "POST",
        "/api/auth/login",
        json_body={"username": "admin", "password": "admin123"},
    )
    if code != 200 or not isinstance(login, dict) or not login.get("access_token"):
        print(f"登录失败: HTTP {code} {login}")
        return 1
    token = login["access_token"]

    b5_template_id = None
    created_task_ids: list[str] = []
    try:
        b5_name = f"验证用-B5-generic-{uuid.uuid4().hex[:8]}"
        code, template = req(
            "POST",
            "/api/templates",
            token=token,
            json_body={
                "name": b5_name,
                "category": "image",
                "engine": "generic_api",
                "workflow_file": "",
                "input_schema": {
                    "fields": [{
                        "name": "prompt",
                        "label": "提示词",
                        "type": "textarea",
                        "required": True,
                    }],
                },
                "param_mapping": {},
            },
        )
        check("B5 创建 generic_api 模板", code == 200, f"HTTP {code}")
        if code != 200:
            return 1
        b5_template_id = template["id"]

        code, tasks = req(
            "POST",
            "/api/tasks",
            token=token,
            json_body={
                "template_id": b5_template_id,
                "params": {"prompt": "test"},
                "batch_count": 1,
            },
        )
        check("B5 提交任务", code in {200, 201} and isinstance(tasks, list), f"HTTP {code}")
        if code not in {200, 201} or not isinstance(tasks, list):
            return 1
        b5_task_id = tasks[0]["id"]
        created_task_ids.append(b5_task_id)
        time.sleep(4)
        code, task = req("GET", f"/api/tasks/{b5_task_id}", token=token)
        error = task.get("error_msg", "") if isinstance(task, dict) else ""
        b5_ok = (
            code == 200
            and task.get("status") == "error"
            and "workflow" not in error.lower()
            and "IsADirectory" not in error
        )
        check(
            "B5 generic_api 无 workflow 可执行",
            b5_ok,
            f"status={task.get('status')} err={error[:80]!r}",
        )

        code, templates = req("GET", "/api/templates", token=token)
        flux = next(
            (item for item in templates if "Flux2" in item.get("name", "")),
            None,
        ) if code == 200 and isinstance(templates, list) else None
        check("存在 Flux2 模板", flux is not None)
        if flux is None:
            return 1

        code, tasks = req(
            "POST",
            "/api/tasks",
            token=token,
            json_body={
                "template_id": flux["id"],
                "params": {
                    "prompt": "a cool girl, test progress",
                    "width": 768,
                    "height": 768,
                },
                "batch_count": 1,
            },
        )
        check("B6 提交真实 ComfyUI 任务", code in {200, 201} and isinstance(tasks, list))
        if code not in {200, 201} or not isinstance(tasks, list):
            return 1
        b6_task_id = tasks[0]["id"]
        created_task_ids.append(b6_task_id)
        created_by = tasks[0].get("created_by", "")
        check("B11 提交任务写入 created_by", bool(created_by), repr(created_by))

        progress_seen = set()
        status_seen = set()
        task = {}
        for _ in range(45):
            code, task = req("GET", f"/api/tasks/{b6_task_id}", token=token)
            if code != 200 or not isinstance(task, dict):
                break
            progress_seen.add(task.get("progress"))
            status_seen.add(task.get("status"))
            if task.get("status") in TERMINAL_STATUSES:
                break
            time.sleep(2)

        final_status = task.get("status")
        reached_terminal = final_status in TERMINAL_STATUSES
        middle_progress = {value for value in progress_seen if value is not None and 0 < value < 100}
        progress_ok = reached_terminal and (bool(middle_progress) or final_status == "done")
        printable_statuses = sorted(value for value in status_seen if value is not None)
        printable_progress = sorted(value for value in progress_seen if value is not None)
        print(f"      状态轨迹: {printable_statuses}  进度轨迹: {printable_progress}")
        check("B6 轮询在时限内到达终态", reached_terminal, f"status={final_status}")
        check("B6 进度回写", progress_ok, f"中途进度={sorted(middle_progress) or '无'}")

        code, assets = (
            req("GET", f"/api/tasks/{b6_task_id}/assets", token=token)
            if final_status == "done"
            else (200, [])
        )
        print(
            f"      任务最终状态: {final_status} "
            f"素材数: {len(assets) if code == 200 and isinstance(assets, list) else 0}"
        )
    finally:
        for task_id in reversed(created_task_ids):
            code, task = req("GET", f"/api/tasks/{task_id}", token=token)
            status = task.get("status") if code == 200 and isinstance(task, dict) else None
            if status not in TERMINAL_STATUSES:
                cancel_code, _ = req("POST", f"/api/tasks/{task_id}/cancel", token=token)
                check(f"取消未终态任务 {task_id[:8]}", cancel_code == 200, f"HTTP {cancel_code}")
            delete_code, body = req("DELETE", f"/api/tasks/{task_id}", token=token)
            check(
                f"清理任务 {task_id[:8]}",
                delete_code in {200, 404},
                f"HTTP {delete_code} {body}",
            )
        if b5_template_id:
            code, body = req("DELETE", f"/api/templates/{b5_template_id}", token=token)
            check("清理 B5 模板", code in {200, 404}, f"HTTP {code} {body}")

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
