"""引擎工厂 — 根据配置返回对应的 Provider 实例

新增引擎（插件方式）只需：
1. 按 docs/provider-dev-guide.md 写 GenerationProvider 子类
2. 从管理页上传 —— 插件加载器校验后调 register_provider 注册

引擎运行配置（base_url/api_key 等）不走这里读数据库（工厂是同步的，
DB 是异步的）——由插件管理 API 在加载/改配置时 set_engine_config 注入内存。
"""
import asyncio
import logging
from dataclasses import dataclass

from app.engines.base import GenerationProvider
from app.engines.comfyui import ComfyUIProvider

_providers: dict[str, GenerationProvider] = {}
_engine_configs: dict[str, dict] = {}
_CLOSE_TIMEOUT_SECONDS = 10.0
_LEASE_DRAIN_TIMEOUT_SECONDS = 5.0
_retire_tasks: set[asyncio.Task[None]] = set()
_pending_retirements: dict[int, GenerationProvider] = {}


@dataclass
class _ProviderState:
    provider: GenerationProvider
    leases: int = 0
    retired: bool = False
    closing: bool = False
    closed: bool = False
    lease_zero: asyncio.Event | None = None


_provider_states: dict[int, _ProviderState] = {}

log = logging.getLogger(__name__)


async def _close_provider(provider: GenerationProvider) -> None:
    """Close one Provider without allowing cleanup failure to escape."""
    provider_name = provider.name or type(provider).__name__
    try:
        await asyncio.wait_for(provider.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning(
            "Provider %s close timed out after %.1f seconds",
            provider_name,
            _CLOSE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- cleanup failure must not undo registry changes
        log.exception("Provider %s close failed", provider_name)


def _state_for(provider: GenerationProvider) -> _ProviderState:
    state = _provider_states.get(id(provider))
    if state is None:
        state = _ProviderState(provider=provider)
        _provider_states[id(provider)] = state
    return state


def _close_done(state: _ProviderState, task: asyncio.Task[None]) -> None:
    _retire_tasks.discard(task)
    state.closing = False
    state.closed = True
    _pending_retirements.pop(id(state.provider), None)
    if state.retired and state.leases == 0:
        _provider_states.pop(id(state.provider), None)


def _schedule_close(state: _ProviderState) -> None:
    if state.leases or state.closing or state.closed:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _pending_retirements[id(state.provider)] = state.provider
        return
    _pending_retirements.pop(id(state.provider), None)
    state.closing = True
    task = loop.create_task(_close_provider(state.provider))
    _retire_tasks.add(task)
    task.add_done_callback(lambda completed: _close_done(state, completed))


def _retire(provider: GenerationProvider) -> None:
    """旧实例退役：关掉它持有的资源（httpx 连接池 / WS 重连协程）。

    工厂刻意保持同步（见模块注释），而 close() 是 async —— 用 create_task
    挂到当前事件循环里异步关。调用点全在 async 端点/启动流程里，必有
    运行中的循环；在同步上下文调用时先保留实例，统一由 shutdown 清理。
    关闭失败不影响注册/注销主流程。

    不关的后果：ComfyUI 的 ProgressTracker 是无限重连循环，每次插件
    升级/停用/删除/改配置都会泄漏一条 WS 协程 + 一个连接池，累积无上限。
    """
    state = _state_for(provider)
    state.retired = True
    _schedule_close(state)


class ProviderLease:
    """One active use of a concrete Provider instance."""

    def __init__(self, state: _ProviderState):
        self._state = state
        self._released = False

    @property
    def provider(self) -> GenerationProvider:
        return self._state.provider

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        state = self._state
        if state.leases <= 0:
            raise RuntimeError("Provider lease released without acquisition")
        state.leases -= 1
        if state.leases == 0:
            if state.lease_zero is not None:
                state.lease_zero.set()
            if state.retired:
                _schedule_close(state)

    def __enter__(self) -> GenerationProvider:
        if self._released:
            raise RuntimeError("Provider lease is already released")
        return self.provider

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._release()

    async def __aenter__(self) -> GenerationProvider:
        return self.__enter__()

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self._release()

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
            log.exception("Provider registry change hook failed: %r", cb)

# 引擎注册表：engine_name → Provider 类
# 内置只有 ComfyUI；其他引擎一律走插件机制（上传 .py 注册），见
# docs/provider-dev-guide.md。早期那个 generic_api 通用适配器已移除 ——
# 它靠「路径模板 + 字段名透传」适配，表达力不够（multipart / Base64 /
# 列表参数都写不出来），已被插件机制取代。
_REGISTRY: dict[str, type[GenerationProvider]] = {
    "comfyui": ComfyUIProvider,
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
        provider = _REGISTRY[engine_name](config=_engine_configs.get(engine_name, {}))
        _providers[engine_name] = provider
        _state_for(provider)
    return _providers[engine_name]


def acquire_provider(engine_name: str = "comfyui") -> ProviderLease:
    """Atomically resolve and retain the current concrete Provider instance."""
    provider = get_provider(engine_name)
    state = _state_for(provider)
    if state.retired or state.closed:
        raise RuntimeError(f"Provider is no longer available: {engine_name}")
    state.leases += 1
    if state.lease_zero is not None and state.lease_zero.is_set():
        state.lease_zero.clear()
    return ProviderLease(state)


def list_engines() -> list[str]:
    return list(_REGISTRY.keys())


async def close_all_providers(
    *,
    lease_timeout: float = _LEASE_DRAIN_TIMEOUT_SECONDS,
) -> None:
    """进程退出前的统一清理（lifespan 关闭段调用）。

    活着的实例和已经退役但还没关完的实例并发清理。"""
    providers_by_id = {id(provider): provider for provider in _providers.values()}
    providers_by_id.update(_pending_retirements)
    _providers.clear()
    for provider in providers_by_id.values():
        _state_for(provider).retired = True
    for state in list(_provider_states.values()):
        state.retired = True

    loop = asyncio.get_running_loop()
    lease_deadline = loop.time() + max(0.0, float(lease_timeout))
    while True:
        states = [state for state in _provider_states.values() if not state.closed]
        if not states:
            _pending_retirements.clear()
            return

        leased = [state for state in states if state.leases]
        if leased:
            remaining = lease_deadline - loop.time()
            if remaining <= 0:
                log.warning(
                    "Provider lease drain timed out after %.1f seconds; "
                    "leaving %d active provider(s) for process exit",
                    lease_timeout,
                    len(leased),
                )
                return
            waits = []
            for state in leased:
                if state.lease_zero is None:
                    state.lease_zero = asyncio.Event()
                waits.append(state.lease_zero.wait())
            try:
                await asyncio.wait_for(asyncio.gather(*waits), timeout=remaining)
            except TimeoutError:
                log.warning(
                    "Provider lease drain timed out after %.1f seconds; "
                    "leaving %d active provider(s) for process exit",
                    lease_timeout,
                    len(leased),
                )
                return
            continue

        for state in states:
            _schedule_close(state)
        retire_tasks = list(_retire_tasks)
        if retire_tasks:
            await asyncio.gather(*retire_tasks)
            await asyncio.sleep(0)
            continue

        # No active lease and no close task means a sync-context pending state
        # was just transferred to this running loop; retry scheduling once.
        await asyncio.sleep(0)
