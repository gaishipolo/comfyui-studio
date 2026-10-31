# -*- coding: utf-8 -*-
"""Real E2E: upload a configured image, generate, and persist an asset."""

import json
import mimetypes
import os
import string
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from test_env import SERVICE_BASE_URL


BASE = SERVICE_BASE_URL
FIXTURE_ENV = "COMFYUI_STUDIO_E2E_IMAGE_PATH"
TEMPLATE_ENV = "COMFYUI_STUDIO_E2E_TEMPLATE_ID"
ENGINE_ENV = "COMFYUI_STUDIO_E2E_ENGINE"


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
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, json.loads(response.read())


def multipart(path, token, filename, data, content_type):
    boundary = "----e2eBoundary77"
    parts = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        BASE + path,
        data=parts,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())


def configured_inputs():
    raw_path = (os.environ.get(FIXTURE_ENV) or "").strip()
    template_id = (os.environ.get(TEMPLATE_ENV) or "").strip()
    engine = (os.environ.get(ENGINE_ENV) or "comfyui").strip()
    missing = [name for name, value in ((FIXTURE_ENV, raw_path), (TEMPLATE_ENV, template_id)) if not value]
    if missing:
        raise ValueError(f"必须设置环境变量: {', '.join(missing)}")
    if not engine:
        raise ValueError(f"{ENGINE_ENV} 不能为空")
    if len(template_id) != 32 or any(char not in string.hexdigits for char in template_id):
        raise ValueError(f"{TEMPLATE_ENV} 必须是 32 位十六进制模板 ID")

    fixture = Path(raw_path).expanduser().resolve()
    if not fixture.is_file():
        raise ValueError(f"{FIXTURE_ENV} 不是可读文件: {fixture}")
    data = fixture.read_bytes()
    if not data:
        raise ValueError(f"{FIXTURE_ENV} 指向空文件: {fixture}")
    content_type = mimetypes.guess_type(fixture.name)[0] or ""
    if not content_type.startswith("image/"):
        raise ValueError(f"{FIXTURE_ENV} 必须是可识别的图片文件: {fixture}")
    return fixture, data, content_type, template_id, engine


def main() -> int:
    try:
        fixture, image, content_type, template_id, engine = configured_inputs()
    except ValueError as exc:
        print(f"配置错误: {exc}")
        return 2

    token = req(
        "POST",
        "/api/auth/login",
        json_body={"username": "admin", "password": "admin123"},
    )[1]["access_token"]

    code, body = multipart(
        f"/api/upload/image?engine={urllib.parse.quote(engine)}",
        token,
        fixture.name,
        image,
        content_type,
    )
    if code != 200 or not body.get("filename"):
        print(f"上传失败: HTTP {code} {body}")
        return 1
    reference_name = body["filename"]
    print(f"1. 上传参考图 OK -> {reference_name}")

    code, tasks = req(
        "POST",
        "/api/tasks",
        token=token,
        json_body={
            "template_id": template_id,
            "params": {
                "ref_image": reference_name,
                "prompt": "Reference image to generate a cool girl, e2e final check",
                "width": 768,
                "height": 768,
            },
            "batch_count": 1,
        },
    )
    if code not in {200, 201} or not isinstance(tasks, list):
        print(f"提交失败: HTTP {code} {tasks}")
        return 1
    task_id = tasks[0]["id"]
    print(f"2. 提交任务 OK -> {task_id}")

    task = {}
    for _ in range(60):
        code, task = req("GET", f"/api/tasks/{task_id}", token=token)
        if task.get("status") in {"done", "error", "cancelled"}:
            break
        time.sleep(3)

    print(
        f"3. 任务状态: {task.get('status')} progress={task.get('progress')} "
        f"err={task.get('error_msg', '')[:100]!r}"
    )
    if task.get("status") != "done":
        print("生成未成功完成")
        return 1

    outputs = (task.get("result") or {}).get("outputs", [])
    print(f"4. 产出 {len(outputs)} 个文件: {[item['filename'] for item in outputs]}")
    code, assets = req("GET", f"/api/tasks/{task_id}/assets", token=token)
    print(f"5. 素材入库 {len(assets) if isinstance(assets, list) else 0} 个")
    if code != 200 or not isinstance(assets, list) or not assets:
        print("无素材")
        return 1
    for asset in assets:
        print(
            f"   - {asset['file_name']} ({asset['file_size']} bytes) "
            f"meta.ref_image={asset['meta'].get('ref_image')}"
        )
    print("\nE2E 全链路通过：上传参考图 → 参数注入 LoadImage → 生成 → 素材入库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
