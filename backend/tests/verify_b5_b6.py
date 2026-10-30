# -*- coding: utf-8 -*-
"""B5/B6/B11 验证 — generic_api 无 workflow、进度回写、created_by"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

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
    try:
        resp = urllib.request.urlopen(r, timeout=15)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


token = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "admin123"})[1]["access_token"]

# ── B5: generic_api 模板不配 workflow 也能执行（错误应是连接问题而非 workflow 缺失）──
# 模板名带 uuid：name 有唯一约束，固定名会导致重跑 400（B29 的教训）
B5_NAME = f"验证用-B5-generic-{uuid.uuid4().hex[:8]}"
code, tpl = req("POST", "/api/templates", token=token, json_body={
    "name": B5_NAME,
    "category": "image",
    "engine": "generic_api",
    "workflow_file": "",
    "input_schema": {"fields": [{"name": "prompt", "label": "提示词", "type": "textarea", "required": True}]},
    "param_mapping": {},
})
assert code == 200, f"create template failed: {code} {tpl}"
code, tasks = req("POST", "/api/tasks", token=token,
                  json_body={"template_id": tpl["id"], "params": {"prompt": "test"}, "batch_count": 1})
task_id = tasks[0]["id"]
time.sleep(4)
code, task = req("GET", f"/api/tasks/{task_id}", token=token)
err = task.get("error_msg", "")
b5_ok = task["status"] == "error" and "workflow" not in err.lower() and "IsADirectory" not in err
print(f"{'OK  ' if b5_ok else 'FAIL'} B5 generic_api 无 workflow 可执行 status={task['status']} err={err[:80]!r}")

# ── B6 + B11: 提交真实 ComfyUI 任务，观察进度回写 + created_by ──
code, tpls = req("GET", "/api/templates", token=token)
flux = next((t for t in tpls if "Flux2" in t["name"]), None)
assert flux, "找不到 Flux2 模板"
code, tasks = req("POST", "/api/tasks", token=token, json_body={
    "template_id": flux["id"],
    "params": {"prompt": "a cool girl, test progress", "width": 768, "height": 768},
    "batch_count": 1,
})
task_id = tasks[0]["id"]
cb = tasks[0].get("created_by", "<missing>")
b11_ok = bool(cb)
print(f"{'OK  ' if b11_ok else 'FAIL'} B11 提交任务写入 created_by = {cb!r}")

progress_seen = set()
status_seen = set()
for _ in range(45):  # 最多 90 秒
    code, task = req("GET", f"/api/tasks/{task_id}", token=token)
    progress_seen.add(task.get("progress"))
    status_seen.add(task.get("status"))
    if task.get("status") in ("done", "error"):
        break
    time.sleep(2)

print(f"      状态轨迹: {sorted(status_seen)}  进度轨迹: {sorted(progress_seen)}")
mid = {p for p in progress_seen if 0 < p < 100}
b6_ok = bool(mid) or "done" in status_seen  # 至少出现过中途进度，或最终完成（含 100）
print(f"{'OK  ' if b6_ok else 'FAIL'} B6 进度回写（中途进度值: {sorted(mid) or '无'}）")

code, assets = req("GET", f"/api/tasks/{task_id}/assets", token=token) if task.get("status") == "done" else (200, [])
print(f"      任务最终状态: {task.get('status')} 素材数: {len(assets) if isinstance(assets, list) else 0}")

# ── 清理说明 ──────────────────────────────────────────────────────
# 本测试会留下 1 个模板（B5_NAME）+ 它的任务/素材。
# 模板名用 uuid 保证可重跑；模板/任务残留由 tests/cleanup_test_rows.py 按
# 模板名前缀「验证用-」级联清理。
#
# 但 B6 那条跑真实 ComfyUI 的任务会给素材库累积 2 张测试图（每跑一次 +2），
# 现在有 DELETE /api/tasks/{id} 了，跑完直接删掉自己的产物，别污染素材库。
if task.get("status") in ("done", "error"):
    st, body = req("DELETE", f"/api/tasks/{task_id}", token=token)
    print(f"      清理 B6 测试任务 {task_id[:8]}: HTTP {st} {body.get('message', body)}")
else:
    print(f"      B6 任务仍在 {task.get('status')}，跳过清理")

print(f"      测试模板 {B5_NAME!r} 留作残留，用 cleanup_test_rows.py 清理")
