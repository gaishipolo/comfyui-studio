"""引擎工厂 — 根据配置返回对应的 Provider 实例

新增引擎（插件方式）只需：
1. 按 docs/provider-dev-guide.md 写 GenerationProvider 子类
2. 从管理页上传 —— 插件加载器校验后调 register_provider 注册

引擎运行配置（base_url/api_key 等）不走这里读数据库（工厂是同步的，
DB 是异步的）——由插件管理 API 在加载/改配置时 set_engine_config 注入内存。
"""
import asyncio
import logging

from app.engines.base import GenerationProvider
from app.engines.comfyui import ComfyUIProvider
from app.engines.generic_api import GenericAPIProvider

_providers: dict[str, GenerationProvider] = {}
_engine_configs: dict[str, dict] = {}
_CLOSE_TIMEOUT_SECONDS = 10.0
_retire_tasks: set[asyncio.Task[None]] = set()

log = logging.getLogger(__name__)


async def _close_provider(provider: GenerationProvider) -> None:
    """Close one Provider without allowing cleanup failure to escape."""
    try:
        await asyncio.wait_for(provider.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning(
            "Provider %s close timed out after %.1f seconds",
            type(provider).__name__,
            _CLOSE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- cleanup failure must not undo registry changes
        log.exception("Provider %s close failed", type(provider).__name__)


def _retire(provider: GenerationProvider) -> None:
    """旧实例退役：关掉它持有的资源（httpx 连接池 / WS 重连协程）。

    工厂刻意保持同步（见模块注释），而 close() 是 async —— 用 create_task
    挂到当前事件循环里异步关。调用点全在 async 端点/启动流程里，必有
    运行中的循环；万一在同步上下文调（离线测试），退化为直接丢弃，
    进程退出时自然回收。关闭失败不影响注册/注销主流程。

    不关的后果：ComfyUI 的 ProgressTracker 是无限重连循环，每次插件
    升级/停用/删除/改配置都会泄漏一条 WS 协程 + 一个连接池，累积无上限。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_close_provider(provider))
    _retire_tasks.add(task)
    task.add_done_callback(_retire_tasks.discard)

# 引擎注册表变更（注册/注销/改配置）时的回调。
# 目前只有健康检查的缓存挂在这里 —— 管理员改完 API Key，界面上不该还显示
# 「离线」等缓存过期。做成回调而不是让 plugins.py 逐个调用点去清，
# 是因为调用点有六七个，将来新增一个忘了清就是一次「明明配好了却报错」。
_change_hooks: list = []


def on_change(callback) -> None:
    """注册「引擎注册表变了」的回调（同步、无参）"""
    if callback not in _change_hooks:
        _change_hooks.append(callback)


def _fire_change() -> None:
    for cb in list(_change_hooks):
        try:
            cb()
        except Exception:  # noqa: BLE001 —— 清理缓存失败不该影响注册本身
            pass

# 引擎注册表：engine_name → Provider 类
_REGISTRY: dict[str, type[GenerationProvider]] = {
    "comfyui": ComfyUIProvider,
    "generic_api": GenericAPIProvider,
}


def register_provider(engine_name: str, provider_cls: type[GenerationProvider]):
    """动态注册新引擎"""
    _REGISTRY[engine_name] = provider_cls
    old = _providers.pop(engine_name, None)
    if old is not None:
        _retire(old)
    _fire_change()


def unregister_provider(engine_name: str):
    """注销引擎（插件删除/停用时调用）"""
    _REGISTRY.pop(engine_name, None)
    old = _providers.pop(engine_name, None)
    _engine_configs.pop(engine_name, None)
    if old is not None:
        _retire(old)
    _fire_change()


def set_engine_config(engine_name: str, config: dict | None):
    """注入/更新引擎运行配置，配置变了让实例缓存失效（下次重新实例化）"""
    _engine_configs[engine_name] = dict(config or {})
    old = _providers.pop(engine_name, None)
    if old is not None:
        _retire(old)
    _fire_change()


def get_provider(engine_name: str = "comfyui") -> GenerationProvider:
    if engine_name not in _providers:
        if engine_name not in _REGISTRY:
            raise ValueError(
                f"Unknown engine: {engine_name}. Registered: {list(_REGISTRY.keys())}"
            )
        _providers[engine_name] = _REGISTRY[engine_name](config=_engine_configs.get(engine_name, {}))
    return _providers[engine_name]


def list_engines() -> list[str]:
    return list(_REGISTRY.keys())


async def close_all_providers() -> None:
    """进程退出前的统一清理（lifespan 关闭段调用）。

    活着的实例和已经退役但还没关完的实例并发清理。"""
    providers = list(_providers.values())
    retire_tasks = list(_retire_tasks)
    _providers.clear()
    await asyncio.gather(
        *(_close_provider(provider) for provider in providers),
        *retire_tasks,
    )
