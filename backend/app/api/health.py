"""健康检查 + 引擎信息"""
import asyncio
import re

from fastapi import APIRouter, Depends

from app.auth import require_admin
from app.engines.factory import get_provider, list_engines
from app.models.models import User

router = APIRouter(prefix="/api", tags=["health"])

# 探活是给监控用的，不能因为下游引擎卡住就一起卡住。
# 单引擎封顶 2 秒 —— 探活失败要快，慢等于没探。
_PROBE_TIMEOUT = 2.0

# 异常原文里常带内网地址，有些供应商还会把 token 放在 query 里
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")


def _safe_detail(exc: Exception) -> str:
    """异常信息脱敏：抹掉 URL，截断长度。别把内网拓扑和密钥片段送到前端"""
    msg = _URL_RE.sub("<url>", str(exc)).strip()
    name = type(exc).__name__
    return f"{name}: {msg}"[:200] if msg else name


async def _probe(engine_name: str) -> dict:
    """探测单个引擎。任何异常都收成结果字段，绝不向上抛 ——
    一个引擎坏了不该让整个探活接口 500。"""
    try:
        provider = get_provider(engine_name)
        ok = await asyncio.wait_for(provider.health_check(), timeout=_PROBE_TIMEOUT)
        return {"name": engine_name, "status": "connected" if ok else "disconnected"}
    except asyncio.TimeoutError:
        return {
            "name": engine_name,
            "status": "disconnected",
            "detail": f"探测超时（>{_PROBE_TIMEOUT:g}s）",
        }
    except Exception as e:  # noqa: BLE001
        return {"name": engine_name, "status": "error", "detail": _safe_detail(e)}


@router.get("/health")
async def health():
    """探活接口（匿名可访问）。

    只承诺「服务活着」—— 引擎状态是附加信息，引擎卡住不能拖垮探活，
    所以有 2 秒上限，超时按 disconnected 报。
    """
    comfyui = await _probe("comfyui")
    return {"status": "ok", "comfyui": comfyui["status"]}


@router.get("/engines")
async def engines(user: User = Depends(require_admin)):
    """列出已注册引擎及其健康状态。

    运维诊断用，回执里带异常详情（已脱敏），因此只给 admin ——
    普通用户不需要知道服务端连的是哪台机器、报了什么错。
    并发探测：6 个引擎串行最坏要 6×2 秒，探活不该排这么长的队。
    """
    return await asyncio.gather(*(_probe(name) for name in list_engines()))
