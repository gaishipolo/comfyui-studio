"""单字段多图（API 层）：确认 multiple 从插件能力一路透到模板 input_schema

离线脚本 verify_multi_image.py 只验证插件自身的逻辑；这里补上中间那段——
插件能力 → /api/plugins 响应 → 建模板 → input_schema 归一化白名单 → 读回。
归一化是白名单实现，新属性漏加就会在这一步被静默丢掉，必须实测。

需要后端在 127.0.0.1:8000 运行（跑新代码）。测试模板自建自删。
"""
import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000/api"
ok = True


def req(path, method="GET", data=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def report(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


T = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})[1]["access_token"]

# ① 插件能力里 image 字段带 multiple
st, plugins = req("/plugins", token=T)
items = plugins if isinstance(plugins, list) else plugins.get("items", [])
h3 = next((p for p in items if p.get("name") == "grsai_minimax_h3"), None)
report("插件列表含 grsai_minimax_h3", h3 is not None, f"共 {len(items)} 个")
if h3:
    caps = h3["capabilities"]
    cap = caps["image_to_video"] if isinstance(caps, dict) else next(c for c in caps if c["kind"] == "image_to_video")
    img = next(f for f in cap["fields"] if f["type"] == "image")
    report("image_to_video.image.multiple = true", img["multiple"] is True, json.dumps(img, ensure_ascii=False)[:90])
    report("无 image2/image3 槽位", not [f for f in cap["fields"] if f["name"] in ("image2", "image3")])

# ② 带 multiple 的 input_schema 存得下、读得回
name = f"deltest-multi-{uuid.uuid4().hex[:6]}"
schema = {"fields": [
    {"name": "prompt", "label": "提示词", "type": "string", "required": True},
    {"name": "image", "label": "参考图", "type": "image_upload", "required": True, "multiple": True},
]}
st, created = req("/templates", "POST", {
    "name": name, "description": "", "category": "video", "icon": "🎬",
    "input_schema": schema, "engine": "grsai_minimax_h3", "capability": "image_to_video",
    "workflow_file": "", "param_mapping": {}, "output_config": {},
    "is_active": True, "sort_order": 0,
}, token=T)
report("创建模板 200", st == 200, f"HTTP {st}")
tid = created.get("id")
if tid:
    got = req(f"/templates/{tid}", token=T)[1]
    img_field = next((f for f in got["input_schema"]["fields"] if f["name"] == "image"), None)
    report("input_schema 读回 multiple = true", bool(img_field) and img_field.get("multiple") is True,
           json.dumps(img_field, ensure_ascii=False)[:90])
    report("清理模板", req(f"/templates/{tid}", "DELETE", token=T)[0] == 200)

print("全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
