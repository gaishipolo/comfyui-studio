"""上传种类（kind）验证：image / audio / video 走同一个入口，各自校验

背景：上传接口原来只收图片（写死 ALLOWED_EXT），所以「参考音频」只能退化成手填 URL
的文本框，插件被迫用 audio/audio2/audio3 三个槽位硬凑列表。现在按 kind 放行。

覆盖：
- kind=audio 传 mp3 → 200，返回 wbinput: 短标记
- kind=image 传 png → 200
- kind=audio 传 png → 400（扩展名不属于音频）
- kind=pdf → 400（未知种类）
- 老入口 /upload/image 仍可用（兼容）

需要后端在 127.0.0.1:8000 运行。
"""
import base64
import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000/api"
ok_all = True

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
# 一个只有 ID3 头的最小 mp3（内容不重要，接口只校验扩展名/大小）
TINY_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 64


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def post_json(path, data):
    r = urllib.request.Request(BASE + path, method="POST")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, json.dumps(data).encode()) as resp:
        return json.loads(resp.read())


def upload(path, token, filename, data, extra=""):
    boundary = "----wb" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + path + extra, method="POST", data=body)
    r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


T = post_json("/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
ENGINE = "grsai_minimax_h3"  # 这个插件实现了 upload_input（图片+音频都收）

print("\n[1] 按 kind 上传")
st, data = upload("/upload/file", T, "ref.mp3", TINY_MP3, f"?engine={ENGINE}&kind=audio")
report("音频 200", st == 200, f"HTTP {st} {str(data)[:80]}")
report("返回 wbinput: 短标记", str(data.get("filename", "")).startswith("wbinput:"), str(data.get("filename"))[:40])
report("回显 kind", data.get("kind") == "audio", str(data.get("kind")))

st, data = upload("/upload/file", T, "ref.png", TINY_PNG, f"?engine={ENGINE}&kind=image")
report("图片 200", st == 200, f"HTTP {st}")

print("\n[2] 种类校验")
st, data = upload("/upload/file", T, "ref.png", TINY_PNG, f"?engine={ENGINE}&kind=audio")
report("图片冒充音频 → 400", st == 400, f"HTTP {st} {data.get('detail', '')[:40]}")
st, data = upload("/upload/file", T, "ref.mp3", TINY_MP3, f"?engine={ENGINE}&kind=pdf")
report("未知种类 → 400", st == 400, f"HTTP {st} {data.get('detail', '')[:40]}")
st, data = upload("/upload/file", T, "empty.mp3", b"", f"?engine={ENGINE}&kind=audio")
report("空文件 → 400", st == 400, f"HTTP {st}")

print("\n[3] 兼容老入口")
st, data = upload("/upload/image", T, "old.png", TINY_PNG, f"?engine={ENGINE}")
report("/upload/image 仍可用", st == 200, f"HTTP {st}")

print()
print("全部通过" if ok_all else "存在失败项")
sys.exit(0 if ok_all else 1)
