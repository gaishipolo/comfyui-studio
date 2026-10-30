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

`FieldSpec` 字段：`name`（=API 参数名）、`label`、`type`、`required`、`default`、`options`（enum 选项，**值直接用 API 原生值**）、`description`、`multiple`、`advanced`。

### 两个正交的维度：元素类型 vs 基数

| 维度 | 属性 | 取值 | 决定什么 |
|---|---|---|---|
| 元素类型 | `type` | `string` / `int` / `float` / `bool` / `enum` / `image` / `audio` / `video` | 单个值长什么样 |
| 基数 | `multiple` | `true` / `false` | 这个参数收几个值 |

**两者互不影响**：音频列表、参考图列表、多值枚举、多行文本都能表达。

`type=image/audio/video` 是**文件类**，表单渲染成上传控件，放行的文件种类由 type 决定（音频框里选不到图片）；其余是**标量类**，渲染成输入框 / 下拉。

### 列表参数（`multiple=True`）

API 收数组时，**不要把数组拆成 image / image2 / audio1 / audio2 / audio3 多个字段** —— 那是把平台的表达力缺口转嫁给用户。声明一次：

```python
FieldSpec(name="image", label="参考图", type="image", required=True, multiple=True,
          description="可一次选多张，按选择顺序提交")
FieldSpec(name="audios", label="参考音频", type="audio", multiple=True,
          description="可一次选多段")
```

界面表现：文件类列表是**一个上传框**（可多选、条目可单个删、音频带播放器便于确认）；标量类列表是**可增删的多行输入**（`select` 则是多选勾选组）。

**表单值形态**：列表字段是数组 `["wbinput:xxx.png", "wbinput:yyy.mp3"]`，单值字段是字符串。插件侧统一按列表消费，别假设一定是列表——历史任务里可能是单值：

```python
def _resolve_images(self, value) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    return [r for r in (self._resolve_image(v) for v in raw) if r]
```

语义分工：`image_to_image`（单张参考图）不开 multiple，图生视频 / 多图合成这类天生多值的才开。

### 上传产物：`wbinput:` 短标记

`upload_input` 返回什么，表单值就存什么。但**别把 base64 直接当表单值返回** —— 表单值会进 `task.params`，而任务列表每次轮询都返回它，几 MB 的 base64 会把轮询响应撑爆。

平台约定：`upload_input` 把文件落到自己的存储，返回 `wbinput:<key>` 短标记；`build_payload` 里再用 `read_asset_bytes(key)` 读回来，转成 API 要的形态（data URI / base64 / 公网 URL）。参考实现见 `examples/provider_grsai_minimax_h3.py`。

`upload_input` 会被**图片、音频、视频**共用（取决于字段的 type），按扩展名分流存储目录与 MIME：

```python
async def upload_input(self, data: bytes, filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in AUDIO_EXT:
        folder, fallback = "my-audio-input", "audio/mpeg"
    elif ext in VIDEO_EXT:
        folder, fallback = "my-video-input", "video/mp4"
    else:
        folder, fallback = "my-image-input", "image/png"
    ...
    return f"wbinput:{key}"
```

### bool 字段：别用 `bool()` 转换

表单传回来的可能是 `True` / `"true"` / `"false"` / `1` / `0`（历史模板里存过字符串选项）。`bool("false") is True` —— 直接 `bool()` 会把「关」当成「开」。用显式转换：

```python
@staticmethod
def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "是"):
        return True
    if s in ("0", "false", "no", "off", "否"):
        return False
    raise ValueError(f"只接受 true/false，收到：{value}")
```

`type=bool` 的字段会自动渲染成「是 / 否」下拉（选项是真布尔），不用自己声明 `options`。

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
| `upload_input(data, filename) -> str` | （可选）上传参考文件（图片/音频/视频共用）：返回 API 要的形态。建议返回 `wbinput:<key>` 短标记，见上文 |
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
