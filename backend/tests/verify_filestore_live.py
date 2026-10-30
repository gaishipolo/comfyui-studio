"""MinIO / S3 协议联调 —— 假客户端证明不了的东西，只有跑真协议才知道。

`verify_filestore.py` 用假客户端验证「我们怎么调 minio SDK」，
本脚本验证「minio SDK 确实这么表现」—— 即 SDK 的请求构造、签名头、
响应解析、连接回收（``release_conn``）在真实 HTTP 往返里都对得上：

- ``stat_object`` 对不存在的键抛什么（我们靠它做 exists / delete 前置判断）
- ``get_object`` 返回的响应对象能 ``read(n)`` 分块、``release_conn()`` 收尾
- ``put_object`` 的 content_type 真的存到对象上
- 键里的 ``/`` 是「前缀」而不是「文件名的一部分」

服务端按优先级三选一：

1. **真实 MinIO**（``MINIO_ENDPOINT`` 可达）—— 最强证据
2. **进程内 S3 线格式服务端**（本文件自带）—— 验证 SDK 线上行为，不依赖外部服务
3. 都不可用 → 跳过并退出 0（不阻塞日常回归）

只有「连上了却跑挂」才算失败。
"""
from __future__ import annotations

import io
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ────────────────────── 进程内 S3 线格式服务端 ──────────────────────

class _S3Stub(BaseHTTPRequestHandler):
    """按 S3 线格式应答。只实现 minio SDK 用到的那几个动词。

    支持**路径式**（``/bucket/key``）和**虚拟主机式**（``bucket.s3.local/key``）
    两种寻址，免得 SDK 换个寻址策略就误判成实现有问题。
    """

    protocol_version = "HTTP/1.1"
    server_version = "S3WireStub/1.0"
    objects: dict = {}          # (bucket, key) -> (bytes, content_type)
    buckets: set = set()        # 桶要单独记：空桶没有任何对象，靠 objects 推不出来
    requests: list = []

    def log_message(self, fmt, *args):  # 安静点，只留我们自己的报告
        pass

    # ── 寻址 ───────────────────────────────────────────────────
    def _target(self):
        parsed = urlparse(self.path)
        segs = [s for s in parsed.path.split("/") if s]
        host = (self.headers.get("Host") or "").split(":")[0]
        # 虚拟主机式：Host 形如 bucket.127.0.0.1 或 bucket.s3.local
        m = re.match(r"^([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])\.(?:s3[.\-].*|localhost|127\.0\.0\.1)$", host)
        if m and segs:
            return m.group(1), "/".join(segs)
        if segs:
            return segs[0], "/".join(segs[1:])
        return "", ""

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-amz-request-id", "stub-req-1")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, status, code, message):
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f"<Error><Code>{code}</Code><Message>{message}</Message>"
            f"<RequestId>stub-req-1</RequestId></Error>"
        ).encode()
        self._reply(status, body, {"Content-Type": "application/xml"})

    # ── 动词 ───────────────────────────────────────────────────
    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        bucket, key = self._target()
        self.requests.append(("GET", bucket, key, dict(self.headers)))
        if not key:
            if "location" in self._query():
                self._reply(200, b'<?xml version="1.0" encoding="UTF-8"?>'
                                b"<LocationConstraint>us-east-1</LocationConstraint>",
                            {"Content-Type": "application/xml"})
                return
            # ListBuckets
            names = sorted(self.buckets)
            body = ('<?xml version="1.0" encoding="UTF-8"?><ListAllMyBucketsResult><Buckets>'
                    + "".join(f"<Bucket><Name>{n}</Name></Bucket>" for n in names)
                    + "</Buckets></ListAllMyBucketsResult>").encode()
            self._reply(200, body, {"Content-Type": "application/xml"})
            return
        rec = self.objects.get((bucket, key))
        if rec is None:
            self._err(404, "NoSuchKey", "The specified key does not exist.")
            return
        data, ctype = rec
        self._reply(200, data, {
            "Content-Type": ctype,
            "ETag": '"stub-etag"',
            "Last-Modified": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        })

    def do_HEAD(self):
        """HEAD 的 Content-Length 必须等于对象大小（SDK 靠它拿 size），不能套 _reply。"""
        bucket, key = self._target()
        self.requests.append(("HEAD", bucket, key, dict(self.headers)))
        if not key:
            # 桶级 HEAD —— bucket_exists() 走的就是这个
            self.send_response(200 if bucket in self.buckets else 404)
            self.send_header("x-amz-request-id", "stub-req-1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rec = self.objects.get((bucket, key))
        if rec is None:
            self._err(404, "NoSuchKey", "The specified key does not exist.")
            return
        data, ctype = rec
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"stub-etag"')
        self.send_header("Last-Modified", datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self.send_header("x-amz-request-id", "stub-req-1")
        self.end_headers()

    def do_PUT(self):
        bucket, key = self._target()
        body = self._read_body()
        self.requests.append(("PUT", bucket, key, dict(self.headers)))
        if not key:
            self.buckets.add(bucket)          # MakeBucket
            self._reply(200, b"")
            return
        ctype = self.headers.get("Content-Type") or "application/octet-stream"
        self.objects[(bucket, key)] = (body, ctype)
        self._reply(200, b"", {"ETag": '"stub-etag"'})

    def do_DELETE(self):
        bucket, key = self._target()
        self.requests.append(("DELETE", bucket, key, dict(self.headers)))
        self.objects.pop((bucket, key), None)
        self._reply(204, b"")


class _StubServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _S3Stub)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ────────────────────── 挑一个服务端 ──────────────────────

try:
    from minio import Minio
except ImportError:
    print("SKIP: 未安装 minio 客户端（pip install minio）")
    sys.exit(0)

endpoint = settings.MINIO_ENDPOINT or "127.0.0.1:9000"
access = settings.MINIO_ACCESS_KEY or "minioadmin"
secret = settings.MINIO_SECRET_KEY or "minioadmin"
mode = ""

stub = None
client = None

# 1) 真实 MinIO
try:
    probe = Minio(endpoint, access_key=access, secret_key=secret, secure=settings.MINIO_SECURE)
    probe.list_buckets()
    client = probe
    mode = f"真实 MinIO（{endpoint}）"
except Exception:
    client = None

# 2) 进程内线格式服务端
if client is None:
    try:
        stub = _StubServer()
        stub.start()
        endpoint = f"127.0.0.1:{stub.port}"
        client = Minio(endpoint, access_key=access, secret_key=secret,
                       secure=False, region="us-east-1")
        client.list_buckets()          # 握手：确认 SDK 真的能跟它说话
        mode = f"进程内 S3 线格式服务端（{endpoint}）"
    except Exception as exc:
        print(f"SKIP: 服务端起不来 —— {type(exc).__name__}: {exc}")
        if stub:
            stub.stop()
        sys.exit(0)

print(f"=== S3 协议联调：{mode} ===")

from app import filestore
from app.filestore import MinioFileStore, get_store, reset_store

# 联调专用 bucket，别污染配置里那个
settings.MINIO_BUCKET = "wb-filestore-live"
if not client.bucket_exists(settings.MINIO_BUCKET):
    client.make_bucket(settings.MINIO_BUCKET)
report("bucket 就绪", client.bucket_exists(settings.MINIO_BUCKET), settings.MINIO_BUCKET)

store = MinioFileStore(client=client)

# ── 1. 写入 / 回读 / 元数据 ────────────────────────────────────
print("\n=== 写入与回读 ===")

KEY = "live-task-001/sample-video.mp4"
payload = bytes(range(256)) * 4096          # 1MB 二进制，含 \n 也含大量无 \n 段
store.save(KEY, payload, content_type="video/mp4")

report("exists 为真", store.exists(KEY))
report("read_bytes 整段无损", store.read_bytes(KEY) == payload, f"{len(payload)} bytes")

stat = client.stat_object(settings.MINIO_BUCKET, KEY)
report("content_type 真的存到对象上", getattr(stat, "content_type", "") == "video/mp4",
       repr(getattr(stat, "content_type", None)))
report("对象大小一致", int(stat.size) == len(payload))

handle = store.open(KEY)
report("open 拿到句柄", handle is not None)
report("size 从 stat 来", handle is not None and handle.size == len(payload))
report("对象存储没有 local_path", handle is not None and handle.local_path is None)

chunks = list(handle.iter_bytes())
report("流式读取整段无损", b"".join(chunks) == payload)
report("确实按块读（不是一次性拉满）", len(chunks) > 1, f"{len(chunks)} chunks")
handle.close()
try:
    handle.close()
    report("close() 幂等，重复调用不炸", True)
except Exception as exc:
    report("close() 幂等，重复调用不炸", False, str(exc))

# ── 2. 前缀语义：/ 是分隔符不是文件名 ──────────────────────────
print("\n=== 键的前缀语义 ===")

store.save("live-task-002/a.png", b"AAAA")
store.save("live-task-002/sub/b.png", b"BBBB")
report("同前缀不同键互不覆盖",
       store.read_bytes("live-task-002/a.png") == b"AAAA"
       and store.read_bytes("live-task-002/sub/b.png") == b"BBBB")
report("删除一个不影响另一个", store.delete("live-task-002/a.png")[0] is True)
report("另一个还在", store.read_bytes("live-task-002/sub/b.png") == b"BBBB")

# ── 3. 删除语义 ─────────────────────────────────────────────────
print("\n=== 删除语义 ===")

ok, reason = store.delete("live-task-001/never-existed.bin")
report("删除不存在的对象返回 (False, 文件本就不存在)",
       ok is False and reason == "文件本就不存在", f"{ok} / {reason!r}")

ok, reason = store.delete(KEY)
report("删除存在的对象返回 (True, '')", ok is True and reason == "", f"{ok} / {reason!r}")
report("对象确实不在了", not store.exists(KEY))

ok, reason = store.delete("../../etc/passwd")
report("越界删除被拒", ok is False and "越界" in reason, f"{ok} / {reason!r}")

# ── 4. 后端切换：get_store() 走配置 ────────────────────────────
print("\n=== 后端切换 ===")

reset_store()
settings.STORAGE_BACKEND = "minio"
settings.MINIO_ENDPOINT = endpoint
settings.MINIO_ACCESS_KEY = access
settings.MINIO_SECRET_KEY = secret
settings.MINIO_SECURE = False
settings.MINIO_REGION = "us-east-1"
report("配置切到 minio 后拿到 MinioFileStore",
       type(get_store()).__name__ == "MinioFileStore")

filestore.save_asset_file("live-task-003/switch.png", b"SWITCH", content_type="image/png")
report("便捷函数写入成功", filestore.asset_exists("live-task-003/switch.png"))
report("便捷函数回读一致",
       filestore.read_asset_bytes("live-task-003/switch.png") == b"SWITCH")
report("便捷函数删除成功", filestore.delete_asset_file("live-task-003/switch.png")[0] is True)

reset_store()
settings.STORAGE_BACKEND = "local"
report("切回 local 生效", type(get_store()).__name__ == "LocalFileStore")

# ── 5. 收尾 ────────────────────────────────────────────────────
for k in ("live-task-002/sub/b.png",):
    store.delete(k)
if stub is None:
    try:
        client.remove_bucket(settings.MINIO_BUCKET)
    except Exception:
        pass
else:
    stub.stop()

print("\n" + ("ALL OK" if ok_all else "HAS FAILURE"))
sys.exit(0 if ok_all else 1)
