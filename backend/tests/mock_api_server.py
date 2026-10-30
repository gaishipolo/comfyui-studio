"""Mock 生成 API 服务器 — 用于测试 GenericAPIProvider

模拟典型的云端生成 API 模式：
  POST /generate      → 返回 task_id
  GET  /tasks/{id}    → 返回状态（先 running 后 completed）
  GET  /health        → 健康检查
  GET  /files/...     → 下载结果文件
"""
import asyncio
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import base64

# 1x1 红色 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

TASKS = {}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path.startswith("/tasks/"):
            task_id = self.path.split("/")[-1]
            task = TASKS.get(task_id)
            if not task:
                self._json(404, {"error": "not found"})
                return
            # 第一次查询返回 running，第二次返回 completed
            if task["polls"] < 1:
                task["polls"] += 1
                self._json(200, {"task_id": task_id, "status": "running", "progress": 50})
            else:
                self._json(200, {
                    "task_id": task_id,
                    "status": "completed",
                    "progress": 100,
                    "images": [{"url": f"http://127.0.0.1:9100/files/{task_id}/result.png", "filename": "result.png"}],
                })
        elif self.path.startswith("/files/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(TINY_PNG)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/generate":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            task_id = f"mock-{len(TASKS) + 1}"
            TASKS[task_id] = {"polls": 0, "input": body.get("input", {})}
            self._json(200, {"task_id": task_id})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def start_server(port=9100):
    server = HTTPServer(("127.0.0.1", port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    server = start_server()
    print("Mock API running on http://127.0.0.1:9100")
    while True:
        time.sleep(1)
