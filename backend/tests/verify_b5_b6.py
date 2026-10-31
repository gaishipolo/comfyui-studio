# -*- coding: utf-8 -*-
"""B5/B6/B11 verification with real ComfyUI and deterministic cleanup."""

import json
import io
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.filestore import asset_exists
from app.thumbs import thumb_key
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


def req_bytes(method, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return (
                response.status,
                response.read(),
                {key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.read(),
            {key.lower(): value for key, value in exc.headers.items()},
        )


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
    generated_assets: dict[str, list[dict]] = {}
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

        prompt_uuid = str(uuid.uuid4())
        code, tasks = req(
            "POST",
            "/api/tasks",
            token=token,
            json_body={
                "template_id": flux["id"],
                "params": {
                    "prompt": f"Task 7 isolated regression image {prompt_uuid}",
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
        for _ in range(90):
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
        progress_ok = final_status == "done" and (bool(middle_progress) or 100 in progress_seen)
        printable_statuses = sorted(value for value in status_seen if value is not None)
        printable_progress = sorted(value for value in progress_seen if value is not None)
        print(f"      状态轨迹: {printable_statuses}  进度轨迹: {printable_progress}")
        check("B6 轮询在时限内到达终态", reached_terminal, f"status={final_status}")
        check("B6 真实任务必须完成为 done", final_status == "done", f"status={final_status}")
        check(
            "B6 UUID 提示词持久化",
            prompt_uuid in str((task.get("params") or {}).get("prompt", "")),
            prompt_uuid,
        )
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
        check(
            "B6 done 任务至少产生 1 个素材",
            code == 200 and isinstance(assets, list) and len(assets) >= 1,
            f"HTTP {code}",
        )
        if code == 200 and isinstance(assets, list) and assets:
            generated_assets[b6_task_id] = assets
            asset = assets[0]
            encoded_name = urllib.parse.quote(asset["file_name"])
            media_path = f"/api/tasks/{b6_task_id}/assets/{encoded_name}"
            source_code, source_bytes, source_headers = req_bytes(
                "GET", media_path, token=token
            )
            source_size = (0, 0)
            source_format = ""
            try:
                with Image.open(io.BytesIO(source_bytes)) as source_image:
                    source_size = source_image.size
                    source_format = source_image.format or ""
            except Exception:
                pass
            check(
                "B6 原图鉴权下载成功",
                source_code == 200
                and bool(source_bytes)
                and source_headers.get("content-type", "").startswith("image/")
                and bool(source_format),
                f"HTTP {source_code} format={source_format} size={source_size} bytes={len(source_bytes)}",
            )

            thumb_code, thumb_bytes, thumb_headers = req_bytes(
                "GET", media_path + "/thumb", token=token
            )
            thumb_size = (0, 0)
            thumb_format = ""
            try:
                with Image.open(io.BytesIO(thumb_bytes)) as thumb_image:
                    thumb_size = thumb_image.size
                    thumb_format = thumb_image.format or ""
            except Exception:
                pass
            persistent_key = thumb_key(b6_task_id, asset["file_name"])
            smaller_when_meaningful = (
                max(source_size or (0, 0)) <= 400 or len(thumb_bytes) < len(source_bytes)
            )
            check(
                "B6 持久 JPEG 缩略图不超过 400 边长且合理变小",
                thumb_code == 200
                and thumb_format == "JPEG"
                and thumb_headers.get("content-type", "").startswith("image/jpeg")
                and max(thumb_size or (0, 0)) <= 400
                and smaller_when_meaningful
                and asset_exists(persistent_key),
                (
                    f"HTTP {thumb_code} size={thumb_size} bytes={len(thumb_bytes)} "
                    f"source_bytes={len(source_bytes)} key={persistent_key}"
                ),
            )
            print(
                f"      真实生成 ID: task={b6_task_id} "
                f"provider={task.get('provider_task_id', '')} prompt_uuid={prompt_uuid}"
            )
    finally:
        for task_id in reversed(created_task_ids):
            code, task = req("GET", f"/api/tasks/{task_id}", token=token)
            status = task.get("status") if code == 200 and isinstance(task, dict) else None
            if status not in TERMINAL_STATUSES:
                cancel_code, _ = req("POST", f"/api/tasks/{task_id}/cancel", token=token)
                check(f"取消未终态任务 {task_id[:8]}", cancel_code == 200, f"HTTP {cancel_code}")
            delete_code, body = 409, {}
            for _ in range(20):
                delete_code, body = req("DELETE", f"/api/tasks/{task_id}", token=token)
                if delete_code != 409:
                    break
                time.sleep(0.25)
            check(
                f"清理任务 {task_id[:8]}",
                delete_code in {200, 404},
                f"HTTP {delete_code} {body}",
            )
            after_code, _ = req("GET", f"/api/tasks/{task_id}", token=token)
            check(f"任务 API 已清理 {task_id[:8]}", after_code == 404, f"HTTP {after_code}")
            for asset in generated_assets.get(task_id, []):
                encoded_name = urllib.parse.quote(asset["file_name"])
                media_path = f"/api/tasks/{task_id}/assets/{encoded_name}"
                source_code, _source_body, _ = req_bytes("GET", media_path, token=token)
                thumb_code, _thumb_body, _ = req_bytes("GET", media_path + "/thumb", token=token)
                check(
                    f"素材 API 已清理 {asset['file_name']}",
                    source_code == 404 and thumb_code == 404,
                    f"source={source_code} thumb={thumb_code}",
                )
                check(
                    f"素材文件已清理 {asset['file_name']}",
                    not asset_exists(asset["file_path"])
                    and not asset_exists(thumb_key(task_id, asset["file_name"])),
                    asset["file_path"],
                )
        if b5_template_id:
            code, body = req("DELETE", f"/api/templates/{b5_template_id}", token=token)
            check("清理 B5 模板", code in {200, 404}, f"HTTP {code} {body}")

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
