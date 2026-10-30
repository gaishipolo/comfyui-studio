# -*- coding: utf-8 -*-
"""全链路 e2e：上传参考图 → 提交生成 → 产出素材（验证 F1 上传链路与模板映射）"""
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8000"


def req(method, path, token=None, json_body=None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    resp = urllib.request.urlopen(r, timeout=20)
    return resp.status, json.loads(resp.read())


def multipart(path, token, filename, data, content_type):
    b = "----e2eBoundary77"
    parts = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{b}--\r\n".encode()
    r = urllib.request.Request(
        BASE + path, data=parts,
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={b}"},
        method="POST",
    )
    resp = urllib.request.urlopen(r, timeout=30)
    return resp.status, json.loads(resp.read())


token = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "admin123"})[1]["access_token"]

# 1. 上传参考图（模拟前端 image_upload 字段）
img = Path(r"E:\xx\ai\comfyui-studio\images.jfif").read_bytes()
code, body = multipart("/api/upload/image?engine=comfyui", token, "e2e_face.jfif", img, "image/jpeg")
ref_name = body["filename"]
print(f"1. 上传参考图 OK -> {ref_name}")

# 2. 提交生成任务（ref_image 使用上传返回的文件名）
code, tasks = req("POST", "/api/tasks", token=token, json_body={
    "template_id": "92c71a45b7a6495f92ec14a37bbc492e",
    "params": {
        "ref_image": ref_name,
        "prompt": "Reference image to generate a cool girl, e2e final check",
        "width": 768,
        "height": 768,
    },
    "batch_count": 1,
})
task_id = tasks[0]["id"]
print(f"2. 提交任务 OK -> {task_id}")

# 3. 轮询直到完成
for _ in range(60):
    code, task = req("GET", f"/api/tasks/{task_id}", token=token)
    if task["status"] in ("done", "error"):
        break
    time.sleep(3)

print(f"3. 任务状态: {task['status']} progress={task['progress']} err={task.get('error_msg', '')[:100]!r}")
assert task["status"] == "done", "生成失败"

# 4. 验证参数确实注入了 LoadImage 节点
code, task = req("GET", f"/api/tasks/{task_id}", token=token)
outputs = task["result"].get("outputs", [])
print(f"4. 产出 {len(outputs)} 个文件: {[o['filename'] for o in outputs]}")

code, assets = req("GET", f"/api/tasks/{task_id}/assets", token=token)
print(f"5. 素材入库 {len(assets)} 个")
for a in assets:
    print(f"   - {a['file_name']} ({a['file_size']} bytes) meta.ref_image={a['meta'].get('ref_image')}")
assert len(assets) >= 1, "无素材"
print("\nE2E 全链路通过：上传参考图 → 参数注入 LoadImage → 生成 → 素材入库")
