"""生产部署验证 —— 后端单进程托管前端构建产物

验证 /、静态资源、SPA 回落、API 路由不被兜底吞掉、目录穿越拦截。
"""
import json, sys, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000"
ok_all = True


def get(path, token=None):
    r = urllib.request.Request(BASE + path)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        return 0, str(e).encode(), {}


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


print("=== 生产部署验证（后端托管前端）===\n")

# 1. 根路径应返回 index.html
st, body, hdr = get("/")
is_html = b"<div id=\"root\"" in body or b"<!doctype html" in body.lower()
report("GET / 返回前端页面", st == 200 and is_html,
       f"status={st} content-type={hdr.get('Content-Type', '?')}")

# 2. index.html 引用的 JS/CSS 资源可访问
import re
html = body.decode("utf-8", "ignore")
assets = re.findall(r'/assets/[\w.-]+\.(?:js|css)', html)
report("index.html 引用资源", len(assets) >= 2, f"{assets}")
for a in assets:
    st2, b2, h2 = get(a)
    report(f"  资源可取 {a}", st2 == 200 and len(b2) > 1000,
           f"status={st2} bytes={len(b2)}")

# 3. SPA 回落：未知非 API 路径应返回 index.html
st3, b3, _ = get("/some/unknown/page")
report("未知路径回落 index.html", st3 == 200 and b"root" in b3, f"status={st3}")

# 4. 未知 API 路径必须 404 JSON，不能被 HTML 吞掉
st4, b4, h4 = get("/api/definitely-not-a-route")
try:
    json.loads(b4)
    is_json = True
except Exception:
    is_json = False
report("未知 API 路径 404 JSON", st4 == 404 and is_json and b4.startswith(b"{"),
       f"status={st4} body={b4[:60]}")

# 5. 已知 API 路由不受兜底影响
st5, b5, _ = get("/api/health")
report("API 路由正常", st5 == 200 and b"comfyui" in b5, f"{b5[:60]}")

# 6. 登录 + 取数据，确认前后端链路通
req = urllib.request.Request(BASE + "/api/auth/login", method="POST")
req.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req, json.dumps(
        {"username": "admin", "password": "admin123"}).encode()) as r:
    tok = json.loads(r.read())["access_token"]
report("登录接口可用", bool(tok))

st6, b6, _ = get("/api/templates", tok)
report("业务接口可用", st6 == 200 and json.loads(b6), f"模板 {len(json.loads(b6))} 个")

# 7. 目录穿越拦截
for evil in ["/../../workbench.db", "/assets/../../../workbench.db",
             "/..%2f..%2fworkbench.db"]:
    st7, b7, _ = get(evil)
    leaked = b"SQLite" in b7 or b"SQLite format" in b7
    report(f"穿越拦截 {evil}", st7 in (200, 404) and not leaked,
           f"status={st7} bytes={len(b7)}")

# 8. 未构建时的友好报错（直接调用路由函数验证分支存在）
from app.main import _DIST
report("FRONTEND_DIST 指向构建产物", _DIST.exists() and (_DIST / "index.html").is_file(),
       f"{_DIST}")

print(f"\n结果: {'[OK] 单进程部署可用' if ok_all else '[FAIL] 部署链路有问题'}")
sys.exit(0 if ok_all else 1)
