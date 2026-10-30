"""Grsai 三个插件线格式联调（离线，假服务端）

背景：provider_grsai / provider_grsai_gpt_image / provider_grsai_minimax_h3 写完只做过
py_compile，请求体形状、异步轮询、状态映射、产物下载都没被真正执行过。这里起一个假的
Grsai 服务端（同一套 /v1/api/generate + /v1/api/result?id=），把三个插件跑通一遍。

覆盖：
- 提交：鉴权头、请求体关键字段、images 数组形状（多图保序）、replyType=async
- 轮询：running(40%) → succeeded，产物带 url + 纯 filename
- 失败/违规：failed 以 HTTP 400 返回时仍要判成 ERROR 并带可读文案（不能当网络错误）
- 下载：URL 形态产物能取回字节
- 探活：无效 id 返回 404 也算通
- 插件差异：gpt_image 的像素优先、minimax_h3 的 audios/duration

用法：python tests/verify_grsai_plugins.py
"""
import asyncio
import base64
import importlib.util
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.base import TaskStatus  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ok_all = True

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ── 假 Grsai 服务端 ────────────────────────────────────
SEEN = {"generate": [], "result": [], "auth": []}
POLLS = {}


class FakeGrsai(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/v1/api/generate":
            return self._send(404, {"error": "no route"})
        SEEN["generate"].append(payload)
        SEEN["auth"].append(self.headers.get("Authorization"))
        prompt = str(payload.get("prompt") or "")
        if "FAIL" in prompt:
            return self._send(200, {"id": "fail-1"})
        if "VIOLATION" in prompt:
            return self._send(200, {"id": "viol-1"})
        if "REJECT" in prompt:
            return self._send(401, {"error": "invalid api key"})
        return self._send(200, {"id": "ok-1"})

    def do_GET(self):
        if self.path.startswith("/files/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(TINY_PNG)))
            self.end_headers()
            self.wfile.write(TINY_PNG)
            return
        if not self.path.startswith("/v1/api/result"):
            return self._send(404, {"error": "no route"})
        tid = ""
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if kv.startswith("id="):
                    tid = kv[3:]
        SEEN["result"].append(tid)
        SEEN["auth"].append(self.headers.get("Authorization"))

        if tid == "fail-1":
            # 文档行为：失败可能以 400 返回，body 仍是 {id,status,error}
            return self._send(400, {"id": tid, "status": "failed", "error": "模型拒绝生成"})
        if tid == "viol-1":
            return self._send(200, {"id": tid, "status": "violation", "error": "内容违规"})
        if tid != "ok-1":
            return self._send(404, {"error": "task not found"})

        POLLS[tid] = POLLS.get(tid, 0) + 1
        if POLLS[tid] < 2:
            return self._send(200, {"id": tid, "status": "running", "progress": 40})
        return self._send(200, {
            "id": tid, "status": "succeeded", "progress": 100,
            "results": [{"url": f"http://127.0.0.1:{PORT}/files/out.png?token=x"}],
        })


srv = HTTPServer(("127.0.0.1", 0), FakeGrsai)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE_URL = f"http://127.0.0.1:{PORT}"


def load(filename):
    path = EXAMPLES / filename
    name = f"verify_grsai_{path.stem}_{uuid.uuid4().hex[:6]}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    cls = next(v for v in vars(mod).values()
               if isinstance(v, type) and getattr(v, "name", "") and hasattr(v, "capabilities"))
    return cls({"api_key": "sk-verify", "base_url": BASE_URL, "image_format": "data_uri"})


def run(coro):
    return asyncio.run(coro)


URLS = ["https://cdn.example/1.png", "https://cdn.example/2.png"]

CASES = [
    ("provider_grsai.py", "grsai", "image_compose", {"prompt": "合成", "image": URLS}),
    ("provider_grsai_gpt_image.py", "grsai_gpt_image", "image_compose", {"prompt": "合成", "image": URLS}),
    ("provider_grsai_minimax_h3.py", "grsai_minimax_h3", "image_to_video",
     {"prompt": "运镜", "image": URLS, "duration": 5, "resolution": "480p"}),
]

print("\n[1] 提交 → 轮询 → 下载（三个插件各跑一遍）")
for filename, pname, cap, params in CASES:
    p = load(filename)
    SEEN["generate"].clear(); SEEN["result"].clear(); SEEN["auth"].clear(); POLLS.clear()

    ctx = {"_capability": cap, "_type": "image", "_template_name": "verify"}
    tid = run(p.submit(ctx, params))
    report(f"{pname} submit 返回任务 ID", tid == "ok-1", tid)

    sent = SEEN["generate"][0]
    report(f"{pname} 带 Bearer 鉴权", SEEN["auth"][0] == "Bearer sk-verify", str(SEEN["auth"][0]))
    report(f"{pname} replyType=async", sent.get("replyType") == "async")
    report(f"{pname} images 是 2 项且保序", sent.get("images") == URLS, str(sent.get("images")))
    report(f"{pname} last_request 快照已生成", isinstance(p.last_request, dict) and "prompt" in p.last_request)

    first = run(p.get_status(tid))
    report(f"{pname} 首轮 RUNNING 40%",
           first.status == TaskStatus.RUNNING and abs(first.progress - 40.0) < 0.01,
           f"{first.status} {first.progress}")

    second = run(p.get_status(tid))
    report(f"{pname} 次轮 DONE", second.status == TaskStatus.DONE and second.success, str(second.status))
    out = second.outputs[0] if second.outputs else {}
    report(f"{pname} 产物带 url", str(out.get("url", "")).startswith("http"), str(out.get("url"))[:50])
    report(f"{pname} filename 是纯文件名（去掉 query）", out.get("filename") == "out.png", str(out.get("filename")))

    data = run(p.download_output(out["url"]))
    report(f"{pname} 下载产物字节", data == TINY_PNG, f"{len(data)} bytes")

    report(f"{pname} 探活通过（404 也算通）", run(p.health_check()) is True)
    report(f"{pname} cancel 诚实返回 False", run(p.cancel(tid)) is False)

print("\n[2] 失败 / 违规 / 鉴权错误")
p = load("provider_grsai.py")
ctx = {"_capability": "text_to_image"}

tid = run(p.submit(ctx, {"prompt": "FAIL 一下"}))
res = run(p.get_status(tid))
report("failed 走 400 仍判 ERROR", res.status == TaskStatus.ERROR, f"{res.status}")
report("失败文案可读（不是 HTTP 报错）", "模型拒绝生成" in res.error, res.error[:40])

tid = run(p.submit(ctx, {"prompt": "VIOLATION 一下"}))
res = run(p.get_status(tid))
report("violation 判 ERROR", res.status == TaskStatus.ERROR, str(res.status))
report("违规文案可读", "违规" in res.error, res.error[:40])

try:
    run(p.submit(ctx, {"prompt": "REJECT 一下"}))
    report("提交 401 要抛错", False)
except ValueError as e:
    report("提交 401 要抛错", "401" in str(e), str(e)[:60])

print("\n[3] 插件差异")
gpt = load("provider_grsai_gpt_image.py")
pl = gpt.build_payload("text_to_image", {"prompt": "x", "imageSize": "1536x1024", "aspectRatio": "16:9"})
report("gpt_image 像素优先于比例", pl.get("aspectRatio") == "1536x1024", str(pl.get("aspectRatio")))
pl = gpt.build_payload("text_to_image", {"prompt": "x", "aspectRatio": "16:9"})
report("gpt_image 无像素时用比例", pl.get("aspectRatio") == "16:9", str(pl.get("aspectRatio")))

h3 = load("provider_grsai_minimax_h3.py")
pl = h3.build_payload("image_to_video", {
    "prompt": "x", "image": URLS, "duration": 8, "resolution": "768p",
    "audio": "https://cdn.example/a.mp3", "audio2": "", "seed": "42", "promptSkill": False,
})
report("h3 audios 收拢为 1 项", pl.get("audios") == ["https://cdn.example/a.mp3"], str(pl.get("audios")))
report("h3 seed 转 int", pl.get("seed") == 42, str(pl.get("seed")))
report("h3 promptSkill=False 要下发", pl.get("promptSkill") is False, str(pl.get("promptSkill")))

print("\n[3b] bool 字段的字符串形态（表单可能给 \"false\"）")
for raw, expect in [(True, True), (False, False), ("true", True), ("false", False),
                    ("TRUE", True), ("否", False), ("是", True), (1, True), (0, False)]:
    try:
        got = h3._to_bool(raw)
        report(f"_to_bool({raw!r}) → {expect}", got is expect, repr(got))
    except ValueError as e:
        report(f"_to_bool({raw!r}) → {expect}", False, str(e)[:40])
try:
    h3._to_bool("maybe")
    report("非法 bool 值要报错", False)
except ValueError as e:
    report("非法 bool 值要报错", "只接受 true/false" in str(e), str(e)[:40])

pl = h3.build_payload("text_to_video", {"prompt": "x", "duration": 5, "promptSkill": "false"})
report("字符串 \"false\" 不能变成开启", pl.get("promptSkill") is False, str(pl.get("promptSkill")))

print("\n[4] 参考图短标记 → data URI")
marker = run(h3.upload_input(TINY_PNG, "tiny.png"))
pl = h3.build_payload("image_to_video", {"prompt": "x", "image": [marker], "duration": 5})
img = (pl.get("images") or [""])[0]
report("wbinput 标记还原成 data URI", img.startswith("data:image/png;base64,"), img[:32])
report("data URI 能解回原图", base64.b64decode(img.split(",", 1)[1]) == TINY_PNG)

srv.shutdown()
print()
print("全部通过" if ok_all else "存在失败项")
sys.exit(0 if ok_all else 1)
