"""存储抽象验证 —— 本地磁盘 / MinIO 可切换。

背景：设计文档写的是 MinIO，实现走的是本地 `settings.STORAGE_DIR`，且有 4 处
业务代码绕过 `app/filestore.py` 直接摸磁盘（`_save_asset` 直写、`FileResponse`
要本地路径、ZIP 批量下载 `read_bytes()`、`from-asset` 上传 `asset_disk_path()`）。
一上对象存储这四处全断。

本脚本不连真 MinIO（用假客户端验证我们对 minio SDK 的调用形状），
另外起真实容器的联调放在 verify_filestore_live.py。
"""
import io
import inspect
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app import filestore
from app.filestore import (
    LocalFileStore,
    MinioFileStore,
    ObjectHandle,
    get_store,
    reset_store,
    safe_key,
)

FileStoreUnavailableError = getattr(
    filestore,
    "FileStoreUnavailableError",
    RuntimeError,
)

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# 用临时目录当 STORAGE_DIR，不污染真实 storage/
_TMP = Path(tempfile.mkdtemp(prefix="filestore-test-"))
_ORIG_STOR = settings.STORAGE_DIR
settings.STORAGE_DIR = _TMP
reset_store()


class FakeS3:
    """形状对齐 minio.Minio 的最小假客户端。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.stat_errors: dict[str, Exception] = {}
        self.remove_errors: dict[str, Exception] = {}

    def put_object(self, bucket, key, data, length, content_type="application/octet-stream", **kw):
        self.calls.append(("put", bucket, key, content_type))
        self.objects[key] = data.read()
        self.types[key] = content_type

    def stat_object(self, bucket, key):
        self.calls.append(("stat", bucket, key))
        if key in self.stat_errors:
            raise self.stat_errors[key]
        if key not in self.objects:
            raise FileNotFoundError(key)
        obj = type("S", (), {})()
        obj.size = len(self.objects[key])
        return obj

    def get_object(self, bucket, key):
        self.calls.append(("get", bucket, key))
        if key not in self.objects:
            raise RuntimeError("NoSuchKey")
        body = self.objects[key]

        class R:
            def __init__(self):
                self.buf = io.BytesIO(body)

            def read(self, n=-1):
                return self.buf.read(n)

            def close(self):
                pass

            def release_conn(self):
                pass

        return R()

    def remove_object(self, bucket, key):
        self.calls.append(("remove", bucket, key))
        if key in self.remove_errors:
            raise self.remove_errors[key]
        self.objects.pop(key, None)


class FakeS3Error(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.messages.append(record.getMessage())
        self.records.append(record)


# ── 1. safe_key：归一 + 越界拒绝 ──────────────────────────────────
print("=== safe_key 归一与越界防护 ===")

report("正常键原样通过", safe_key("abc/file.png") == "abc/file.png")
report("反斜杠归一成 /", safe_key("abc\\file.png") == "abc/file.png")
report("混用分隔符也归一", safe_key("abc\\d/e.png") == "abc/d/e.png")
report("尾斜杠被收掉", safe_key("abc/file.png/") == "abc/file.png")

report(".. 段被拒", safe_key("a/../../etc/passwd") is None)
report("纯 .. 被拒", safe_key("..") is None)
report("反斜杠的 .. 也被拒", safe_key("a\\..\\..\\b") is None)
report("绝对路径被拒", safe_key("/etc/passwd") is None)
report("盘符被拒", safe_key("C:/windows/system32") is None)
report("盘符反斜杠被拒", safe_key("C:\\windows") is None)
report("空串被拒", safe_key("") is None)
report("纯空白被拒", safe_key("   ") is None)
report("非字符串被拒", safe_key(None) is None)
report("只剩点号被拒", safe_key("././.") is None)

# ── 2. LocalFileStore 回环 ───────────────────────────────────────
print("\n=== LocalFileStore 读写回环 ===")

local = LocalFileStore()
KEY = "t1/out.mp4"

local.save(KEY, b"hello-video-bytes")
report("exists 为真", local.exists(KEY))
report("read_bytes 回读一致", local.read_bytes(KEY) == b"hello-video-bytes")

handle = local.open(KEY)
report("open 拿到句柄", handle is not None)
report("size 正确", handle is not None and handle.size == len(b"hello-video-bytes"))
report("本地后端给出 local_path（可走 FileResponse 拿 Range）",
       handle is not None and handle.local_path is not None)

streamed = b"".join(handle.iter_bytes())
report("iter_bytes 整段一致", streamed == b"hello-video-bytes")
report("iter_bytes 分块大小正确（不按行切）",
       all(len(c) <= 256 * 1024 for c in handle.iter_bytes()))

# 二进制里带换行/无换行都不能被切错 —— 这是按行迭代的老毛病
blob = bytes(range(256)) * 1024      # 256KB，含 \n 且无 \n 的段很长
local.save("t1/blob.bin", blob)
h2 = local.open("t1/blob.bin")
report("含换行的二进制整段无损", b"".join(h2.iter_bytes()) == blob)
h2.close()

# 同名不同目录
local.save("t1/a.png", b"1")
local.save("t2/a.png", b"2")
report("同名不同目录互不覆盖",
       local.read_bytes("t1/a.png") == b"1" and local.read_bytes("t2/a.png") == b"2")

# ── 3. 老记录兼容：反斜杠写入的 file_path 仍能读到 ────────────────
print("\n=== 老记录兼容（Windows 反斜杠 file_path） ===")

local.save("old1/f.png", b"legacy")
report("反斜杠键读到同一份文件", local.read_bytes("old1\\f.png") == b"legacy")
report("反斜杠键 exists", local.exists("old1\\f.png"))
report("反斜杠键 open 正常", local.open("old1\\f.png") is not None)


class FailingLocalPath:
    def is_file(self):
        return True

    def stat(self):
        raise PermissionError("local stat denied")


failing_local = LocalFileStore()
failing_local._resolve = lambda _path: FailingLocalPath()
try:
    failing_local.open("t1/denied.bin")
except FileStoreUnavailableError as exc:
    report("本地 stat 操作失败抛 typed unavailable", "local stat denied" not in str(exc), str(exc))
else:
    report("本地 stat 操作失败抛 typed unavailable", False, "没有抛 typed error")

resolve_failure = LocalFileStore()
resolve_failure._resolve = lambda _path: (_ for _ in ()).throw(
    OSError("local resolve unavailable")
)
try:
    resolve_failure.open("t1/resolve.bin")
except FileStoreUnavailableError as exc:
    report("本地路径解析失败抛 typed unavailable", True, str(exc))
except OSError as exc:
    report("本地路径解析失败抛 typed unavailable", False, f"raw {exc!r}")
else:
    report("本地路径解析失败抛 typed unavailable", False, "没有抛 typed error")

# ── 4. 删除语义 ─────────────────────────────────────────────────
print("\n=== 删除语义（删不掉不能让主流程失败） ===")

local.save("d1/f.png", b"x")
ok, reason = local.delete("d1/f.png")
report("删掉返回 (True, '')", ok is True and reason == "", f"{ok} / {reason!r}")
report("删完确实不在了", not local.exists("d1/f.png"))

ok, reason = local.delete("d1/f.png")
report("重复删除返回 (False, 文件本就不存在)", ok is False and reason == "文件本就不存在",
       f"{ok} / {reason!r}")

ok, reason = local.delete("../../etc/passwd")
report("越界删除返回 (False, 路径越界)", ok is False and "越界" in reason, f"{ok} / {reason!r}")

# ── 5. 越界写入被拒 ─────────────────────────────────────────────
print("\n=== 越界写入被拒 ===")

try:
    local.save("../evil.png", b"x")
    report("越界 save 抛 ValueError", False, "居然没抛")
except ValueError as exc:
    report("越界 save 抛 ValueError", True, str(exc))

report("越界 read_bytes 返回 None", local.read_bytes("../evil.png") is None)
report("越界 open 返回 None", local.open("../evil.png") is None)
report("越界 exists 返回 False", local.exists("../evil.png") is False)

# 确认没写到 TMP 之外
outside = _TMP.parent / "evil.png"
report("目录外没有产生文件", not outside.exists())

# ── 6. MinioFileStore（假客户端，验证调用形状） ──────────────────
print("\n=== MinioFileStore 调用形状 ===")

fake = FakeS3()
mstore = MinioFileStore(client=fake)

mstore.save("t9/v.mp4", b"video-bytes", content_type="video/mp4")
report("put_object 被调用", any(c[0] == "put" for c in fake.calls))
report("对象键是 POSIX 形式", "t9/v.mp4" in fake.objects)
report("content_type 存到了对象上", fake.types.get("t9/v.mp4") == "video/mp4")
report("bucket 用配置值", all(c[1] == settings.MINIO_BUCKET for c in fake.calls))

report("exists 为真", mstore.exists("t9/v.mp4"))
report("exists 缺失返回 False", mstore.exists("t9/nope.mp4") is False)
report("read_bytes 回读一致", mstore.read_bytes("t9/v.mp4") == b"video-bytes")

mh = mstore.open("t9/v.mp4")
report("open 拿到句柄", mh is not None)
report("对象存储没有 local_path（必须走流式）", mh is not None and mh.local_path is None)
report("size 从 stat_object 来", mh is not None and mh.size == len(b"video-bytes"))
report("iter_bytes 流式一致", mh is not None and b"".join(mh.iter_bytes()) == b"video-bytes")
mh.close()

open_capture = LogCapture()
old_level = filestore.logger.level
old_propagate = filestore.logger.propagate
filestore.logger.addHandler(open_capture)
filestore.logger.setLevel(logging.WARNING)
filestore.logger.propagate = False
try:
    fake.stat_errors["t9/open-private"] = FakeS3Error(
        "AccessDenied",
        "invalid open credentials",
    )
    fake.stat_errors["t9/open-network"] = ConnectionError("open network unavailable")
    open_errors = []
    for key in ("t9/open-private", "t9/open-network"):
        try:
            mstore.open(key)
        except FileStoreUnavailableError as exc:
            open_errors.append((key, exc))
finally:
    filestore.logger.removeHandler(open_capture)
    filestore.logger.setLevel(old_level)
    filestore.logger.propagate = old_propagate

report("MinIO open 鉴权/网络失败抛 typed unavailable",
       len(open_errors) == 2 and all(str(exc) == "文件存储服务暂时不可用" for _, exc in open_errors),
       repr(open_errors))
open_logs = "\n".join(
    logging.Formatter("%(levelname)s %(message)s").format(record)
    for record in open_capture.records
)
report("MinIO open 操作失败记录服务端上下文",
       any("open-private" in message and "AccessDenied" in message for message in open_capture.messages)
       and any("open-network" in message for message in open_capture.messages)
       and "invalid open credentials" in open_logs
       and "open network unavailable" in open_logs,
       open_logs)

report("越界 open 返回 None", mstore.open("../x") is None)
try:
    mstore.save("../x", b"y")
    report("越界 save 抛 ValueError", False, "居然没抛")
except ValueError:
    report("越界 save 抛 ValueError", True)

before = len(fake.calls)
ok, reason = mstore.delete("t9/never-existed")
report("删除不存在的对象返回 (False, 文件本就不存在)",
       ok is False and reason == "文件本就不存在", f"{ok} / {reason!r}")
report("对象不存在时不会白发 remove_object",
       not any(c[0] == "remove" for c in fake.calls[before:]))

for missing_code in ("NoSuchKey", "NoSuchObject"):
    key = f"t9/{missing_code}"
    fake.stat_errors[key] = FakeS3Error(missing_code, "missing")
    ok, reason = mstore.delete(key)
    report(f"{missing_code} 明确映射为文件不存在",
           ok is False and reason == "文件本就不存在", f"{ok} / {reason!r}")

capture = LogCapture()
old_level = filestore.logger.level
old_propagate = filestore.logger.propagate
filestore.logger.addHandler(capture)
filestore.logger.setLevel(logging.WARNING)
filestore.logger.propagate = False
try:
    fake.stat_errors["t9/private"] = FakeS3Error("AccessDenied", "invalid credentials")
    auth_ok, auth_reason = mstore.delete("t9/private")
    fake.stat_errors["t9/network"] = ConnectionError("network unavailable")
    network_ok, network_reason = mstore.delete("t9/network")
finally:
    filestore.logger.removeHandler(capture)
    filestore.logger.setLevel(old_level)
    filestore.logger.propagate = old_propagate

report("对象存储鉴权失败不会伪装成文件不存在",
       auth_ok is False and auth_reason != "文件本就不存在" and "AccessDenied" in auth_reason,
       repr(auth_reason))
report("对象存储网络失败返回可诊断原因",
       network_ok is False and network_reason != "文件本就不存在" and "network unavailable" in network_reason,
       repr(network_reason))
report("对象存储 stat 失败会记录日志",
       any("private" in message and "AccessDenied" in message for message in capture.messages)
       and any("network" in message and "network unavailable" in message for message in capture.messages),
       repr(capture.messages))

fake.objects["t9/disappeared"] = b"racy"
fake.remove_errors["t9/disappeared"] = FakeS3Error("NoSuchKey", "gone after stat")
ok, reason = mstore.delete("t9/disappeared")
report("stat 后消失的对象仍按文件不存在处理",
       ok is False and reason == "文件本就不存在", f"{ok} / {reason!r}")

ok, reason = mstore.delete("t9/v.mp4")
report("删除存在的对象返回 (True, '')", ok is True and reason == "", f"{ok} / {reason!r}")
report("对象确实被移除", "t9/v.mp4" not in fake.objects)

# ── 7. get_store 后端选择 ───────────────────────────────────────
print("\n=== get_store 后端选择 ===")

reset_store()
settings.STORAGE_BACKEND = "local"
report("local 后端选中", type(get_store()).__name__ == "LocalFileStore")

reset_store()
settings.STORAGE_BACKEND = "minio"
report("minio 后端选中", type(get_store()).__name__ == "MinioFileStore")

reset_store()
settings.STORAGE_BACKEND = "MINIO"
report("大小写不敏感", type(get_store()).__name__ == "MinioFileStore")

reset_store()
settings.STORAGE_BACKEND = "bogus"
try:
    get_store()
    report("未知后端报错", False, "居然没抛")
except ValueError as exc:
    report("未知后端报错", "bogus" in str(exc), str(exc))

reset_store()
settings.STORAGE_BACKEND = "local"
report("默认后端是 local", type(get_store()).__name__ == "LocalFileStore")

# ── 8. 回归守卫 ─────────────────────────────────────────────────
print("\n=== 回归守卫 ===")

app_root = Path(__file__).resolve().parents[1] / "app"
py_sources = {p.name: p.read_text(encoding="utf-8") for p in app_root.rglob("*.py")}

leaks = [
    name for name, src in py_sources.items()
    if name != "config.py" and name != "filestore.py" and "STORAGE_DIR" in src
]
report("业务代码不再直接摸 STORAGE_DIR", not leaks, f"{leaks}")

hits = [name for name, src in py_sources.items() if "asset_disk_path" in src]
report("asset_disk_path 已彻底移除（换成 safe_key）", not hits, f"{hits}")

ts_src = py_sources.get("task_service.py", "")
report("_save_asset 的存储键用 POSIX 分隔符", 'f"{task.id}/{safe_name}"' in ts_src)
report("_save_asset 不再自己 mkdir / write_bytes", "write_bytes" not in ts_src)

# 素材文件的读取/下载已从 tasks.py 抽到 asset_media.py（素材可脱离任务独立存在，
# 路由也搬到了 /api/assets/{id}/file|thumb），守卫跟着搬。
media_src = py_sources.get("asset_media.py", "")
report("下载接口不再假设本地路径", "asset_disk_path" not in media_src)
report("下载接口保留 FileResponse 分支（本地走 Range）", "FileResponse(" in media_src)
report("下载接口有流式兜底", "StreamingResponse(" in media_src)

fs_src = py_sources.get("filestore.py", "")
report("filestore 里 minio 是懒导入（本地模式不被依赖绑住）",
       "from minio import Minio" in fs_src and fs_src.index("def ") < fs_src.index("from minio import Minio"))

# 收尾：还原配置，删临时目录
settings.STORAGE_DIR = _ORIG_STOR
reset_store()
shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + ("ALL OK" if ok_all else "HAS FAILURE"))
sys.exit(0 if ok_all else 1)
