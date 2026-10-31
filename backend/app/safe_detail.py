"""异常信息脱敏 —— API 响应的最后一道防线。

凡是把引擎/上游的异常原文回给前端的地方都必须过这里：
httpx 的连接错误通常带完整请求 URL，有些供应商还会把 token 放在 query 里，
内网拓扑和密钥片段不该送到浏览器。/engines 与 /upload 两条链路共用本实现，
别再各写一份（漂移一次就是一个泄露口）。
"""
import re

# 形如 scheme://... 的都抹掉（http/https/ws/任意自定义协议）
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_AUTHORIZATION_RE = re.compile(r"\bauthorization\s*:\s*[^\r\n]*", re.IGNORECASE)
_BEARER_RE = re.compile(
    r"\bbearer\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[-_]?key|token|secret|password)(\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)",
    re.IGNORECASE,
)


def safe_detail(exc: BaseException) -> str:
    """抹掉 URL 和凭证，截断长度。保留异常类型名方便排查。"""
    msg = _URL_RE.sub("<url>", str(exc))
    msg = _AUTHORIZATION_RE.sub("Authorization: <redacted>", msg)
    msg = _BEARER_RE.sub("Bearer <redacted>", msg)
    msg = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", msg).strip()
    name = type(exc).__name__
    return f"{name}: {msg}"[:200] if msg else name
