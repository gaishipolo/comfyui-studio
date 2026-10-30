"""媒体类型判定（app/media.py）—— 离线，纯函数

重点：插件 provider 的产物常是无扩展名的 CDN URL，扩展名猜不出时必须靠文件头，
否则视频落盘成 application/octet-stream，前端当图片渲染 → 一片空白。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.media import guess_mime  # noqa: E402

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 8
WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 4
MP3 = b"ID3\x03\x00" + b"\x00" * 8

print("\n[1] 有扩展名：直接用扩展名")
report("a.png → image/png", guess_mime("a.png", MP4) == "image/png", "扩展名优先，不看字节")
report("a.mp4 → video/mp4", guess_mime("a.mp4") == "video/mp4")
report("a.webm → video/webm", guess_mime("a.webm") == "video/webm")

print("\n[2] 无扩展名：看文件头（插件 CDN URL 的场景）")
for label, data, expect in [
    ("PNG", PNG, "image/png"), ("JPEG", JPG, "image/jpeg"), ("GIF", GIF, "image/gif"),
    ("WEBP", WEBP, "image/webp"), ("MP4", MP4, "video/mp4"), ("MOV", MOV, "video/quicktime"),
    ("WEBM", WEBM, "video/webm"), ("WAV", WAV, "audio/wav"), ("MP3", MP3, "audio/mpeg"),
]:
    got = guess_mime("abc123", data)
    report(f"无扩展名 {label} → {expect}", got == expect, got)

print("\n[3] 猜不出的兜底")
report("空字节 → octet-stream", guess_mime("abc123", b"") == "application/octet-stream")
report("未知字节 → octet-stream", guess_mime("abc123", b"\x00\x01\x02\x03") == "application/octet-stream")
report("无扩展名且无字节 → octet-stream", guess_mime("abc123") == "application/octet-stream")
report("data 为 None 不炸", guess_mime("abc123", None) == "application/octet-stream")

print("\n[4] 边界")
report("扩展名带 query 残留（x.png?token=1）", guess_mime("x.png?token=1") == "image/png",
       guess_mime("x.png?token=1"))
report("大写扩展名", guess_mime("X.MP4") == "video/mp4")
report("空文件名 + MP4 字节", guess_mime("", MP4) == "video/mp4")

print()
print("全部通过" if ok_all else "存在失败项")
sys.exit(0 if ok_all else 1)
