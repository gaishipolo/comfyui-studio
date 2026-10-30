"""健康检查 + 引擎信息"""
from fastapi import APIRouter
from app.engines.factory import get_provider, list_engines

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    provider = get_provider("comfyui")
    comfyui_ok = await provider.health_check()
    return {
        "status": "ok",
        "comfyui": "connected" if comfyui_ok else "disconnected",
    }


@router.get("/engines")
async def engines():
    """列出已注册的生成引擎及其健康状态"""
    result = []
    for name in list_engines():
        try:
            provider = get_provider(name)
            ok = await provider.health_check()
            result.append({"name": name, "status": "connected" if ok else "disconnected"})
        except Exception as e:
            result.append({"name": name, "status": "error", "detail": str(e)})
    return result
