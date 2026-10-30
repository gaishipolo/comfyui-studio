"""引擎插件加载器 —— 上传的 Provider 代码：校验、动态加载、注册

安全边界（丑话说在前面）：
- **上传 = 服务端执行任意 Python**，等于把服务器交给上传者。功能只给 admin，
  上传留审计（uploaded_by / sha256）。admin=开发的信任模型下成立。
- 不是沙箱：插件拥有服务进程的全部权限（文档已注明）。
- 依赖白名单只是防「随手 import 没装的包」把加载搞崩，不是安全防线。
- **异常隔离**：单个插件加载失败只影响它自己，绝不拖垮主服务。
"""
import ast
import importlib.util
import inspect
import sys
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path

from app.engines.base import CapabilitySpec, FieldSpec, GenerationProvider
from app.engines.factory import register_provider

# 插件落盘目录（backend/plugins/），启动时扫描恢复
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins"

# 依赖白名单：标准库 + httpx + 本项目（拿引擎基类）。白名单外静态拒绝
_ALLOWED_MODULES = set(sys.stdlib_module_names) | {"httpx", "app"}

# 内置引擎注册名，插件不许撞
_RESERVED_NAMES = {"comfyui", "generic_api"}


class PluginError(Exception):
    """插件校验/加载失败。message 面向管理员，直接展示到界面"""


def _check_dependencies(tree: ast.AST) -> None:
    """AST 扫 import，白名单外的模块加载前就拒（SyntaxError 同理提前拒）"""
    bad: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _ALLOWED_MODULES:
                    bad.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                bad.add("相对导入（插件是单文件，不允许 from . import）")
            elif node.module and node.module.split(".")[0] not in _ALLOWED_MODULES:
                bad.add(node.module)
    if bad:
        raise PluginError(
            f"依赖不在白名单（只允许标准库 / httpx / app.engines.base）: {sorted(bad)}"
        )


def _find_provider_cls(module) -> type[GenerationProvider]:
    """在插件模块里找 GenerationProvider 子类 —— 只认本文件定义的类"""
    found = []
    for _name, obj in vars(module).items():
        if (
            isinstance(obj, type)
            and issubclass(obj, GenerationProvider)
            and obj is not GenerationProvider
            and not inspect.isabstract(obj)
            and obj.__module__ == module.__name__
        ):
            found.append(obj)
    if not found:
        raise PluginError("文件里没找到 GenerationProvider 子类（类要在本文件里定义，不能只 import）")
    if len(found) > 1:
        raise PluginError(f"一个插件文件只允许一个 Provider 子类，发现多个: {[c.__name__ for c in found]}")
    return found[0]


def _validate(cls: type[GenerationProvider]) -> None:
    """校验类声明 —— 元数据不齐就拒，别让半成品进注册表"""
    name = getattr(cls, "name", "")
    if not name:
        raise PluginError("Provider.name 不能为空（这是引擎注册名）")
    if name in _RESERVED_NAMES:
        raise PluginError(f"注册名「{name}」与内置引擎冲突，换一个")
    caps = getattr(cls, "capabilities", None)
    if not caps:
        raise PluginError("Provider.capabilities 不能为空（至少声明一个能力）")
    if not isinstance(caps, dict) or not all(isinstance(v, CapabilitySpec) for v in caps.values()):
        raise PluginError("capabilities 必须是 {kind: CapabilitySpec} 结构")
    schema = getattr(cls, "config_schema", [])
    if not all(isinstance(f, FieldSpec) for f in schema):
        raise PluginError("config_schema 必须是 list[FieldSpec]")


def _field_dict(f) -> dict:
    if is_dataclass(f) and not isinstance(f, type):
        return asdict(f)
    raise PluginError(f"字段声明必须是 FieldSpec 实例，拿到: {f!r}")


def describe_provider(cls: type[GenerationProvider]) -> dict:
    """类声明 → 可 JSON 化的描述（插件管理页 / 模板配置读）"""
    return {
        "name": cls.name,
        "label": getattr(cls, "label", "") or cls.name,
        "capabilities": {k: asdict(v) for k, v in (cls.capabilities or {}).items()},
        "config_schema": [_field_dict(f) for f in (getattr(cls, "config_schema", []) or [])],
    }


def load_plugin_file(path: Path) -> dict:
    """加载单个插件文件：语法 → 依赖白名单 → 动态 import → 找子类 → 校验 → 注册。

    成功返回 describe_provider(cls)；失败抛 PluginError（文案面向管理员）。
    注册名即插件里声明的 Provider.name —— 同名重传 = 升级覆盖。
    """
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PluginError(f"读文件失败: {e}") from e

    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        raise PluginError(f"语法错误（第 {e.lineno} 行）: {e.msg}") from e
    _check_dependencies(tree)

    # 模块名带 uuid：同名文件重传时干净覆盖，不撞 sys.modules 缓存
    module_name = f"engine_plugin_{path.stem}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError("无法为该文件创建加载器")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 —— 插件里的任何炸法都收成可读报错
        sys.modules.pop(module_name, None)
        raise PluginError(f"代码执行失败: {type(e).__name__}: {e}") from e

    cls = _find_provider_cls(module)
    _validate(cls)
    register_provider(cls.name, cls)
    return describe_provider(cls)


def load_all_plugins() -> list[dict]:
    """启动时扫描 plugins/ 目录恢复加载（重启不丢）。

    单个插件失败跳过并记进结果 —— 坏插件不能拖垮主服务。
    返回 [{file, ok, name|error, ...}]，调用方负责把 DB 配置注入 set_engine_config。
    """
    results: list[dict] = []
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        try:
            info = load_plugin_file(path)
            results.append({"file": path.name, "ok": True, **info})
        except PluginError as e:
            results.append({"file": path.name, "ok": False, "error": str(e)})
    return results
