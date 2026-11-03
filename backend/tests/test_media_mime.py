"""媒体类型判定（app/media.py）—— 离线，纯函数

两件事要保证：

1. 插件 provider 的产物常是无扩展名的 CDN URL，扩展名猜不出时必须靠文件头，
   否则视频落盘成 application/octet-stream，前端当图片渲染 → 一片空白。

2. MIME 推断必须与平台无关。mimetypes 读的是**平台自己**的数据库（Windows 读
   注册表、Linux 读 /etc/mime.types），同一个扩展名两个平台结论不同：
   wav 在 Windows 是 audio/wav、Linux 是 audio/x-wav；更糟的是 m4a/flac/ogg/mkv
   在 Linux 上根本猜不出来（None），掉到嗅探后 m4a 被判成 video/mp4 ——
   前端按 mime.split('/')[0] 分流，音频被渲染成 <video>，声音就播不出来。
   这个差异只在 Linux（也就是 Docker 部署）上出现，本地 Windows 全绿，
   所以下面的断言必须在这里钉死。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.media import guess_mime  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8
M4A = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 8
WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 4
MP3 = b"ID3\x03\x00" + b"\x00" * 8


def test_extension_wins_over_bytes() -> None:
    """有扩展名就用扩展名，不看字节 —— 扩展名是文件名契约，比内容可信。"""
    assert guess_mime("a.png", MP4) == "image/png"
    assert guess_mime("a.mp4") == "video/mp4"
    assert guess_mime("a.webm") == "video/webm"


def test_sniff_without_extension() -> None:
    """无扩展名：看文件头（插件 CDN URL 的场景）。"""
    for label, data, expect in [
        ("PNG", PNG, "image/png"),
        ("JPEG", JPG, "image/jpeg"),
        ("GIF", GIF, "image/gif"),
        ("WEBP", WEBP, "image/webp"),
        ("MP4", MP4, "video/mp4"),
        ("MOV", MOV, "video/quicktime"),
        ("WEBM", WEBM, "video/webm"),
        ("WAV", WAV, "audio/wav"),
        ("MP3", MP3, "audio/mpeg"),
    ]:
        got = guess_mime("abc123", data)
        assert got == expect, f"无扩展名 {label} → {got}，应为 {expect}"


def test_fallback_to_octet_stream() -> None:
    """猜不出的兜底。"""
    assert guess_mime("abc123", b"") == "application/octet-stream"
    assert guess_mime("abc123", b"\x00\x01\x02\x03") == "application/octet-stream"
    assert guess_mime("abc123") == "application/octet-stream"
    assert guess_mime("abc123", None) == "application/octet-stream"


def test_query_fragment_and_case() -> None:
    """插件给的 URL 常带签名参数；扩展名大小写也要认。"""
    assert guess_mime("x.png?token=1") == "image/png"
    assert guess_mime("x.mp4#frag") == "video/mp4"
    assert guess_mime("X.MP4") == "video/mp4"
    assert guess_mime("", MP4) == "video/mp4"


def test_mime_is_platform_independent() -> None:
    """关键扩展名的 MIME 不能随平台变。

    这组值曾经随平台漂：Windows 读注册表给 audio/wav、Linux 读
    /etc/mime.types 给 audio/x-wav；m4a/flac/ogg/mkv 在 Linux 上压根猜不出。
    现在由 app/media.py 的显式表钉死，两边必须完全一致。
    """
    expected = {
        # 音频
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "opus": "audio/opus",
        # 视频
        "mp4": "video/mp4",
        "m4v": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "mkv": "video/x-matroska",
        # 图片
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "svg": "image/svg+xml",
    }
    for ext, want in expected.items():
        got = guess_mime(f"sample.{ext}")
        assert got == want, f".{ext} 推成 {got}，应为 {want}"


def test_audio_stays_in_audio_bucket() -> None:
    """前端按 mime.split('/')[0] 分流 img / video / audio。

    音频一旦被推成 video/*，前端就渲染成 <video> 标签，音频文件能显示但
    播不出声音 —— m4a 在 Linux 上就踩过这个坑。
    """
    for ext in ("wav", "m4a", "mp3", "flac", "ogg", "opus", "aac"):
        mime = guess_mime(f"sample.{ext}")
        assert mime.split("/")[0] == "audio", f".{ext} → {mime}，会被渲染成 <video>"


def test_m4a_brand_sniffed_without_extension() -> None:
    """没有扩展名时，ISO BMFF 的 major brand 能区分 m4a 音频与 mp4 视频。"""
    assert guess_mime("abc123", M4A) == "audio/mp4"
    assert guess_mime("abc123", MP4) == "video/mp4"


def test_declared_mime_is_last_resort() -> None:
    """客户端声明的 MIME 只在扩展名和文件头都指不出方向时才用。

    很多客户端对未知类型一律报 application/octet-stream，那没有信息量，
    不能让它盖过文件头的判断。
    """
    unknown = b"\x00\x01\x02\x03"
    assert guess_mime("blob", unknown, declared="audio/mpeg") == "audio/mpeg"
    assert guess_mime("blob", unknown, declared="application/octet-stream") == (
        "application/octet-stream"
    )
    assert guess_mime("blob", unknown, declared="audio/mpeg; charset=binary") == "audio/mpeg"
    # 有文件头时以文件头为准，不信声明
    assert guess_mime("blob", WAV, declared="application/octet-stream") == "audio/wav"


def main() -> int:
    tests = [
        test_extension_wins_over_bytes,
        test_sniff_without_extension,
        test_fallback_to_octet_stream,
        test_query_fragment_and_case,
        test_mime_is_platform_independent,
        test_audio_stays_in_audio_bucket,
        test_m4a_brand_sniffed_without_extension,
        test_declared_mime_is_last_resort,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
