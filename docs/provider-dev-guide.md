# Provider 插件开发规范

给管理员（=开发）看的接入指南。接一家新供应商 = 按本规范写一个 Python 文件，从管理页上传，即刻生效——不改仓库、不发版。

## 最小骨架

```python
from app.engines.base import (
    CapabilitySpec, FieldSpec, GenerationProvider, GenerationResult, TaskStatus,
)

class MyProvider(GenerationProvider):
    name = "my_cloud"            # 注册名（唯一，小写字母/数字/下划线/连字符）
    label = "我的云"              # 展示名

    capabilities = {             # 「我会什么」—— 建模板时界面读它生成表单
        "text_to_image": CapabilitySpec(
            kind="text_to_image", label="文生图",
            fields=[FieldSpec(name="prompt", label="提示词", type="string", required=True)],
        ),
    }
    config_schema = [            # 「我需要什么配置」—— 界面渲染配置表单
        FieldSpec(name="base_url", label="API 地址", type="string", required=True),
        FieldSpec(name="api_key", label="API 密钥", type="string", required=True),
    ]

    async def submit(self, ctx, params) -> str: ...
    async def get_status(self, task_id) -> GenerationResult: ...
    async def download_output(self, filename, subfolder="", output_type="output") -> bytes: ...
    async def health_check(self) -> bool: ...
```

完整可跑样例：`backend/examples/provider_template.py`（假想的「示例云」REST API，带全部注释）。

## 类声明

| 属性 | 说明 |
|---|---|
| `name` | 引擎注册名，模板的 engine 字段引用它。**不许用** `comfyui` / `generic_api`（内置保留） |
| `label` | 界面展示名 |
| `capabilities` | `{kind: CapabilitySpec}`。kind 用 `text_to_image` / `image_to_image` / `text_to_video` 这类蛇形命名 |
| `config_schema` | `list[FieldSpec]`。运行配置项声明，**机密放这里，不要写死在代码里** |

`FieldSpec` 字段：`name`（=API 参数名）、`label`、`type`（string/int/float/bool/enum/image）、`required`、`default`、`options`（enum 选项，**值直接用 API 原生值**）、`description`。

### 值形态差异怎么处理

优先用 `options` 枚举对齐：API 要 `size="1024*1024"` 字符串，就把 size 声明成 enum，选项 `["512*512", "768*768", "1024*1024"]`——表单渲染下拉，用户选出来的值就是 API 原生值，零转换。

需要拼接/改名/嵌套时重写 `build_payload(capability, params) -> dict`，几行 Python 搞定。默认实现是原样透传 `params`。

## 方法契约

| 方法 | 说明 |
|---|---|
| `submit(ctx, params) -> str` | 提交任务，返回引擎侧任务 ID。`ctx` 由平台组装：`_type`（场景分类）、`_capability`（能力 kind）、`_template_name`、`_workflow`（工作流 JSON，非 ComfyUI 忽略）、`_param_mapping` |
| `get_status(task_id) -> GenerationResult` | 轮询状态。完成时把产物放进 `outputs`：`[{"url": 产物URL, "filename": 纯文件名, "subfolder": "", "type": "output"}]` |
| `download_output(filename, subfolder, output_type) -> bytes` | 下载产物。**支持 filename 是完整 URL**（平台优先传 outputs 里的 url 字段） |
| `health_check() -> bool` | 连通性检查（管理页「测试」按钮） |
| `upload_input(data, filename) -> str` | （可选）图生图传图：上传图片返回 API 要的形态（URL/base64/文件 id）。默认不支持 |
| `cancel(provider_task_id) -> bool` | （可选）取消引擎侧任务 |
| `build_payload(capability, params) -> dict` | （钩子）表单值 → 请求体，默认透传 |

## 产物约定（重要）

`get_status` 返回的产物放 `url` + `filename`（纯文件名）两个字段。平台拿 `url` 调 `download_output` **立即下载落自己的存储**再进素材库——供应商返回的多是 24 小时过期的临时签名 URL，直接存进素材库第二天全是死链。

## 请求体快照

提交时把实际发出的 payload 存进 `self.last_request = payload`，平台会记进任务记录（审计 + 重跑依据）。别把整个大对象（如完整工作流）塞进去。

## 上传与加载规则

- 管理页「引擎插件」上传 `.py`（≤512KB），**同名重传 = 升级覆盖**
- 一个文件只允许一个 Provider 子类，类必须在本文件定义（不能只 import）
- 依赖白名单：**标准库 + httpx + app.engines.base**（及 `app.*`），白名单外直接拒
- 语法错误 / 加载失败会把报错详情显示在界面上，不影响其他插件和主服务
- 启停、删除都在管理页；配置（base_url/api_key）也在管理页填，改配置不用重传代码

## 安全边界（丑话）

**上传 = 在服务端执行任意 Python**，插件拥有服务进程的全部权限（读库、读文件、外发请求都行）。这个功能只给 admin，上传留审计（谁传的、代码 sha256）。不是沙箱——别把上传能力给不受信任的人。

## 自测建议

写完先本地冒烟：

```python
# 起个假 API（或者直接打真实测试环境）跑一遍
result = await provider.get_status(await provider.submit({"_capability": "text_to_image"}, {"prompt": "test"}))
assert result.status == TaskStatus.DONE and result.outputs
```

再上传到平台：管理页「测试」连通 → 建模板（选引擎/能力，一键带入字段）→ 跑一个真任务看产物落进素材库。
