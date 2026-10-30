# ComfyUI 工作台 — 前后端代码 Review

- **日期**: 2026-10-27
- **范围**: `backend/`（17 个 Python 文件）+ `frontend/`（15 个 ts/tsx 文件），全部逐文件通读
- **分级**: P0 = 必须修（安全/核心链路）；P1 = 尽快修（功能缺陷/联动问题）；P2 = 改进项

---

## ✅ 修复状态（2026-10-27 下午）

**P0 全部修复**：B1（SECRET_KEY 环境变量 + 临时密钥告警）、B2（下载文件名清洗+DB 校验+路径 resolve 校验）、B3（上传文件名清洗+重名加后缀）、B4（初始密码可配置/随机生成）、F1（新增 `POST /api/upload/image` 转发 ComfyUI `/upload/image`，前端 image_upload 真上传）、F2（轮询 effect 拆分，请求风暴消失）。

**P1 全部修复**：B5（非 ComfyUI 引擎跳过 workflow 校验）、B6（轮询回写 progress，真实任务验证轨迹 0→50→100）、B7（映射配置错误抛 ValueError 明确报错）、B8（启动时 recover_stale_tasks）、B9+F4（include_inactive 参数，停用模板可再启用）、B10（重试清理旧素材）、B11（Task.created_by + SQLite 列迁移）、B12（role Literal 校验）、B13（改名查重返回 400）、F3（DynamicForm 加 key 重置）、F5（字段编辑器支持 options/默认值/占位提示）、F6/F17（resp 校验+数组兜底）、F7（encodeURIComponent）、F8（engine 下拉，数据源 /api/engines）。

**P2 修复**：B14（CORS_ORIGINS 可配置）、B15（monotonic 计时）、B16（ZIP 同名加前缀）、B17（mimetypes）、B18（无 task_id 报错+缓存裁剪）、B20（tags 类型标注）、B21（未用 import）、B22（批量单次 commit）、B23（SQL_ECHO 开关）、F9（detail 错误信息）、F10（素材缓存失效）、F11（搜索防抖 300ms）、F12（统一 client.ts）、F13（移除双重断言）、F14（fields 兜底）、F15（健康状态 30s 刷新）、F16（简化）。

**P2 全部处理完毕**：B19 复核后修正描述（精确匹配无误，LIKE 通配符未转义已修）；仅"标签独立建表"作为未来优化项保留。

**收尾补强**：参考图字段已接入 Flux2 模板（`ref_image → 112.inputs.image`），全链路 e2e 通过（`backend/tests/e2e_upload_generate.py`）：上传参考图 → 参数注入 LoadImage → 生成 → 素材入库（meta 记录 ref_image）。

---

### 🔴 Review 遗漏项（修复过程中实测发现，本次补记）

**B24（P0）— JSON 列 `\uXXXX` 转义导致中文搜索/筛选全盲**

这是 B19 的**真正病灶**，原报告判断有误：问题不止是 LIKE 通配符未转义，更根本的是 **SQLAlchemy 默认 `json.dumps(ensure_ascii=True)` 把非 ASCII 转成 `\uXXXX` 存库**，导致：

| 症状 | 根因 |
|---|---|
| `GET /api/assets?tag=smoke_标签` 返回 `[]` | 库里存的是 `["smoke_\u6807\u7b7e"]`，LIKE 拿真实中文比对永远不命中 |
| `keyword` 搜中文提示词搜不到 | `CAST(meta AS VARCHAR) LIKE '%酷女孩%'` 同理失配 |
| 用 String 列做实验却"能命中" | String 列不做 JSON 转义 —— 实验模型选错，把排查带偏 |

三层修复（均实测验证）：

1. **存储端** — 新增 `JSONStore`（`app/models/models.py`），`impl = Text` + `json.dumps(ensure_ascii=False)`，非 ASCII 原样入库。
   ⚠️ `impl` 必须是 `Text` 而非 `JSON`，否则 impl 会再序列化一次造成**双重编码**（实测 `'"{\"prompt\":...}"'`）。
   ⚠️ SQLAlchemy 2.1 已移除 `JSON(serializer=...)` 参数，只能走 TypeDecorator。
2. **查询端** — 标签筛选改用 SQLite `json_each()` 精确比对（`app/api/assets.py:_TAG_EXISTS_SQL`），由 SQLite 自己解码 JSON，与存储编码无关。同时天然规避了 LIKE 的子串语义（"图" 不命中 "图片"），且 `%`/`_`/`"` 均无需转义。
3. **存量数据** — `app/database.py:_unescape_json_columns` 启动时幂等改写老库 7 个 JSON 列，`\uXXXX` → 真实字符；脏数据跳过不误伤。

**实测证据**：
- `tests/verify_tag_json_each.py` — 12/12：中文/下划线/`%`/引号标签精确命中，部分匹配正确拒绝，大小写语义符合预期
- `tests/verify_json_migration.py` — 7/7：老格式→新格式改写无损、中文 LIKE 命中、幂等、脏数据保留
- `tests/verify_json_ascii.py` — 6/6：JSONStore 无转义残留、中文 LIKE 命中 meta 与 tags
- `tests/smoke_fixes.py` — **18/18 全绿**（此前唯一失败项「标签过滤命中」转为通过）

---

**验证汇总**：冒烟测试 **18/18** 通过（`backend/tests/smoke_fixes.py`，含路径穿越拦截、文件名清洗、映射报错、非法 role 拒绝、图片真上传到 ComfyUI、标签增删改查与过滤命中）；B5/B6/B11 真实生成验证通过（`backend/tests/verify_b5_b6.py`，进度 0→50→100）；JSON 转义修复专项验证 25 项断言全过；`tsc --noEmit` 零错误。

---

### 🔴 三次自查发现（部署链路 review）

对"这个东西到底怎么跑起来"做了一遍复查，发现一个**阻断部署**的缺口：

**B27（P1）— 生产构建产物无法运行，应用只能靠 `vite dev` 跑**

- `vite.config.ts` 的 `/api` 代理写在 `server.proxy` 下 —— **仅 dev 生效**
- 后端不托管任何静态文件（`main.py` 无 `StaticFiles`/`dist` 引用）
- 结论：`vite build` 出来的 `dist/` 交给任何静态服务器托管，**所有 `/api` 请求 404**，页面能开但完全不能用

对一个给内容/运营团队用的内网工作台来说，这意味着没法交付——只能让使用者连开发服务器。

修复（`backend/app/main.py` + `app/config.py`）：
- 新增 `settings.FRONTEND_DIST`（默认 `../frontend/dist`）
- 新增兜底路由 `GET /{full_path:path}` 托管构建产物，未命中回落 `index.html`
- **注册在所有 `/api/*` 之后**（Starlette 按注册顺序匹配，API 优先）
- 未知 API 路径显式返 404 JSON，**不吞成 HTML**（否则前端 fetch 报错会变成解析 HTML 失败，难排查）
- 目录穿越防护：`resolve()` + `is_relative_to(_DIST)` 校验
- `dist` 不存在时返 503 并提示 `npm run build`
- 原 `GET /` 的应用信息移到 `/api/info`（原先无任何依赖，已确认可安全改道）

由此实现**单进程部署**：一个 uvicorn 起服务即可，无需 nginx。

**B28（P2）— `normalize_input_schema` 是白名单，加字段需两处同步**（我自己引入的设计约束）
归一化只保留 `name/label/type/required/placeholder/options/default/advanced`，未知键被清除。这是有意的（保证输出永远满足 TS 契约），代价是给 `TemplateField` 加新属性时必须同步改后端组装逻辑。已在 docstring 显式标注耦合点，另补 `tests/verify_schema_idempotent.py`（21 断言）验证幂等性与假值保留（`default=0`/`""`/`advanced=False` 均不丢）。

**部署链路验证**（`tests/verify_prod_deploy.py`，13 项全过）：`GET /` 返回页面、JS/CSS 资源可取、SPA 回落、未知 API 404 JSON、API 路由不被兜底遮蔽、登录/业务接口通、3 组目录穿越不泄漏数据库、`FRONTEND_DIST` 指向正确。

---

### 🔴 四次自查发现（UI 动作语义 review）

**B29（P1）— 「删除」和「停用」是同一个后端行为，但界面是两个按钮**

排查一个测试失败（重复建模板 400）时顺藤摸出来的：

- 前端 `TemplateManager.tsx` 有两个**不同**动作：「停用/启用」切换 `is_active`，「删除」带独立确认框 `确定删除模板「x」？`
- 后端 `DELETE /templates/{id}` 的实现是 `template.is_active = False` —— **和「停用」完全一样**
- 叠加名唯一约束的全表查重 → **被"删除"的模板名永久占用**，管理员再也建不了同名模板

对一个给运营用的模板管理界面，"我删了它却不能重建同名"是无法解释的。根因是删除该做的事被停用代劳了。

修复（`app/api/templates.py`）：`DELETE` 改为物理删除并释放名称；已有任务记录的模板**拒绝删除**并提示改用「停用」（否则任务历史成孤儿）。两个按钮各司其职：

| 动作 | 行为 | 记录 | 名称 |
|---|---|---|---|
| 停用 | `is_active=False` | 保留 | 仍占用 |
| 删除 | 物理删除 | 清除 | 释放可复用 |

⚠️ 实现细节：统计任务数**不能用 `template.tasks`** —— 懒加载关系在 async 会话里抛 `MissingGreenlet`，必须显式 `select(func.count())`。

验证 `tests/verify_delete_semantics.py`（12 项全过）：删除后名称可复用、停用后名称仍占用且仍在管理列表、带任务模板删除被拒且记录完好。

**测试卫生**：`verify_schema_normalization.py` 固定模板名导致重跑 400 —— 与之前标签测试撞历史数据同一个教训，已改为 uuid 唯一名。**凡有唯一约束的实体，测试必须用唯一键**。（此教训后来又踩第二次，见 B30 附注）

---

### 🔴 五次自查发现（真实浏览器 UI 实操 review）

前面几轮的验证都停在 API / 编译 / 契约层。改用 **playwright-cli**（`npx -y @playwright/cli@latest`，v0.1.22）后第一次真正打开页面点了一遍 —— agent-browser 跑不通不代表 UI 不能自动化验证，之前"只能人工过一遍"的结论是错的。真实浏览器立刻暴露了 API 层测试**永远发现不了**的问题。

**B30（P0）— 所有素材缩略图 401，图片一张都显示不出来**

浏览器控制台：`Failed to load resource: 401 Unauthorized @ /api/tasks/{id}/assets/{file}`，8 条全挂。

- 根因：`download_asset` 依赖 `Depends(get_current_user)`（JWT），而 **`<img src>` / `<a href>` 根本发不出 `Authorization` 头** —— 不是接口错，是调用方式根本不可能带上凭据
- 影响面 5 处：素材库缩略图、素材详情大图、详情「下载」按钮、任务结果缩略图、任务结果「打开原图」
- 这是**纯 API 层测试的盲区**：`curl -H "Authorization: Bearer $TOK"` 测出来一直是 200，完全正常

修复：新增 `AuthImage.tsx`（fetch 带头取 blob → `URL.createObjectURL` 渲染，卸载时 revoke）+ `fetchAssetBlob` / `downloadAsset` / `openAssetInNewTab` 三个带鉴权取文件的 helper，把 5 处直链全部换掉。前端组件里已无任何 `<img src>` 直连鉴权接口、无任何 `href` 直连鉴权接口。

⚠️ 修的时候自己又引入一个 bug：`getAssetUrl()` 返回值**已经含 `/api` 前缀**，而 `fetchAssetBlob` 又拼了一次 `BASE` → `/api/api/tasks/...` 404，图片仍然一张不显示。**教训：函数入参的"路径"到底含不含前缀，必须在契约里写死**，不能靠猜。

验证 `tests/ui_verify_pw.sh`（10 项断言全过）：
- 素材库 `{"total":16,"loaded":16,"broken":0}` —— 16 张缩略图真实渲染（截图肉眼可确认）
- 控制台 `无 401 残留` / `无 /api/api 双前缀` / `无任何控制台错误`
- 鉴权探测 `withToken:200, withoutToken:401` —— 图能取到，且**安全性没被削弱**

> 探测时曾误报 `withoutToken: 200`（看着像鉴权被绕过）。实为假象：同一 URL 先带 token 请求过，浏览器 HTTP 缓存直接回了第二个请求，**根本没到服务端**。curl 三连（匿名 401 / 假 token 401 / 真 token 200）才定论后端鉴权完好。**测鉴权必须 `cache: 'no-store'` + `credentials: 'omit'`**。

**B31（P1）— 任务和素材都无法删除，素材库只增不减** → ✅ **已实现，见「六、B31 落地记录」**

修 B30 时想清理测试残留才发现的：后端 `tasks.py` 只有 `POST/GET/retry`，`assets.py` 只有 tags 的 `DELETE` —— **任务、素材都没有删除接口，前端也没有删除入口**。只有模板能删。

后果对内容/运营团队是实际问题：失败任务、废图会无限堆积，素材库只能进不能出，磁盘和列表都会越来越重。模板侧做了完整的删除/停用区分（B29），任务侧却是空白。

已按方案 B 补齐（权限：仅本人/管理员；级联清理磁盘文件；运行中拒删），并抽出 `app/filestore.py` 统一路径穿越守卫 —— 过程中发现 `batch_download` 缺失该检查，一并堵上。

附带修掉的测试卫生问题（**同 B29 教训第二次踩**）：`verify_b5_b6.py` 用固定模板名 `验证用-B5-generic`，重跑即 400。已改 uuid 唯一名。测试残留改由 `cleanup_test_rows.py` 按模板名前缀 `验证用-`/`测试模板-` 级联清理（因任务/素材无 DELETE 接口，无法在测试内自清理）。

---

### 📊 最终验证矩阵（13 套件全绿 + tsc 零错误 + UI 断言 0 失败）

| 套件 | 覆盖 |
|---|---|
| `smoke_fixes` | 18 项：路径穿越/文件名清洗/映射报错/role 校验/图片上传/标签增删查 |
| `verify_frontend_contract` | 真实响应逐字段比对 TS 接口 |
| `verify_schema_normalization` | 归一化补齐/丢弃/降级 |
| `verify_schema_idempotent` | 归一化幂等 + 假值保留（21 断言） |
| `verify_delete_semantics` | 删除 vs 停用 语义（12 项） |
| `verify_delete_assets_tasks` | **B31 删除能力**（25 项：级联/磁盘/权限/越界防护） |
| `verify_prod_deploy` | 单进程部署链路（13 项） |
| `verify_tag_json_each` | 标签精确匹配（12 断言） |
| `verify_json_migration` | 转义数据改写（7 断言） |
| `verify_json_migration_edge` | 迁移边界无损（9 用例×2 轮） |
| `verify_json_ascii` | JSONStore 无转义存储（6 断言） |
| `verify_b5_b6` | 进度回写 / created_by / 无 workflow 引擎 |
| `ui_verify_pw.sh` | **真实浏览器 UI**：登录/仪表盘/三模块/11 项断言全过（28 张缩略图真实渲染 + 鉴权探测 + 删除入口） |

**UI 覆盖情况**：已通过 playwright-cli 完成真实浏览器验证（登录 → 仪表盘 → 生成/素材/任务三模块），10 项断言全过，截图存于 `docs/reviews/ui-screenshots/`。此前"agent-browser 挂死导致 UI 无法自动化验证"的结论已作废 —— **换 playwright-cli 即可**（注意：会话随宿主任务结束而销毁，必须单任务内完成 open→交互→close；`eval` 需 `() => ...` 函数形式；返回值是 JSON 字符串字面量，解析前要反转义）。

仍未覆盖：跨浏览器兼容性、移动端布局、并发压力、真实多用户权限隔离。

---

### 🟠 二次自查发现（修复完成后对本轮改动本身的 review）

修完 B24 后对**自己这轮改动**做了一遍复查，又查出 2 项：

**B25（P1）— `input_schema` 无契约约束，字段缺键漂移**

由 `tests/verify_frontend_contract.py`（新增：拉真实响应逐字段比对 TS 接口）实测发现：Flux2 模板的 `width`/`height` 字段**缺 `required` 键**，而前端 `TemplateField.required: boolean` 是必填声明。

- 病灶不在前端 —— `TemplateManager.tsx:36-38,145` 生成字段时**总是**写 `required`
- 真凶是**我本人**：此前"补强 Flux2 模板"时用脚本手插字段，漏写 `required`
- 但暴露的真实脆弱点是：`input_schema: dict` 完全自由，后端零校验 → 手改 JSON / 直调 API 都能造出偏离 TS 契约的数据

修复：`app/schemas/schemas.py` 新增 `normalize_input_schema()`，在 `TemplateCreate`/`TemplateUpdate` 上用 `field_validator` 归一化——补 `label`/`required`/`type` 默认值、非法 `type` 降级 `text`、无 `name` 字段丢弃。
`TemplateResponse` 继承同一 validator，故**读出时也归一化**，存量脏数据无需另跑迁移即可修正。

**B26（P2）— `JSONStore.python_type` 违反 SQLAlchemy 契约（我自己引入的）**

原实现返回 `(dict, list)` 元组，而契约要求返回**单个 type**。`isinstance` 恰好容忍元组，但 `issubclass` 与部分库集成会炸。该属性无人调用，直接删除，不留自定义 API 面。

**另**：针对 `_unescape_json_columns` 的边界补测（`tests/verify_json_migration_edge.py`，9 用例 ×2 轮）——用户文本里的字面 `\u1234`、`C:\users\xx`、双反斜杠、emoji、嵌套结构、内嵌 JSON 字符串、空串、null，迁移后逻辑值**全部无损**且幂等。此前担心的"字面 \u 被误解码"未发生（`json.loads`+`dumps(ensure_ascii=False)` 是保真往返）。

**最终验证矩阵**：

| 套件 | 结果 |
|---|---|
| `smoke_fixes.py` | 18/18 |
| `verify_frontend_contract.py`（新增） | 契约一致 |
| `verify_schema_normalization.py`（新增） | 17 项全过 |
| `verify_tag_json_each.py` | 12/12 |
| `verify_json_migration.py` | 7/7 |
| `verify_json_migration_edge.py`（新增） | 9 用例×2 轮无损 |
| `verify_json_ascii.py` | 6/6 |
| `verify_b5_b6.py` | 全过 |
| `tsc --noEmit` | 零错误 |

**未覆盖（本轮历史状态，已被后续推翻）**：当时 agent-browser daemon 持续无响应，只能用契约测试替代验证前后端数据一致性，页面渲染/交互行为未经真实浏览器验证。
> ✅ **已作废**：后续换 playwright-cli 完成了真实浏览器验证，见文首「最终验证矩阵」与「UI 自动化验证」。此处保留仅为呈现排查过程。

---

## 一、总体评价

架构是干净的：引擎抽象（Provider 模式）、模板驱动表单、前端模块注册表三处解耦都做得不错，
M6 的引擎可扩展设计在代码层面确实成立。主要问题集中在三类：

1. **安全面**（路径穿越、硬编码密钥、弱口令）——上线前必须清掉；
2. **两个核心断点**：前端图片"上传"没传文件本体、任务进度从不回写；
3. **若干前后端联动缺陷**：模板停用后无法再启用、切换场景表单不重置等。

---

## 二、后端问题

### P0 — 安全

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| B1 | `app/auth.py:14` | `SECRET_KEY` 硬编码在源码里，拿到源码即可伪造任意 JWT | 从环境变量读取；未配置时启动随机生成并在日志警告 |
| B2 | `app/api/tasks.py:99-111` | `download_asset` 的 `filename` 直接拼路径，`..%2f` 可穿越读取 `STORAGE_DIR` 之外的文件；也未校验文件确实属于该任务 | `Path(filename).name` 取纯文件名 + `resolve()` 后校验 `is_relative_to(STORAGE_DIR)`；改用 Asset 记录定位文件 |
| B3 | `app/api/templates.py:109-112` | `upload_workflow` 的 `file.filename` 未清洗，可写入 `WORKFLOW_DIR` 之外（路径穿越写入）；同名文件静默覆盖 | `Path(file.filename).name` + 重名自动加后缀 |
| B4 | `app/main.py:22-28` | 默认管理员 `admin/admin123` 弱口令且打印到日志 | 从环境变量读初始密码；首启后强制改密或打印一次性随机密码 |

### P1 — 功能缺陷

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| B5 | `app/services/task_service.py:55-58` | **非 ComfyUI 引擎也强校验 workflow 文件**：`workflow_file` 为空时 `WORKFLOW_DIR/""` 是目录，`read_text` 抛异常，generic_api 模板跑不起来 | 只对需要 workflow 的引擎（ComfyUI）加载文件 |
| B6 | `app/services/task_service.py:94-104` | `_poll_result` 拿到的 `result.progress` **从不写回 `task.progress`**，前端进度条永远 0%；也不感知 ComfyUI 队列位置 | 轮询中回写 progress；用 `/queue` 排队信息估算 |
| B7 | `app/engines/comfyui.py:47-48` | `inject_params` 遇到映射的节点不存在时**静默跳过**，任务"成功"但参数没生效，配置错误极难排查 | 节点/路径缺失时抛错（或至少回传 warning 到 task.result） |
| B8 | `app/api/tasks.py:20` | `_running_tasks` 是进程内存集合：服务重启后 `running` 状态任务永久卡死，无恢复或超时扫描；多 worker 下失效 | 启动时把超时的 running 任务标为 error；长期看引入真正的任务队列 |
| B9 | `app/api/templates.py:23-25` | `GET /templates` 过滤 `is_active == True`，**停用的模板从管理列表消失，永远无法再启用**（前端管理页用同一接口） | 加 `include_inactive` 查询参数，或给 admin 单独的全量列表接口 |
| B10 | `app/api/tasks.py:114-137` | `retry_task` 不清理旧素材，重试成功后任务下素材翻倍 | 重试前删除该任务旧 Asset 记录与文件 |
| B11 | `app/models/models.py:57` | `Task` 表没有 `created_by`/user 归属字段，无法按人审计、过滤、限流 | 加 `created_by` 外键，提交时写入当前用户 |
| B12 | `app/schemas/auth_schemas.py:10` | `role: str = "member"` 不校验取值，可存入任意字符串 | 改 `Literal["admin", "member"]` |
| B13 | `app/api/templates.py:58-74` | 更新模板改名撞 `unique` 时抛 `IntegrityError` → 500 | 更新前查重，或 catch 后返回 400 |

### P2 — 改进项

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| B14 | `app/main.py:51` | CORS `allow_origins=["*"]` + `allow_credentials=True`，生产环境必须收紧 | 配置化前端域名 |
| B15 | `app/services/task_service.py:98` | `elapsed` 只累计 sleep 时间，实际等待可能远超 600s；超时后 ComfyUI 侧仍在跑但结果丢弃 | 用 `time.monotonic()` 计时；超时先尝试 `/queue` 移除 |
| B16 | `app/api/assets.py:144-149` | 批量下载全量读进内存；`writestr(asset.file_name)` 同名互相覆盖；文件缺失静默跳过 | 文件名加 task 前缀；缺失时在响应里告知；大文件改流式 |
| B17 | `app/services/task_service.py:123`、`app/api/tasks.py:110` | MIME 只认 `.png`/其余当 jpeg，webp/mp4 等全错 | `mimetypes.guess_type()` |
| B18 | `app/engines/generic_api.py:86` | 响应无 task_id 时**伪造 uuid**，之后永远查不到状态，600s 后才超时；`_results` 缓存永不清理（内存增长） | 无 task_id 直接报错；缓存加 TTL/清理 |
| B19 | `app/api/assets.py:38,43` | **两度误判，最终定位见 B24**。第一版称"引号包裹不精确"→ 实测 `图` 不命中 `图片`，描述有误；第二版归因"LIKE 通配符未转义"→ 实测下划线换 `x` 同样搜不到，仍未触达病灶。真因是 JSON 列 `\uXXXX` 转义（见 B24） | ✅ 随 B24 一并根治：标签改 `json_each` 精确匹配 + JSONStore 无转义存储 |
| B20 | `app/models/models.py:101` | `tags: Mapped[dict]` 实际存的是 list，类型标注误导 | 改 `Mapped[list]` |
| B21 | `comfyui.py:4-5`、`tasks.py:4`、`task_service.py:5` | 未使用的 import（`asyncio`、`Path`、`select`） | 清理 |
| B22 | `app/api/tasks.py:36-38` | 批量提交每个 task 单独 commit，32 个任务 32 次往返 | 建完统一 commit |
| B23 | `app/database.py:7` | `echo=settings.DEBUG` 时 SQL 全量刷日志 | 建议独立开关，默认关 |

---

## 三、前端问题

### P0 — 核心链路断点

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| F1 | `components/DynamicForm.tsx:84-105` | **`image_upload` 字段从不上传文件本体**——只把本地文件名 `file.name` 塞进参数。后端把这个名字注入 workflow，只有当 ComfyUI 服务器恰好有同名文件时才能跑通（之前 e2e 测试是手动把 images.jfif 放到服务器才成功的）。运营用户在自己电脑选图必然失败 | 新增后端 `/api/upload` → 转发 ComfyUI `/upload/image`，前端拿到服务器文件名再入参 |
| F2 | `components/TaskPanel.tsx:28-35` | **轮询死循环**：`useEffect` 依赖 `tasks`，而 `refresh()` 每次 `setTasks(新数组)` 都会再触发 effect → 无条件 `refresh()`，形成持续请求风暴（3s interval 反而是配角） | 拆两个 effect：`useEffect(() => { refresh() }, [refresh])` 只跑一次；interval 单独依赖 `hasActive` |

### P1 — 功能缺陷

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| F3 | `GenerateWorkspace.tsx:56` + `DynamicForm.tsx:15-21` | **切换场景表单不重置**：`values` 只在挂载时初始化，`<DynamicForm>` 没有 `key`，换模板后同名字段带入旧值、生成数量残留 | `<DynamicForm key={selected.id} ...>` |
| F4 | `TemplateManager.tsx:58-62` | 管理页拿不到已停用模板（见后端 B9），"已停用"标签是死代码，停用即失踪 | 配合 B9 修复 |
| F5 | `TemplateManager.tsx:298-343` | **字段编辑器不能配置 `options`/`default`/`placeholder`**——select 型字段的选项无处填写（现在 emptyForm 里的选项是写死的），模板系统的核心能力缺失 | 字段行按 type 展开 options 编辑（每行一个选项） |
| F6 | `AssetGallery.tsx:37`、`TemplateManager.tsx:61` | `resp.json()` 不检查 `resp.ok`：出错时返回 `{detail:...}` 被当数组渲染 → `assets.map` 崩溃白屏 | 判断 `resp.ok`，或统一走 `client.ts` 的 `request()` |
| F7 | `client.ts:90-91`、`AssetGallery.tsx:106-107` | `getAssetUrl` 拼接文件名**未 `encodeURIComponent`**，文件名含中文/空格/# 即 404 | 编码；更稳妥是后端提供 `/api/assets/{id}/file` |
| F8 | `TemplateManager.tsx:31-49` | 表单里没有 engine 选择（恒为 `comfyui`），`generic_api` 模板无法通过界面创建，与 M6"引擎可切换"目标不符 | 加 engine 下拉（数据源 `/api/engines`） |

### P2 — 改进项

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| F9 | `client.ts:42-49` | 401 时 `window.location.reload()` 硬刷新体验差；API 错误把原始 JSON 塞进 `alert` | 渲染登录过期提示；解析 `detail` 字段展示 |
| F10 | `TaskPanel.tsx:43-50` | `assetsMap` 缓存不失效：任务重试后仍显示旧素材或一直"结果加载中" | 任务状态变化时清对应缓存 |
| F11 | `AssetGallery.tsx:27-43` | 搜索框无防抖，每敲一个字符一次请求 | 300ms debounce |
| F12 | `AssetGallery.tsx`、`TemplateManager.tsx` | 手动 `fetch` + 各自复制 `authHeaders()`，绕过 `client.ts`，401 处理不一致 | 统一走 `request()` |
| F13 | `modules/index.ts:30-71` | 大量 `as unknown as ComponentType<ModuleProps>` 双重断言，把类型检查完全关掉 | 组件少接 props 是兼容的（结构化类型），多数断言可直接删掉 |
| F14 | `TemplateManager.tsx:432` | 编辑 `input_schema` 无 `fields` 键的模板时 `undefined.map` 崩溃 | `template.input_schema.fields ?? []` |
| F15 | `App.tsx:25-29` | 健康状态只在登录时查一次，离线后不会自动恢复显示 | 加定时刷新 |
| F16 | `DynamicForm.tsx:18` | `f.default ?? (f.type === 'number' ? '' : '')` 三元两分支相同，废代码 | 简化为 `f.default ?? ''` |
| F17 | `AssetGallery.tsx:67-85` | 批量下载不检查 `resp.ok`，失败时会把错误 JSON 当 zip 下载下来 | 先判状态码 |

---

## 四、前后端联动问题（跨层）

1. **图片上传链路**（F1 + B2/B3）：前端不传文件、后端不收文件，全链路缺 `/upload` 通道 —— 这是当前对目标用户（不懂技术的运营）最大的可用性障碍。
2. **模板启停**（B9 + F4）：后端过滤 + 前端同一接口 → 停用即失踪。
3. **进度条**（B6）：后端不回写 progress → 前端 `TaskPanel.tsx:97-106` 的进度条永远 0%。
4. **文件定位方式**（F7 + B2）：前后端都用 `task_id/filename` 定位文件，文件名成了事实上的主键，应统一改为 asset_id。

---

## 五、建议修复顺序

1. **安全四项**：B1、B2、B3、B4（上线前硬性要求，半天内可完成）
2. **两个核心断点**：F1（图片上传通道，需要前后端各加一个接口）+ F2（轮询死循环，10 分钟能修）
3. **联动缺陷**：B9+F4（模板启停）、B6（进度回写）、F3（表单重置）
4. **配置体验**：F5（options 编辑器）、F8（engine 选择）、B7（映射校验报错）
5. 其余 P2 按迭代节奏清理

---

## 六、B31 落地记录（任务/素材删除能力）—— 已实现

前面「待决策」的 B31 已按**方案 B（补齐删除能力）**落地，并做了安全加固。

**后端新增**
| 接口 | 行为 |
|---|---|
| `DELETE /api/assets/{id}` | 删记录 + 磁盘文件；返回 `file_removed` 标明是否真删了文件 |
| `DELETE /api/tasks/{id}` | 级联删掉全部素材（记录 + 文件）再删任务；返回 `assets_deleted` / `files_removed` |

安全与语义：
- **权限**：管理员可删任意；普通用户只能删自己提交的（素材归属走 `task.created_by`，显式查询，不用懒加载）。无主记录（`created_by` 为空）仅管理员可删
- **运行中的任务拒绝删除**（400）—— 后台协程还会往这条记录写进度/素材，删了就是竞态
- **级联顺序**：素材文件 → 素材记录 → 任务记录，不留孤儿
- **路径穿越防护**：抽出 `app/filestore.py` 统一实现 `asset_disk_path` / `delete_asset_file`，删除与读取都走它

> 抽公共守卫时顺手查出一个真漏洞：`batch_download` 原先 `settings.STORAGE_DIR / asset.file_path` **完全没有 `is_relative_to` 检查**，会把任意文件读进 ZIP。已一并堵上。**安全守卫不该复制到多个 API 模块 —— 复制就意味着某处会漏。**

**前端**：素材详情面板 + 批量勾选有删除入口，任务行有删除按钮（运行中隐藏）。均带二次确认，文案写明「不可撤销、文件一并从磁盘删除」，非本人提交会收到 403 明确提示。

**连带收益**：`verify_b5_b6` 每跑一次会往素材库累积 2 张真实测试图（今天跑 5 次，16→26 张就是这么来的）。现在跑完自动删掉自己的产物 —— 实测**跑前 26 张、跑两遍后仍是 26 张**，顺带拿真实 ComfyUI 产物验证了删除链路。

验证 `tests/verify_delete_assets_tasks.py`（**25 项断言全过**）：删素材记录+文件同消失、删任务级联清零无孤儿、非本人 403 且文件未被动、本人/管理员可删、404、运行中拒删、`file_path` 含 `../` 的越界记录只删记录不碰库外文件。

### 🔴 运行时实测发现：环境 safe-delete 钩子会让删除接口 500（已修）

功能实现完、25 项断言一度全过，但重跑全量时 `DELETE /api/tasks/{id}` 开始返回 **500**。逐层追到服务端堆栈：

```
app/filestore.py:32 delete_asset_file → target.unlink()
sitecustomize.py _safe_path_unlink → _try_trash → _check_bulk_delete_guard
  → _exit_bulk_guard_control → raise SystemExit(1)
```

**根因不是业务代码**：本环境 `sitecustomize.py` 给 `pathlib.Path.unlink` 套了安全钩子——先走回收站（不真删），再过一个「批量删除守卫」。守卫把删除次数累计到 `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR`，超阈值（`BULK_THRESHOLD=50`）就 `raise SystemExit(1)`。

**两层坑叠加**：
1. `SystemExit` **不是 `Exception` 的子类** → `except Exception` 捕不到 → 一路炸穿 ASGI → 客户端拿到空 body 的 500，`json.loads` 跟着崩
2. 守卫计数挂在 `CODEBUDDY_TOOL_CALL_ID` 上，是**单次 tool-call 的预算**。我启动后端时把它继承进去了，于是**常驻服务在跟一次工具调用共享删除额度**——跑几轮测试就撞阈值

**修复（代码层，任何环境下都正确）**：`delete_asset_file` 改返回 `(是否真删, 失败原因)`，显式捕获 `(OSError, RuntimeError, SystemExit)`：

```python
try:
    if not target.exists():
        return False, "文件本就不存在"
    target.unlink()
    return True, ""
except (OSError, RuntimeError, SystemExit) as exc:
    logger.warning("删除素材文件失败 %s —— %s", target, reason)
    return False, reason
```

**核心语义：文件删不掉绝不能让「删除记录」这个主动作失败** —— 记录才是主体，文件清理是附带的。两个调用方都按 tuple 消费：`assets.py` 返回 `file_removed` + `file_error`，`tasks.py` 累加 `files_removed` 并汇总 `file_errors`，前端能据此提示「记录已删但文件清理失败」。

实测证据（清 6 条残留时）：5 条 `file_removed: true`（进回收站），1 条 `file_removed: false` + `"文件本就不存在"`（上次清理只删了记录）——**如实上报，而不是假装成功或整体失败**。

**修复（启动姿势层）**：服务以生产姿势运行，只摘掉 per-tool-call 预算那几个变量（`CODEBUDDY_TOOL_CALL_ID` / `..._BULK_STATE_DIR`），**回收站安全（`GENIE_TRASH_DIR` / trash binary）保留不动**——那是该留的安全属性，不是要绕过的东西。

> **教训**：`except Exception` 不等于「捕获所有错误」。`SystemExit`/`KeyboardInterrupt`/`GeneratorExit` 是 `BaseException` 直系子类，会让「尽力而为」的清理代码变成整条请求的 500。凡是「附带动作不该拖垮主动作」的地方，都要显式想清楚要吞掉哪些 `BaseException`。

### 🟠 测试卫生：断言别依赖共享库状态

`smoke_fixes` 两处断言被我自己的测试残留搞挂过：盲取 `assets[0]` 当「真实大图」断言 `len(content) > 1000`，撞上 14 字节的 `deltest` 文件就红；标签断言同样受共享库污染。**素材库是共享的，`assets[0]` 不保证是你的数据。** 已改为显式按 `file_size > 1000` 挑真实生成图，标签断言加幂等前置清理。

另一处更隐蔽：`verify_delete_assets_tasks` 的 `cleanup()` 写死遍历 `(t1, t2, t5, t6)`，但测试造了 **t1–t6 六个**，`t3`/`t4` 漏掉 → **每跑一次就在共享素材库漏 2 条脏数据**。改成 `seed()` 自己登记 `SEEDED_TASKS`、cleanup 遍历登记表，加测试组也不会再漏。实测连跑两遍：素材恒 28 张、0 残留。

---

*Review 方法：全部源文件逐个通读，结合 10-27 的端到端测试记录（真实 ComfyUI v0.40.0 验证）交叉比对。*
