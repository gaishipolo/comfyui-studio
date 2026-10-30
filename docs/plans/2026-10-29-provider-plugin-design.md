# 多引擎 Provider 插件机制设计（2026-10-29）

## 背景与决策历程

需求：多供应商引擎接入，核心难点是「不同供应商的接口参数怎么映射到统一表单」。

讨论中评估过两条路线，都被否了：

1. **配置化三层**（供应商/能力/模板全在界面配置，零代码接入）——否。四个真风险：长尾 API 配置搞不定（可灵 JWT 签发、阿里云 HMAC 签名、webhook 流程）、配置复杂度转移成「配置地狱」（管理员在界面写 JSONPath）、产物临时 URL 变死链、配置漂移影响历史任务。
2. **纯代码 + 我写好每家供应商**——否。写供应商不是平台方的事。

**最终形态：Provider 插件机制**。关键前提：**管理员 = 开发**，「接新供应商要写代码」不是代价。平台交付**一套标准 + 插件上传加载能力**，管理员按标准写 Provider 文件，界面上传，后端加载注册即生效——不改仓库、不发版。

## 一、标准契约（核心交付物）

`app/engines/base.py` 扩展：

```python
@dataclass
class FieldSpec:
    name: str            # API 参数名（表单字段名默认同名）
    label: str
    type: str            # string / int / float / bool / enum / image
    required: bool = False
    default: Any = None
    options: list[str] = field(default_factory=list)  # enum 选项，值=API 原生值
    description: str = ""

@dataclass
class CapabilitySpec:
    kind: str            # text_to_image / image_to_image / text_to_video ...
    label: str
    fields: list[FieldSpec]

class GenerationProvider(ABC):
    name: str = ""               # 注册名（唯一）
    label: str = ""              # 展示名
    capabilities: dict[str, CapabilitySpec] = {}
    config_schema: list[FieldSpec] = []   # 需要的配置项（base_url/api_key/...）

    def __init__(self, config: dict | None = None): ...
    def build_payload(self, capability: str, params: dict) -> dict: ...  # 表单→请求体
    async def submit(self, ctx: dict, params: dict) -> str: ...
    async def get_status(...) / download_output(...) / upload_input(...) / cancel(...) / health_check(...)
```

设计要点：

- **值形态差异在 FieldSpec.options 消化**（`size` 配成枚举 `512*512/1024*1024`，表单渲染下拉，选出的值即 API 原生值）；实在要转换，`build_payload` 里几行 Python 搞定——**不需要任何映射表达式**。
- **`config_schema` 配置声明与注入**：Provider 声明需要哪些配置（base_url/api_key/secret_key），上传后界面渲染配置表单，**值存 DB 不进代码文件**，换 key 不用重新传代码；实例化时注入 `config`。
- **认证自由**：`_auth()` 写在类里，Bearer / JWT 签发 / HMAC 签名都是几行代码——配置化卡死的地方在纯代码下全部有解。
- `submit(ctx, params)` 的 ctx 由 task_service 统一组装：`{"_type", "_capability", "_workflow"(原始工作流), "_param_mapping"}`。ComfyUI 在 submit 内部做 `inject_params`（isinstance 特判从 task_service 消掉）。

## 二、插件生命周期

1. **上传**：管理页「引擎插件」传 `.py` → `POST /api/plugins/upload`（admin only）
2. **静态校验**：`compile()` 语法检查 → AST 依赖白名单（stdlib + httpx + `app.engines.base`，白名单外拒绝加载）→ 动态加载后校验：是 `GenerationProvider` 子类、`name` 非空且不撞内置（comfyui/generic_api）、`capabilities` 非空、抽象方法齐全
3. **落盘 + 注册**：存 `backend/plugins/` 目录 + DB 元数据（`engine_plugins` 表：name/filename/sha256/enabled/last_error/config/uploaded_by）→ `factory.register_provider(name, cls)`
4. **持久化**：启动时扫描 `plugins/` 重新加载（重启不丢），加载失败写 `last_error` 并跳过——**坏插件只影响自己，绝不拖垮主服务**
5. **启停 / 替换 / 删除**：`enabled` 开关清实例缓存；重新上传同名 = 升级；删除 = 注销 + 删文件

## 三、模板联动

- 模板 `engine` 下拉 = 内置 + 已加载插件（数据源 `GET /api/plugins`），加 `capability` 列（迁移走 `_COLUMN_MIGRATIONS`）
- 选引擎后读 `capabilities` 渲染**推荐字段清单**，admin 一键带入 `input_schema`（改 label/默认值/排序照旧）
- 表单字段名默认 = API 参数名（值域对齐，映射自动成立）

## 四、运行时链路

`get_provider(template.engine)`（按 DB 配置实例化）→ `build_payload` → `submit` → 轮询 `get_status` → `download_output` → **产物立即落 filestore**（供应商 URL 多为 24h 临时签名，不落盘素材库全是死链）→ 素材库。提交时**请求体快照**存 `result['_request']`（审计 + 重跑依据，不受后续配置/代码变更影响）。

## 五、安全边界（丑话）

- **上传 = 服务端执行任意 Python**，等于把服务器交给上传者。信任模型成立（admin=开发），但功能必须只给 admin + 上传审计留痕（uploaded_by/sha256）
- 异常隔离：加载 try/except 包住，单插件失败不影响主服务
- 依赖白名单：挡掉随手 import 未安装包导致的加载崩溃（非安全沙箱——文档注明插件拥有服务进程全部权限）
- 热更新语义：更新后新任务用新代码，跑着的任务继续用旧实例

## 六、测试计划

`verify_plugin_loader.py`：合法插件加载注册 / 语法错 / 非子类 / 依赖越界 / 撞内置名 → 各拒绝且服务无恙；配置注入生效；启停删除；member 403；示例插件经本地假 HTTP 服务器跑通 submit→status→download→Asset 全链路。全量回归 + 前端构建。

## 七、非目标

- 不做沙箱/容器级隔离（admin 信任模型，文档注明即可）
- 不做多进程配置广播（当前单进程 uvicorn）
- 不写任何真实供应商的 Provider（示例用假想通用 REST 供应商）
- 不做 webhook/SSE 异步回调（先覆盖提交-轮询-下载三步式 + 同步式）

## 交付清单

标准文档 + 示例 Provider 模板文件、插件加载器、插件管理 API、引擎插件管理页、模板能力声明联动、测试。
