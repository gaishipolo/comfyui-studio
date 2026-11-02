"""Content-Disposition 下载文件名的唯一实现（RFC 5987）。

文件名来自数据库（ComfyUI 产物前缀用户可控，中文很常见），**绝不能裸拼进
header**：非 latin-1 字符会让 h11 的 header 编码抛 UnicodeEncodeError → 500，
含引号/换行/分号则产生畸形 header。统一输出：

    attachment; filename="<ASCII 兜底>"; filename*=UTF-8''<百分号编码>

`filename*` 是权威值（现代浏览器认它），`filename=` 是 ASCII 兜底（老客户端）。
"""
import re
from urllib.parse import quote

# ASCII 兜底里只保留这些字符，其余（引号/分号/反斜杠/控制符…）一律替换成 _
_FALLBACK_SAFE = re.compile(r"[^A-Za-z0-9._ -]")


def attachment_disposition(filename: str) -> str:
    """生成永远 latin-1 可编码、无注入面的 attachment header 值。"""
    # 默认 safe="/"：字母数字与 ._ - ~ 原样保留，其余（含非 ASCII、引号、
    # 换行、冒号）全部百分号编码 —— 文件名不含 /，safe 取值无差别
    star = quote(filename)
    fallback = filename.encode("ascii", "ignore").decode("ascii")
    fallback = _FALLBACK_SAFE.sub("_", fallback).strip() or "download"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{star}'
