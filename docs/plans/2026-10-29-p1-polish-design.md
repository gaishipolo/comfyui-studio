# P1 体验优化设计（2026-10-29）

用户隔离（P0）交付后，把运营日常最容易撞到的四个粗糙点一次补齐。UI 测试由用户负责，本轮交付到构建 + API 层验证为止。

## P1-1 错误信息友好化

**问题**：任务失败时 `error_msg` 是引擎原样透传（如 `adaln kernel launch failed: invalid argument`），运营看不懂、不知道下一步做什么。

**方案**：`frontend/src/components/FriendlyError.tsx`
- `friendlyError(raw)` 按已知错误模式表（adaln/OOM/参数校验/超时/连接/缺模型/中断）返回处置建议；匹配不上原样返回（`matched=false`，不瞎猜）。
- `<FriendlyError message>` 组件：主文是人话，匹配到已知模式时用 `<details>` 折叠「原始报错」供技术人员排查。
- 接入点：`TaskPanel` 展开行、`GenerateWorkspace` 会话任务面板。

**错误模式表**（可按需扩充）：

| 模式 | 处置建议 |
|---|---|
| adaln kernel launch failed | 生成引擎内部计算出错（大负载已知问题）。降低分辨率/帧数重试；反复出现联系管理员重启服务 |
| cuda/hip out of memory | 显存不足。降低分辨率或减少生成数量 |
| invalid_input_type / value not in list / failed validation | 参数不被引擎接受。检查输入值（下拉选项、数字） |
| timeout | 任务太重或引擎繁忙。稍后重试或降低分辨率/帧数 |
| connection / econrefused | 连不上生成服务。稍后重试 |
| no such file / checkpoint / safetensors | 缺模型文件。联系管理员安装 |
| interrupt / cancel | 任务已被中断 |

## P1-2 复制参数再生成

**问题**：任务跑失败或想微调重跑，得回生成页重新找场景、手抄参数。

**方案**：跨模块草稿传递
- `types.GenerateDraft = { template_id, params, source_task_id? }`
- `App` 持 `draft` 状态；`ModuleProps` 扩展 `onNavigate` / `onReuseTask` / `draft` / `onDraftConsumed`。
- 任务页（TaskPanel / GenerateWorkspace 会话任务行）加「用这些参数再生成」按钮 → `onReuseTask(task)` 存草稿 + 切到生成页。
- `DynamicForm` 加 `initialValues`（挂载时生效，调用方换值时换 `key` 强制重挂载）；生成页消费草稿后置 `selected` + 预填 + 提示「已带入原任务参数，改一改再提交即可」+ 滚到顶部。
- 场景已删除时提示「原任务的生成场景已不存在」。

## P1-3 素材时间范围 + 创建人筛选

**问题**：素材多了以后只能按场景/标签找，不能按「谁生成的」「哪天生成的」缩范围。

**后端** `assets.py list_assets` 新增三参数：
- `creator`：按创建人 `task.created_by` 筛选（走 join，与归属过滤共用一次 join，不重复）。
- `created_from` / `created_to`：ISO 日期或日期时间；纯日期的 `created_to` 走 `_parse_day(s, end=True)` 解析成当天 23:59:59（「到今天」含今天全天）。
- 隔离过滤与 `creator` 并存：member 借 `creator` 参数也穿不透隔离（仍按 `created_by == user.id OR is_shared` 收）。

**前端** `AssetGallery` 筛选条：
- 两个 `<input type="date">`（创建时间起/止）+ admin 专属创建人 `<select>`（数据源 `listUsers()`，「全部创建人」）。
- 与关键词/场景/标签筛选并列，实时生效。

**验证**：`backend/tests/verify_asset_filters.py` 15 项（时间范围 4 / 创建人 4 / 回归守卫 3 / 清理 4）。

**测试教训**：日期参数不能硬编码测试跑的那天 —— 用 `TODAY` 动态算，且 `created_to` 用一个固定已知日（`OLD`）验证「纯日期含当天全天」语义（`a_old` 创建于当天 12:00，`end=True` 才收得到；`a_new` 是今天则被滤掉）。

## P1-4 任务完成通知

**问题**：提交任务后要盯着任务列表刷新才知道跑没跑完，人不能离开。

**方案**：`frontend/src/notify.ts` + toast 浮层
- `notify(text, tone)` / `notifyOnce(key, text, tone)`（按 key 去重，`done:{task_id}` / `error:{task_id}`）。
- 任务页与生成页各有一个 3 秒轮询器，都会观察到同一任务终态；`notifyOnce` 保证只弹一次。
- 状态过渡检测：只在「上轮 pending/running → 本轮 done/error」时通知（`prevStatusRef: Map<task_id, status>`），避免刷新页面把历史任务全弹一遍。
- `App` 末尾渲染右上角 toast 浮层（`fixed right-md top-md z-modal`，点击关闭，自动消失）。

## 验证汇总

- 后端 21 套回归全绿（含 `verify_asset_filters.py` 15 项、`verify_user_isolation.py` 56 项）。
- 前端 `tsc -b && vite build` 通过（`index-DB6PM8VB.js` 217.10 kB）。
- 产物字符串核对 14/14 命中：共享/再生成/原始报错/创建时间/通知文案等。
- 测试残留清理：deltest 用户 4 个、验证用模板 2 个、磁盘 `deltest-*` 目录 25 个，均已清。

## 非目标（后续 P2）

- 素材视频缩略图（ffmpeg 未安装，待补）
- 配额用量统计
- 模板版本导出
- 多引擎并行
- 审批模块
