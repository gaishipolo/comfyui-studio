"""引擎工厂 — 根据配置返回对应的 Provider 实例

新增引擎（插件方式）只需：
1. 按 docs/provider-dev-guide.md 写 GenerationProvider 子类
2. 从管理页上传 —— 插件加载器校验后调 register_provider 注册

引擎运行配置（base_url/api_key 等）不走这里读数据库（工厂是同步的，
DB 是异步的）——由插件管理 API 在加载/改配置时 set_engine_config 注入内存。
"""
from app.engines.base import GenerationProvider
from app.engines.comfyui import ComfyUIProvider
from app.engines.generic_api import GenericAPIProvider

_providers: dict[str, GenerationProvider] = {}
_engine_configs: dict[str, dict] = {}

# 引擎注册表：engine_name → Provider 类
_REGISTRY: dict[str, type[GenerationProvider]] = {
    "comfyui": ComfyUIProvider,
    "generic_api": GenericAPIProvider,
}


def register_provider(engine_name: str, provider_cls: type[GenerationProvider]):
    """动态注册新引擎"""
    _REGISTRY[engine_name] = provider_cls
    _providers.pop(engine_name, None)  # 清除旧实例缓存


def unregister_provider(engine_name: str):
    """注销引擎（插件删除/停用时调用）"""
    _REGISTRY.pop(engine_name, None)
    _providers.pop(engine_name, None)
    _engine_configs.pop(engine_name, None)


def set_engine_config(engine_name: str, config: dict | None):
    """注入/更新引擎运行配置，配置变了让实例缓存失效（下次重新实例化）"""
    _engine_configs[engine_name] = dict(config or {})
    _providers.pop(engine_name, None)


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
