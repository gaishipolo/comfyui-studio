# -*- coding: utf-8 -*-
"""修复项冒烟测试 — review 修复后验证"""
import json
import sys
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402
from app.engines.comfyui import ComfyUIProvider  # noqa: E402

BASE = "http://127.0.0.1:8000"
PASS, FAIL = [], []


def req(method, path, token=None, json_body=None, raw=False):
    url = BASE + path
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=15)
        code, content = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, content = e.code, e.read()
    if raw:
        return code, content
    try:
        return code, json.loads(content)
    except Exception:
        return code, content.decode("utf-8", "replace")[:200]


def multipart(path, token, filename, data, content_type, extra_fields=None):
    boundary = "----smokeBoundary42"
    parts = b""
    for k, v in (extra_fields or {}).items():
        parts += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    parts += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(
        BASE + path,
        data=parts,
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'OK  ' if cond else 'FAIL'} {name} {detail}")


# ── 1. 健康检查 ──
code, body = req("GET", "/api/health")
check("health 200", code == 200, str(body))

# ── 2. 登录 ──
code, body = req("POST", "/api/auth/login", json_body={"username": "admin", "password": "admin123"})
token = body.get("access_token") if isinstance(body, dict) else None
check("login", code == 200 and bool(token))

# ── 3. B9: include_inactive ──
code, active_only = req("GET", "/api/templates", token=token)
code2, all_t = req("GET", "/api/templates?include_inactive=true", token=token)
check("B9 include_inactive 返回全量模板", code == 200 and code2 == 200
      and isinstance(all_t, list) and len(all_t) >= len(active_only),
      f"active={len(active_only)} all={len(all_t)}")

# ── 4. B2: 路径穿越必须 404 ──
trav = urllib.parse.quote("../../workbench.db", safe="")
code, _ = req("GET", f"/api/tasks/x/assets/{trav}", token=token)
check("B2 文件名穿越被拒", code == 404, f"code={code}")
code, _ = req("GET", "/api/tasks/..%2F..%2F/assets/x.png", token=token)
check("B2 task_id 穿越被拒", code == 404, f"code={code}")

# ── 5. 素材下载回归 ──
# 不能盲取 assets[0]：素材库是共享的，最新一条可能是别处留下的小文件/测试残留。
# 显式挑一条真实生成图（>1KB），断言才有确定性。
code, assets = req("GET", "/api/assets", token=token)
real_assets = [x for x in assets if (x.get("file_size") or 0) > 1000] if isinstance(assets, list) else []
if real_assets:
    a = real_assets[0]
    fname = urllib.parse.quote(a["file_name"], safe="")
    code, content = req("GET", f"/api/tasks/{a['task_id']}/assets/{fname}", token=token, raw=True)
    check("素材下载回归", code == 200 and len(content) > 1000, f"code={code} bytes={len(content)}")
else:
    check("素材下载回归", False, "无 >1KB 的真实素材可测")

# ── 6. B3: 工作流上传文件名清洗 ──
wf = json.dumps({"1": {"class_type": "X", "inputs": {}}})
code, body = multipart("/api/templates/upload-workflow", token, "../../evil_smoke.json",
                       wf.encode(), "application/json")
saved = body.get("filename", "") if isinstance(body, dict) else ""
check("B3 上传文件名被清洗", code == 200 and "/" not in saved and "\\" not in saved
      and ".." not in saved and saved.endswith(".json"), f"code={code} saved={saved!r}")
p = settings.WORKFLOW_DIR / saved
check("B3 文件落在 WORKFLOW_DIR 内", bool(saved) and p.exists(), str(p))
outside = (settings.WORKFLOW_DIR.parent / "evil_smoke.json")
check("B3 目录外无穿越文件", not outside.exists(), str(outside))

# ── 7. B7: inject_params 映射校验 ──
wf_dict = {"76": {"class_type": "X", "inputs": {"value": "old"}}}
try:
    ComfyUIProvider.inject_params(wf_dict, {"prompt": "999.inputs.value"}, {"prompt": "hi"})
    check("B7 错误节点报错", False, "未抛异常")
except ValueError as e:
    check("B7 错误节点报错", "999" in str(e), str(e)[:80])
try:
    ComfyUIProvider.inject_params(wf_dict, {"prompt": "badpath"}, {"prompt": "hi"})
    check("B7 非法路径报错", False, "未抛异常")
except ValueError:
    check("B7 非法路径报错", True)
out = ComfyUIProvider.inject_params(wf_dict, {"prompt": "76.inputs.value"}, {"prompt": "new"})
check("B7 正常注入回归", out["76"]["inputs"]["value"] == "new")

# ── 8. B11: created_by 字段 ──
code, tasks = req("GET", "/api/tasks?limit=1", token=token)
if isinstance(tasks, list) and tasks:
    check("B11 TaskResponse 含 created_by", "created_by" in tasks[0],
          f"created_by={tasks[0].get('created_by')!r}")
else:
    check("B11 TaskResponse 含 created_by", False, "无任务可测")

# ── 9. B12: role 取值校验 ──
code, _ = req("POST", "/api/auth/register",
              json_body={"username": "badrole01", "password": "test1234", "role": "superuser"},
              token=token)
check("B12 非法 role 被拒", code == 422, f"code={code}")

# ── 10. F1: 图片上传通道（依赖 ComfyUI 在线）──
img_path = Path(r"E:\xx\ai\comfyui-studio\images.jfif")
if img_path.exists():
    code, body = multipart("/api/upload/image?engine=comfyui", token, "smoke_upload.jfif",
                           img_path.read_bytes(), "image/jpeg")
    fname = body.get("filename", "") if isinstance(body, dict) else ""
    check("F1 图片上传到 ComfyUI", code == 200 and bool(fname),
          f"code={code} filename={fname!r}")
else:
    check("F1 图片上传到 ComfyUI", False, "测试图片不存在")

# ── 11. 标签持久化回归（JSON 原地修改不落库的坑）──
if real_assets:
    aid = real_assets[0]["id"]
    tag = "smoke_标签"
    # 幂等前置清理：上次跑挂可能残留同名标签，不先清掉会让「添加落库」变成空断言
    req("DELETE", f"/api/assets/{aid}/tags/{urllib.parse.quote(tag)}", token=token)
    req("POST", f"/api/assets/{aid}/tags?tag={urllib.parse.quote(tag)}", token=token)
    code, detail = req("GET", f"/api/assets/{aid}", token=token)
    check("标签添加真正落库", tag in (detail.get("tags") or []), f"tags={detail.get('tags')}")
    code, filtered = req("GET", f"/api/assets?tag={urllib.parse.quote(tag)}", token=token)
    check("标签过滤命中", isinstance(filtered, list) and any(a["id"] == aid for a in filtered))
    req("DELETE", f"/api/assets/{aid}/tags/{urllib.parse.quote(tag)}", token=token)
    code, detail = req("GET", f"/api/assets/{aid}", token=token)
    check("标签删除真正落库", tag not in (detail.get("tags") or []), f"tags={detail.get('tags')}")

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
