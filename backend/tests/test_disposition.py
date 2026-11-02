"""I2 回归：Content-Disposition 下载文件名 header 的统一实现。

事故场景：minio 后端下载时手工拼 f'attachment; filename="{safe_name}"'，
文件名来自库里（用户可控前缀，中文很常见）——非 latin-1 字符让 uvicorn/h11
的 header 编码抛 UnicodeEncodeError → 每次下载 500；含引号/换行则畸形 header。
"""
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.disposition import attachment_disposition  # noqa: E402


def test_ascii_name_has_plain_fallback():
    header = attachment_disposition("result.png")
    assert header.startswith("attachment;"), header
    assert 'filename="result.png"' in header, header
    assert "filename*=UTF-8''result.png" in header, header


def test_non_ascii_name_uses_rfc5987_and_round_trips():
    header = attachment_disposition("输出图-v2.png")
    assert "filename*=UTF-8''" in header, header
    star = header.split("filename*=UTF-8''", 1)[1]
    assert unquote(star) == "输出图-v2.png"
    # ASCII 兜底不能丢（老客户端只认 filename=）
    assert 'filename="' in header, header


def test_latin1_only_name_does_not_raise_and_is_encoded():
    header = attachment_disposition("café.png")
    star = header.split("filename*=UTF-8''", 1)[1]
    assert star == "caf%C3%A9.png", header
    assert unquote(star) == "café.png"
    header.encode("latin-1")  # 不抛 = 服务器能安全发出


def test_header_is_always_latin1_encodable():
    for name in ("输出图.png", "café.png", 'a"b.png', "x\ny.png", "中文 空格 .mp4"):
        attachment_disposition(name).encode("latin-1")


def test_quote_and_newline_cannot_break_header():
    evil = 'a"b\r\nX-Injected: 1.png'
    header = attachment_disposition(evil)
    # 整行 header：不允许出现真实换行/回车（只能以百分号编码形式存在）
    assert "\r" not in header, repr(header)
    assert "\n" not in header, repr(header)
    # filename="..." 兜底里只允许两个引号（成对包裹），注入的引号必须被中和
    fallback = header.split("; filename=", 1)[1].split(";", 1)[0]
    assert fallback.count('"') == 2, repr(header)
    # filename* 里注入内容全部百分号编码
    star = header.split("filename*=UTF-8''", 1)[1]
    assert '"' not in star and "\r" not in star and "\n" not in star, repr(star)


def test_empty_fallback_still_yields_valid_header():
    header = attachment_disposition("　")  # 全角空格：ASCII 兜底剥空
    assert "filename=" in header, header
    header.encode("latin-1")


def main() -> int:
    tests = [
        test_ascii_name_has_plain_fallback,
        test_non_ascii_name_uses_rfc5987_and_round_trips,
        test_latin1_only_name_does_not_raise_and_is_encoded,
        test_header_is_always_latin1_encodable,
        test_quote_and_newline_cannot_break_header,
        test_empty_fallback_still_yields_valid_header,
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
