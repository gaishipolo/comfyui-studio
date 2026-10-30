"""引擎插件机制验证（2026-10-29 多引擎）

覆盖 Provider 插件全链路：
- 上传校验的错误路径（语法错 / 依赖越界 / 无子类 / 撞内置名 / 非 .py）
- 合法上传 → 描述 → 列表（内置 + 插件一视同仁）
- 同名重传 = 升级覆盖
- 配置注入 + 实例缓存失效（假 HTTP 服务记录 Authorization，k1 → k2 换配置必须生效）
- 启停（停用后引擎不可用）/ 删除（文件与账一起清）/ 内置保护
- 越权：member 一律 403
- examples/provider_template.py 进程内全链路：submit → get_status → download_output

需要后端在 127.0.0.1:8000 运行（跑新代码）。
测试数据全部自建（deltest- 前缀），跑完自清理。
"""
import asyncio
import base64
import json
import socket
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api"
BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "workbench.db"
ok_all = True

# 1x1 红色 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


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


def upload(token, filename, code: bytes):
    """multipart 上传 .py（urllib 没有现成的，手工拼 body）"""
    boundary = "----wbt" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: text/x-python\r\n\r\n"
    ).encode() + code + f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + "/plugins/upload", method="POST", data=body)
    r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ── 假供应商 API（记录 Authorization，供配置注入断言）─────────────
AUTH_LOG: list[str] = []
TASK_POLLS: dict[str, int] = {}
PORT = 0


class FakeVendor(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        AUTH_LOG.append(self.headers.get("Authorization", ""))
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path.startswith("/v1/tasks/"):
            tid = self.path.rsplit("/", 1)[-1]
            n = TASK_POLLS.get(tid, 0)
            TASK_POLLS[tid] = n + 1
            if n < 1:
                self._json(200, {"status": "running", "progress": 0.4})
            else:
                self._json(200, {
                    "status": "succeeded", "progress": 1.0,
                    "output": {"url": f"http://127.0.0.1:{PORT}/v1/files/out.png"},
                })
        elif self.path.startswith("/v1/files/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(TINY_PNG)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        AUTH_LOG.append(self.headers.get("Authorization", ""))
        if self.path == "/v1/generate":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._json(200, {"task_id": f"vt-{len(TASK_POLLS) + 1}"})
        elif self.path.startswith("/v1/files"):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._json(200, {"url": f"http://127.0.0.1:{PORT}/v1/files/up.png"})
        else:
            self._json(404, {"error": "not found"})


def start_fake():
    global PORT
    srv = HTTPServer(("127.0.0.1", 0), FakeVendor)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 测试插件源码（deltest_plug：health_check 打配置的 base_url/health）──
PLUGIN_SRC = '''
import httpx
from app.engines.base import (
    CapabilitySpec, FieldSpec, GenerationProvider, GenerationResult, TaskStatus,
)


class DelTestProvider(GenerationProvider):
    name = "deltest_plug"
    label = "删除测试引擎"

    capabilities = {
        "text_to_image": CapabilitySpec(
            kind="text_to_image", label="文生图",
            fields=[FieldSpec(name="prompt", label="提示词", type="string", required=True)],
        ),
    }
    config_schema = [
        FieldSpec(name="base_url", label="API 地址", type="string", required=True),
        FieldSpec(name="api_key", label="API 密钥", type="string", required=True),
    ]

    async def submit(self, ctx: dict, params: dict) -> str:
        self.last_request = {"ctx_type": ctx.get("_type"), "params": params}
        return "deltest-task"

    async def get_status(self, task_id: str) -> GenerationResult:
        return GenerationResult(success=True, status=TaskStatus.DONE, progress=100.0,
                                outputs=[{"url": "http://x/f.png", "filename": "f.png",
                                          "subfolder": "", "type": "output"}])

    async def download_output(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        return TINY

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(self.config.get("base_url", "").rstrip("/") + "/health",
                                headers={"Authorization": "Bearer " + self.config.get("api_key", "")})
                return r.status_code == 200
        except Exception:
            return False
'''.replace("TINY", repr(TINY_PNG))

# ── 账号 ─────────────────────────────────────────────
_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
ADMIN_T = tok["access_token"]
ADMIN_ID = tok["user"]["id"]

member_name = f"deltest-plug-{uuid.uuid4().hex[:8]}"
req("/auth/register", "POST",
    {"username": member_name, "password": "pw123456", "display_name": "插件测试-member", "role": "member"},
    token=ADMIN_T)
_, mtok = req("/auth/login", "POST", {"username": member_name, "password": "pw123456"})
MEMBER_T = mtok["access_token"]

fake = start_fake()

print("\n=== 1. 上传校验 —— 错误路径（每条都要 400 + 可读文案）===")
st, d = upload(ADMIN_T, "bad_syntax.py", b"def broken(:\n  pass\n")
report("语法错被拒", st == 400 and "语法错误" in str(d.get("detail", "")), f"st={st} {d.get('detail', '')[:60]}")

st, d = upload(ADMIN_T, "bad_dep.py", b"import numpy\nfrom app.engines.base import GenerationProvider\n")
report("依赖越界被拒", st == 400 and "白名单" in str(d.get("detail", "")), f"st={st} {d.get('detail', '')[:60]}")

st, d = upload(ADMIN_T, "no_subclass.py", b"from app.engines.comfyui import ComfyUIProvider\n")
report("只 import 不算子类", st == 400 and "没找到" in str(d.get("detail", "")), f"st={st} {d.get('detail', '')[:60]}")

st, d = upload(ADMIN_T, "not_python.txt", b"hello")
report("非 .py 被拒", st == 400 and ".py" in str(d.get("detail", "")), f"st={st}")

reserved_src = PLUGIN_SRC.replace('"deltest_plug"', '"comfyui"').replace("DelTestProvider", "RsvProvider")
st, d = upload(ADMIN_T, "reserved.py", reserved_src.encode())
report("撞内置名被拒", st == 400 and "冲突" in str(d.get("detail", "")), f"st={st} {d.get('detail', '')[:60]}")

print("\n=== 2. 合法上传 → 描述 → 列表 ===")
st, d = upload(ADMIN_T, "deltest_plug.py", PLUGIN_SRC.encode())
report("合法插件上传成功", st == 200 and d.get("name") == "deltest_plug", f"st={st} {d.get('detail', '')[:60]}")
report("描述含能力声明", d.get("capabilities", {}).get("text_to_image", {}).get("label") == "文生图")
report("描述含配置项", [f["name"] for f in d.get("config_schema", [])] == ["base_url", "api_key"])

st, lst = req("/plugins", token=ADMIN_T)
report("GET /plugins 200", st == 200)
by_name = {p["name"]: p for p in lst}
report("内置 comfyui 在列且 builtin", by_name.get("comfyui", {}).get("builtin") is True)
report("内置 generic_api 在列", "generic_api" in by_name)
plug = by_name.get("deltest_plug", {})
report("插件在列且非内置", plug.get("builtin") is False, f"got {plug.get('builtin')}")
report("插件已启用", plug.get("enabled") is True)
report("审计：上传人是 admin", plug.get("uploaded_by") == ADMIN_ID, f"got {plug.get('uploaded_by')}")
report("能力随列表返回", "text_to_image" in plug.get("capabilities", {}))
report("has_config=True", plug.get("has_config") is True)

print("\n=== 3. 同名重传 = 升级覆盖 ===")
st, d = upload(ADMIN_T, "deltest_plug.py", PLUGIN_SRC.replace('"删除测试引擎"', '"删除测试引擎v2"').encode())
st2, lst = req("/plugins", token=ADMIN_T)
by_name = {p["name"]: p for p in lst}
report("重传成功且 label 更新", st == 200 and by_name.get("deltest_plug", {}).get("label") == "删除测试引擎v2",
       f"st={st} label={by_name.get('deltest_plug', {}).get('label')}")

print("\n=== 4. 配置注入 + 缓存失效（换 api_key 必须生效）===")
st, _ = req("/plugins/deltest_plug/config", "PUT",
            {"config": {"base_url": f"http://127.0.0.1:{PORT}", "api_key": "k1"}}, token=ADMIN_T)
report("保存配置 k1", st == 200, f"st={st}")
AUTH_LOG.clear()
st, d = req("/plugins/deltest_plug/test", "POST", token=ADMIN_T)
report("test 通过", st == 200 and d.get("ok") is True, f"st={st} {d}")
report("请求带 Bearer k1", AUTH_LOG and AUTH_LOG[-1] == "Bearer k1", f"got {AUTH_LOG}")

st, _ = req("/plugins/deltest_plug/config", "PUT",
            {"config": {"base_url": f"http://127.0.0.1:{PORT}", "api_key": "k2"}}, token=ADMIN_T)
st, d = req("/plugins/deltest_plug/test", "POST", token=ADMIN_T)
report("换 k2 后 test 仍通过", st == 200 and d.get("ok") is True)
report("实例缓存失效：请求已带 Bearer k2", AUTH_LOG[-1] == "Bearer k2", f"got {AUTH_LOG[-1:]}")

print("\n=== 5. 启停 ===")
st, _ = req("/plugins/deltest_plug/enabled", "PUT", {"enabled": False}, token=ADMIN_T)
report("停用成功", st == 200, f"st={st}")
st, lst = req("/plugins", token=ADMIN_T)
by_name = {p["name"]: p for p in lst}
report("列表 enabled=False", by_name.get("deltest_plug", {}).get("enabled") is False)
st, d = req("/plugins/deltest_plug/test", "POST", token=ADMIN_T)
report("停用后 test 400（引擎不可用）", st == 400 and "未启用" in str(d.get("detail", "")), f"st={st}")

st, _ = req("/plugins/deltest_plug/enabled", "PUT", {"enabled": True}, token=ADMIN_T)
st, d = req("/plugins/deltest_plug/test", "POST", token=ADMIN_T)
report("重新启用后 test 恢复（配置从 DB 回注）", st == 200 and d.get("ok") is True, f"st={st} {d}")

print("\n=== 6. 越权（member 一律 403）===")
st, _ = req("/plugins", token=MEMBER_T)
report("member GET 403", st == 403, f"st={st}")
st, d = upload(MEMBER_T, "x.py", b"print(1)")
report("member upload 403", st == 403, f"st={st}")
st, _ = req("/plugins/deltest_plug/enabled", "PUT", {"enabled": False}, token=MEMBER_T)
report("member 启停 403", st == 403, f"st={st}")

print("\n=== 7. 内置保护 + 删除 ===")
st, d = req("/plugins/comfyui", "DELETE", token=ADMIN_T)
report("内置引擎拒删", st == 400 and "内置" in str(d.get("detail", "")), f"st={st}")

st, _ = req("/plugins/deltest_plug", "DELETE", token=ADMIN_T)
report("删除插件", st == 200, f"st={st}")
st, lst = req("/plugins", token=ADMIN_T)
report("列表不再含 deltest_plug", "deltest_plug" not in {p["name"] for p in lst})
report("代码文件已删", not (BACKEND / "plugins" / "deltest_plug.py").exists())

print("\n=== 8. 示例插件（examples/provider_template.py）进程内全链路 ===")
from app.engines.factory import get_provider, set_engine_config, unregister_provider  # noqa: E402
from app.engines.plugin_loader import PluginError, load_plugin_file  # noqa: E402

info = load_plugin_file(BACKEND / "examples" / "provider_template.py")
report("示例插件加载成功", info.get("name") == "sample_cloud", f"got {info.get('name')}")
report("能力齐（文生图/图生图）", set(info.get("capabilities", {})) == {"text_to_image", "image_to_image"},
       f"got {sorted(info.get('capabilities', {}))}")
report("配置项齐", [f["name"] for f in info.get("config_schema", [])] == ["base_url", "api_key", "model"])

set_engine_config("sample_cloud", {"base_url": f"http://127.0.0.1:{PORT}", "api_key": "ek", "model": "m1"})
provider = get_provider("sample_cloud")


async def e2e():
    task_id = await provider.submit(
        {"_type": "image", "_capability": "text_to_image", "_template_name": "e2e",
         "_workflow": {}, "_param_mapping": {}},
        {"prompt": "一只猫", "size": "512*512", "n": 1},
    )
    r1 = await provider.get_status(task_id)          # 第一次：running
    r2 = await provider.get_status(task_id)          # 第二次：succeeded
    data = await provider.download_output(r2.outputs[0]["url"])
    return task_id, r1, r2, data


task_id, r1, r2, data = asyncio.run(e2e())
report("submit 返回任务 ID", bool(task_id), f"got {task_id}")
report("请求体快照（last_request）", provider.last_request.get("model") == "m1"
       and provider.last_request.get("size") == "512*512",
       f"got {provider.last_request}")
report("get_status 首查 running", r1.status.value == "running", f"got {r1.status}")
report("get_status 再查 succeeded/outputs", r2.success and r2.outputs and r2.outputs[0]["filename"] == "out.png",
       f"got {r2.status} {r2.outputs}")
report("download_output 拿到 PNG 字节", data == TINY_PNG, f"len={len(data)}")
report("产物带 url 字段（落盘防死链约定）", "url" in r2.outputs[0])
unregister_provider("sample_cloud")

print("\n=== 9. 清理 ===")
st, _ = req("/plugins/deltest_plug", "DELETE", token=ADMIN_T)   # 幂等兜底（前面已删则 404）
conn = sqlite3.connect(DB_PATH)
cur = conn.execute("DELETE FROM users WHERE username LIKE 'deltest-plug-%'")
conn.commit()
conn.close()
report("member 测试账号已清", cur.rowcount == 1, f"deleted={cur.rowcount}")
report("无残留插件文件", not any((BACKEND / "plugins").glob("deltest_*.py")) if (BACKEND / "plugins").exists() else True)

print(f"\n{'=' * 50}\n结果: {'全部通过 ✅' if ok_all else '存在失败 ❌'}")
sys.exit(0 if ok_all else 1)
