"""健康检查 + 引擎信息"""
import asyncio
import re
import time

from fastapi import APIRouter, Depends, Query

from app.auth import get_current_user
from app.engines import factory
from app.engines.base import GenerationProvider
from app.engines.factory import get_provider, list_engines
from app.models.models import User

router = APIRouter(prefix="/api", tags=["health"])

# 探活是给监控用的，不能因为下游引擎卡住就一起卡住。
# 单引擎封顶 2 秒 —— 探活失败要快，慢等于没探。
_PROBE_TIMEOUT = 2.0

# 结果缓存多久。**必须有**：前端每 30 秒轮询一次，而插件的 health_check 是
# 真实的上游 HTTP 请求（打各家的 /v1/api/result 之类）。没有缓存的话，
# N 个在线用户 = 每分钟 N×6 次对供应商的探测，白烧配额还可能触发限流。
# 20 秒 < 客户端 30 秒轮询间隔，用户感知不到延迟。
_CACHE_TTL = 20.0

# 异常原文里常带内网地址，有些供应商还会把 token 放在 query 里
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")

# engine_name -> (过期时刻, 探测结果)；_inflight 让并发请求共享同一次探测
_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, asyncio.Future] = {}


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
        # 插件没实现 health_check 时基类返回 None —— 那是「不知道」，
        # 不能当成「离线」，否则每个没写的插件都会天天误报
        if type(provider).health_check is GenerationProvider.health_check:
            return {"name": engine_name, "status": "unknown", "detail": "该引擎未实现健康检查"}
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


async def _probe_cached(engine_name: str, fresh: bool = False) -> dict:
    """带缓存的探测。并发请求共享同一次探测（inflight 去重），
    避免十个用户同时刷新时打出十份上游请求。"""
    if not fresh:
        hit = _cache.get(engine_name)
        if hit and hit[0] > time.monotonic():
            return hit[1]

    task = _inflight.get(engine_name)
    if task is None or task.done():
        task = asyncio.ensure_future(_probe(engine_name))
        _inflight[engine_name] = task

    # shield：某个调用方被取消（客户端断开）不该把这次探测一起取消
    result = await asyncio.shield(task)
    _cache[engine_name] = (time.monotonic() + _CACHE_TTL, result)
    return result


def invalidate_cache(engine_name: str | None = None):
    """配置变了/插件换了要让缓存立刻失效 ——
    否则管理员改完 API Key，界面上还要「离线」20 秒。"""
    if engine_name is None:
        _cache.clear()
    else:
        _cache.pop(engine_name, None)


# 引擎注册表一变就清缓存（插件上传/改配置/启停/删除都会触发）
factory.on_change(invalidate_cache)


@router.get("/health")
async def health():
    """探活接口（匿名可访问）。

    只承诺「服务活着」—— 引擎状态是附加信息，引擎卡住不能拖垮探活，
    所以有 2 秒上限，超时按 disconnected 报。
    """
    comfyui = await _probe_cached("comfyui")
    return {"status": "ok", "comfyui": comfyui["status"]}


@router.get("/engines")
async def engines(
    user: User = Depends(get_current_user),
    fresh: bool = Query(False, description="跳过缓存立即探测（管理页改完配置后用）"),
):
    """列出已注册引擎及其健康状态。

    登录用户都能看 —— 生成页要据此提示「你选的引擎离线了」，
    不然用户只会看到任务莫名失败。

    但**异常详情只给 admin**：里面可能有内网地址、上游报错细节，
    普通用户不需要知道服务端连的是哪台机器、报了什么错。

    并发探测 + 20 秒缓存：6 个引擎串行最坏要 6×2 秒，而前端每 30 秒就会问一次，
    不缓存等于让每个在线用户都在替所有用户重复探测上游。
    """
    results = await asyncio.gather(
        *(_probe_cached(name, fresh) for name in list_engines())
    )
    if user.role == "admin":
        return results
    return [{"name": r["name"], "status": r["status"]} for r in results]
