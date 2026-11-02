# ComfyUI Studio 代码审查报告（2026-11-01）

## 0. 审查范围与方法

- **基线**：git HEAD `1983939` + 工作区未提交改动（前次审查 `docs/code-review-2026-10-30.md` 的修复：token 吊销、时区、分页边界、插件恢复校验、前端状态与样式修复等）。
- **方法**：按 5 个独立区域做只读全量审查（后端核心与安全、后端 API 层、引擎与服务、前端、测试/契约/文档），交叉核对前后端契约；前次已修复项逐条复核不重复报告。
- **审查前已验证**：后端 `test_*.py` 与核心 `verify_*` 全过、前端 `test:state` 全过、`tsc -b && vite build` 通过。
- **统计**：Critical 1 · Important 17 · Minor 44 · 测试盲区 5。

## 1. 总体结论

**结论：修复后可发布。**

无系统性架构问题。前次审查的修复（M5/M6/H1/M9/M4/M3/M1/M12/M13/M14/M17 等）经逐条复核**均正确且完整**，未发现回归。新发现问题集中在四个面：请求准入遗漏（`/prepare` 端点）、任务生命周期的头端竞态与轮询零容错、前端若干错误态/时区/竞态缺口、测试基建对真实数据的接触面。除 1 个 Critical（测试清理脚本会误删产品演示数据）外，均为小范围、低成本修复。

## 2. 总体亮点

- `backend/app/access.py:21-48` — 用户隔离规则单点收口（4 个 `can_*` + 批量归属查询），API 层各端点用法一致，无内联判断；404/403 语义成文。
- `backend/app/download_tickets.py:46-107` — 一次性下载票据：64-bit ticket + 256-bit secret、`hmac.compare_digest`、消费即删、按 ticket 隔离的 HttpOnly+SameSite=Strict Cookie、启动清扫超期 ZIP，安全设计干净。
- `backend/app/filestore.py:70-95,158-167` — 路径穿越防护单点实现（`safe_key`）+ `resolve()`/`is_relative_to` 纵深防御（符号链接越界被拦），`open()` 拒绝非普通文件（S_ISREG）。
- `backend/app/request_limits.py:103-174` — 在 FastAPI 解析 body 之前完成鉴权 + 体积封顶 + 1MB spool 落盘 + 精确重放，超限 413，是对内存 DoS 的正确工程化防御（有 ASGI 级测试）。
- `backend/app/auth.py:52-90` — token_version 吊销链路正确（老 token 无 `ver` 按 0 处理，改密/重置后旧 JWT 立即 401）；鉴权读 DB 角色而非 JWT 载荷，无权限漂移。
- `backend/app/services/task_service.py:240-248,341-349` — done 终态写入前在 lifecycle 锁内重读（堵 stale-miss 竞态）；submit 返回后补查取消标志。
- `backend/app/engines/comfyui.py:145-208` — 取消前先用 `/queue` 确认"跑着的确实是我们这条"，避免误杀他人任务；新旧版 ComfyUI 降级兼容。
- `backend/app/engines/factory.py:87-140` — ProviderLease 引用计数 + retire/close 状态机，lease 归零才关闭，无提前 close。
- `backend/app/database.py:21-41` — 列迁移幂等（先 PRAGMA 查列）+ JSON 列 `\uXXXX` 全量解码重写（幂等、脏数据跳过）。
- `backend/tests/test_task_terminal_race.py:141-353`、`test_request_limits.py:113-211` — 真实 SQLite 上的终态竞态测试与 ASGI 级准入测试，断言粒度到 `receive.calls`，是本项目测试质量的标杆。
- `frontend/src/components/DynamicForm.tsx:104-137` + `uploadTransaction.ts:12-33` — 按字段的 generation + pending 计数，过期上传不会写回 value；object URL 统一 revoke，无泄漏路径。
- `frontend/src/api/client.ts:399-415` — `openAssetInNewTab` 在同步栈里先 `window.open` 再 fetch，规避弹窗拦截，且有"open 先于首个 await"的时序测试。
- `README.md:135-177` — 测试文档诚实标注哪些脚本碰真实 `workbench.db`、哪些破坏性脚本被排除。

## 3. Critical（必须修）

### C1. 清理脚本前缀与产品演示模板撞名，`--apply` 会级联删真实数据
- 位置：`backend/tests/cleanup_test_rows.py:11-13`
- 问题：`TEST_TEMPLATE_PREFIXES = ("验证用-", "测试模板-")` 会命中产品界面演示模板「测试模板-M5」（`frontend/verify_f3.sh:17` 点击它、UI 快照 `tests/.ui-snap.yaml:29` 依赖它存在）。脚本直连真实库（`from app.database import engine`），`--apply` 时连带级联删除它的全部任务与素材；而 docstring 声称"只删测试 task_id，不碰真实数据"，与事实不符。
- 影响：运营者误信 docstring 执行 `--apply` = 生产/演示数据丢失。
- 修复：前缀改为测试脚本唯一占用的命名（如 `测试残留-`），或显式排除已知演示模板，并更正 docstring。

## 4. Important（应该修）

### 4.1 安全与准入

**I1. `/api/assets/batch-download/prepare` 漏进请求准入中间件，body 无上限（内存 DoS）**
- 位置：`backend/app/request_limits.py:28-47`、`backend/app/api/assets.py:535-558`、`frontend/src/api/client.ts:286`
- 问题：`_raw_limit` 只覆盖旧路径 `/api/assets/batch-download`、`/api/upload/image`、`/api/upload/file`；新端点 `/prepare` 接收同样的 `asset_ids: list[str]` JSON 裸 body 却不限。50 条上限在 FastAPI 完整反序列化之后才生效，认证用户可发数百 MB~GB 的 JSON 数组吃满单进程内存——正是本中间件专门要防的 H5 类问题，属改动遗漏。
- 修复：`_raw_limit` 加 `path == "/api/assets/batch-download/prepare"` → 64KB 上限（50 个 UUID 足够），`test_request_limits.py` 补一条用例。

**I2. minio 后端非 ASCII 文件名下载直接 500（Content-Disposition 手工拼接）**
- 位置：`backend/app/api/tasks.py:256-263`
- 问题：`STORAGE_BACKEND=minio` 时走 `StreamingResponse`，`Content-Disposition: f'attachment; filename="{safe_name}"'` 手工拼接；`safe_name` 用户可控（ComfyUI SaveImage 前缀），非 latin-1 字符（中文很常见）触发 header 编码 `UnicodeEncodeError` → 每次下载 500；含 `"`/换行则畸形 header。本地分支走 `FileResponse`（Starlette 自动 RFC 5987 编码），两后端行为不一致。
- 修复：按 `templates.py:413` 的 `urllib.parse.quote` 生成 `filename*=UTF-8''...`（补 ASCII 兜底），或抽全库共用的 helper。

**I3. 登录无暴力破解防护**
- 位置：`backend/app/api/auth_api.py:43-58`
- 问题：`/api/auth/login` 无频率限制/失败锁定。bcrypt（passlib 默认 cost 12，约 0.25s/次）提供部分缓解，但多连接可并行爆破；用户密码仅要求 ≥6 位。
- 修复：按用户名/来源做失败计数（如 5 次失败锁 15 分钟），或文档明确要求部署时前置限流。内部工具风险中等，修复成本低。

**I4. JWT 存 localStorage + 24h TTL + 登出不吊销（安全取舍未成文）**
- 位置：`frontend/src/api/client.ts:12-23`、`backend/app/auth.py:22`、`frontend/src/App.tsx:115-121`
- 问题：任何 XSS 可读取 24h 有效 token；登出仅清本地，服务端继续有效 24h（仅改密吊销）。
- 修复：至少把该取舍写入部署/安全文档；可选加登出吊销接口或缩短 TTL。

**I5. 破坏性 live 脚本无"目标可丢弃"自检，默认目标恰是生产端口**
- 位置：`backend/tests/test_env.py:10`（默认 `http://127.0.0.1:8000`）、`verify_frontend_contract.py:99`、`verify_plugin_loader.py:188`、`purge_test_residue.py:34` 等十余处硬编码 `admin/admin123`
- 问题：对"恰好还是 admin123"的在用实例跑这些脚本（增删模板/任务/素材/用户/插件文件）= 生产数据事故，脚本自身无任何防线，只靠 README 警告。
- 修复：要求显式环境变量（如 `COMFYUI_STUDIO_LIVE_TESTS_OK=1`）或先调 `/api/info` 比对测试实例标记，不满足即退出。

### 4.2 正确性与竞态

**I6. 入口"running"提交可覆盖并发取消（头竞态）**
- 位置：`backend/app/services/task_service.py:294-296`（execute）、`408-410`（resume）
- 问题：取消走 `cancel_task`（持 lifecycle 锁），但 execute/resume 的 pending→running 提交不持同一把锁、不重读。若取消的 commit 落在"本协程 `db.get` 之后、running commit 之前"，running 把 cancelled 覆盖回库；随后 `_cancel_requested` 检查只让执行协程安静退出，**DB 永远停在 running**，前端无限转圈，取消被静默吞掉（重启后 reap 还会 resume 这条幽灵任务再空耗 30 分钟）。M5 只修了尾端（done 覆盖 cancelled），没修头端。
- 修复：把 pending→running 迁移挪进 `thumbnail_lifecycle([task_lifecycle_key(task_id)])`，写入前重读、遇终态放弃——与 `cancel_task`/`_finish_task` 同一把锁同一模式。

**I7. 轮询无容错：单次 get_status 失败 = 任务终态失败 + 引擎侧孤儿任务**
- 位置：`backend/app/services/task_service.py:498`（`_poll_result` 无 try/except）→ `359-367`（外层 except 直接 `_commit_error`）
- 问题：任何一次瞬时故障（连接重置、远端 500、httpx 超时、畸形响应）直接把任务打成 error，且**不像超时路径那样先 cancel 引擎**——prompt 继续占显存跑完、产物无人回收，一次 30 分钟视频白跑。对远程 ComfyUI/云端 API 是现实概率事件。
- 修复：`_poll_result` 包住 `get_status`，单败记日志继续轮询；连续 N 次（如 5）或累计 >60s 失败才判失败，判失败前先 `provider.cancel`。

**I8. UsagePanel 月份快捷按钮在 UTC 以东时区差一天**
- 位置：`frontend/src/components/modules/UsagePanel.tsx:33-38`
- 问题：`d.toISOString().slice(0,10)` 取 UTC 日期；UTC+8 用户在本地 11-01 00:00 点"本月"算出 `2026-10-31`，整个统计区间偏移一天。
- 修复：用本地日期分量拼字符串（`getFullYear()/getMonth()+1/getDate()` 补零）。

**I9. AssetGallery 列表请求无乱序保护，慢响应覆盖新状态**
- 位置：`frontend/src/components/AssetGallery.tsx:94-115`
- 问题：`fetchAssets` 无序号/AbortController。任务完成触发无筛选请求 A，用户同时点标签发起带筛选请求 B，A 后返回 → 画廊显示无筛选结果但标签仍是选中态。`AssetPickerModal.tsx:61-84` 已用 `cancelled` 标志做对，属同类问题修了一半。
- 修复：请求序号计数器（或 AbortController），只接受最新一次。

**I10. 优雅停机可阻塞最长 30 分钟（新 H1 修复引入的回归）**
- 位置：`backend/app/engines/factory.py:243-251`、`backend/app/main.py:80`
- 问题：`close_all_providers` 对 leased 状态 `lease_zero.wait()` 无总超时；execute/resume 的 lease 覆盖整个任务生命周期（最长 1800s），lifespan 关闭段不先取消后台任务 → 有任务在跑时 Ctrl+C / 容器停止信号会卡最多 30 分钟，之后只能 SIGKILL。旧代码进程退出干脆不 close（秒退），新修复堵住了运行期泄漏却让停机路径变差。
- 修复：lifespan finally 先取消并短暂 await `_background_tasks`（超时即放弃），或给 lease 等待加总超时（如 30s）。

### 4.3 错误处理与 UX

**I11. 登录失败被 401→reload 吞掉，用户得不到任何反馈**
- 位置：`frontend/src/api/client.ts:46-50`（后端 `auth_api.py:50` 对"用户名或密码错误"返回 401）
- 问题：`request()` 把所有 401 当 token 过期：`clearToken()` + `reload()`。`Login.tsx` 的 `setError` 永远渲染不出来——输错密码只看到页面闪一下重载、输入框被清空。
- 修复：`request()` 对 `/auth/login` 跳过 401→reload 分支（或后端凭证错误改 400）。

**I12. TaskPanel 没有错误态：网络失败渲染成"暂无任务"且不再自愈**
- 位置：`frontend/src/components/TaskPanel.tsx:43-56,83-87,260-263`
- 问题：`refresh()` 静默 catch；初次加载失败时 `tasks=[]` 显示"暂无任务"，且 `hasActive` 由空数组算出 false，3s 轮询不启动，用户除非手点"刷新"会一直以为没有任务。AssetGallery 刚修好同样的问题，TaskPanel 漏了。
- 修复：加 `loadFailed` 状态 + 错误态 UI（复用 `listViewState` 思路），初始失败留轮询兜底或显著重试入口。

**I13. 上传类接口绕过统一的 401 处理**
- 位置：`frontend/src/api/client.ts:135-143,188-197,453-459,286-303`
- 问题：`uploadWorkflow/uploadInput/uploadPlugin/prepareBatchDownload` 直接 fetch，token 过期时用户得到裸的 "Not authenticated" alert，而不清 token 跳登录。同一文件里两套 401 语义。
- 修复：抽 `authHeadersFetch` 复用 `request()` 的 401 归一化处理。

**I14. submit 的 HTTP 错误把响应体丢了——最常见失败查不到原因**
- 位置：`backend/app/engines/comfyui.py:136-139`、`backend/app/engines/generic_api.py:101-103`
- 问题：ComfyUI `/prompt` 校验失败返回 400 + `{"error","node_errors"}`（哪个节点哪个参数非法），现在用户只看到脱敏后的 `HTTPStatusError: 400`；配模板调参映射是最高频出错场景，完全无法定位。
- 修复：catch `HTTPStatusError`，尝试 `resp.json()` 提取 `error`/`node_errors` 拼进异常消息（`safe_detail` 已脱敏，可安全透传）。

### 4.4 数据与测试基建

**I15. 两个 verify 脚本向真实 `workbench.db` 写测试行，且负向断言依赖"库里没有这种数据"**
- 位置：`backend/tests/verify_tag_persist.py:20-71`、`verify_tag_json_each.py:19-93`
- 问题：直接 INSERT 测试 Asset，仅正常跑完时自清理，Ctrl+C/崩溃留残留；负向断言（期望 `%"smoke"%` 命中 0 条等）会被真实数据误报 FAIL。
- 修复：改用临时 DB（`verify_queue_and_auth.py:112-116` 已有现成模式）。

**I16. `e2e_restart_recovery.py` 端口匹配是子串匹配，可能杀错进程**
- 位置：`backend/tests/e2e_restart_recovery.py:79-82`
- 问题：`f":{SERVICE_PORT}" in line` 对 netstat 整行子串匹配，`:8000` 会命中 `:80000`；`taskkill /PID /F` 会杀掉无关进程（脚本里唯一真正破坏性的一步）。
- 修复：严格锚定本地地址字段，如 `re.search(rf"127\.0\.0\.1:{port}\s", line)`。

**I17. DB 连接池与锁策略未显式声明**
- 位置：`backend/app/database.py:7`（`create_async_engine` 无参数）、`backend/app/services/task_service.py:273-274`
- 问题：aiosqlite 默认 `AsyncAdaptedQueuePool(size=5)` 且默认 journal_mode=delete（非 WAL）。后台任务 session 从提交持有到任务结束（最长 30 分钟）：`.env` 把 `MAX_CONCURRENT_TASKS` 调到 ≥4 后后台任务即可耗尽连接池，API 请求挂起 30s 后 500 且无告警；并发写时读请求还可能撞 "database is locked"（默认 busy timeout 5s）。
- 修复：显式 `pool_size=max(10, MAX_CONCURRENT_TASKS+5)`，并加 `PRAGMA journal_mode=WAL` + 合理 busy timeout；注释声明该约束。

## 5. Minor（可改进）

### 5.1 后端核心
| # | 位置 | 问题 |
|---|------|------|
| M1 | `timeutils.py:26` | Python 3.11+ `fromisoformat` 接受 `20261029` 等 basic 形式，与 400 文案"应为 YYYY-MM-DD"不一致；可加长度/格式白名单 |
| M2 | `timeutils.py:32-40` | 未知时区回退抛 ValueError 被 `parse_day_or_400` 转成 400"日期格式不合法"，文案误导（tzdata 已入依赖，概率低） |
| M3 | `filestore.py:303-311` | `MinioFileStore.exists` 吞所有异常返回 False，"文件不存在"与"存储不可用"混同（read/open 区分正确，仅 exists 不区分） |
| M4 | `request_limits.py:76,122` | 双 Content-Length 头 → 413（语义应为 400） |
| M5 | `main.py:148-149` | dist 存在但 `index.html` 缺失时 `FileResponse` 抛错 → 500，无友好 503 |
| M6 | `config.py:10` | `DEBUG` 死配置：全库无代码读取（`.env.example` 同步有，受 `test_ffmpeg_settings` 一一对应约束，删需两处同步） |
| M7 | `models.py:118-138` | `(task_id, file_name)` 无 DB 唯一约束，重名靠应用层（与 API 层 M9 同源） |

### 5.2 后端 API
| # | 位置 | 问题 |
|---|------|------|
| M8 | `templates.py:84-95,121-130` | 停用模板对普通用户可读（`include_inactive`/详情只要求登录态），泄露配置草稿；提交会被 `is_active` 挡住，影响小 |
| M9 | `tasks.py:225,286` | 同任务内重名文件 `scalar_one_or_none()` 抛 `MultipleResultsFound` → 500；建议 `scalars().first()` + 落盘去重 |
| M10 | `plugins.py:221-235` | `test_plugin` 无超时（插件 `health_check` 可无限挂起管理页），建议套 `asyncio.wait_for` |
| M11 | `templates.py:221-231` | 工作流文件上传 `await file.read()` 无大小上限（admin-only，建议 10MB 封顶） |
| M12 | `plugins.py:113-128` | 上传中途崩溃的 `_upload_*.py` 残留会被启动扫描当插件加载，建议扫描跳过或启动清扫 |
| M13 | `plugins.py:248-270` | restore 首循环新建 DB 行未过 `_SAFE_NAME`（第二道校验只迭代旧 `rows`），超 64 字符名字可入库（admin-only，路由难命中） |
| M14 | `assets.py:316,335,361` | `remove_tag` 精确 `in` 而 `add_tag`/`list_tags` 用 casefold：加 "Foo" 删 "foo" 静默不删；tag 查询参数无长度上限 |
| M15 | `auth_schemas.py:9-10,33-34`、`schemas.py:88` | 字符串字段缺 `max_length`（SQLite 不强制，跨库迁移会炸），建议对齐列宽 |
| M16 | `config.py:16` + `main.py:100-106` | CORS 默认 `*` + `allow_credentials=True`（鉴权走 Authorization 头，实际风险低；`.env.example` 已提示收紧） |
| M17 | `assets.py:577-580` | API 层直接调票据注册表私有 `_unlink`，建议暴露公开 `discard_archive(path)` |

### 5.3 引擎与服务
| # | 位置 | 问题 |
|---|------|------|
| M18 | `generic_api.py:156` | 进度 `float(data.get("progress", 50.0))` 无校验/钳制：非数值 ValueError、>100/负值直接落库（ComfyUI 端精心钳到 3..97，generic 端裸传） |
| M19 | `base.py:105` + `thumbs.py:67` | `download_output -> bytes` 契约决定大产物全内存（200MB 视频 = 200MB 字节 + 整段写临时文件喂 ffmpeg），当前规模可接受，provider-dev-guide 应写明 |
| M20 | `task_service.py:206,348,511` | 三处 `print(...)` 混在 logger 体系里，换 `logger.warning` |
| M21 | `factory.py:155-160` | `_fire_change` `except Exception: pass` 无日志，缓存清理钩子坏了无从察觉 |
| M22 | `plugin_loader.py:25,50` | 白名单报错文案说"只允许 app.engines.base"，实际放行整个 `app` 顶层包，误导管理员（信任模型已声明，非安全问题） |
| M23 | `comfyui_progress.py:19-20` | 重启接回的任务因新 `client_id` 收不到 WS 事件，恒显示粗粒度进度；设计约束，建议在 resume 文档注明 |
| M24 | `generic_api.py:83,196` | 复用 `COMFYUI_TIMEOUT` 设置（命名耦合）；URL 直连下载每次新建 `AsyncClient` 重复 TLS 握手 |
| M25 | `task_service.py:182` | `_cancel_requested` 成功取消后不清除（仅 retry 清），缓慢增长（上次报告已有） |

### 5.4 前端
| # | 位置 | 问题 |
|---|------|------|
| M26 | `App.tsx:32,248-258` | 改密修复后 `notice` 状态再无任何赋值点，横幅永不可达，可删 |
| M27 | `UserManager.tsx:110` | `text-headline` 未在 tailwind 注册，字号静默失效，改 `text-section` 或补注册 |
| M28 | `TaskPanel.tsx:426-429`、`GenerateWorkspace.tsx:281-284` | 点缩略图 `openAssetInNewTab(...).catch(() => {})` 零反馈（弹窗被拦/404 时点了没反应） |
| M29 | `DynamicForm.tsx:901` | `batchCount` 可提交小数（3.5 → 后端 422），`Math.round` 一下 |
| M30 | `client.ts:25-28` | `getStoredUser` 无 try/catch，localStorage 坏 JSON → App 首次渲染白屏 |
| M31 | `AssetGallery.tsx:620-631` | 标签输入框直接操作 DOM（非受控 + `e.target.value=''`），改受控 state |
| M32 | `PluginManager.tsx:42-48`、`UsagePanel.tsx:60-67` | 角色守卫前就发注定 403 的 admin 请求，effect 加 `user.role === 'admin'` 条件 |
| M33 | `App.tsx:115-121` | 登出清了 `notified` 但没清 `prevTaskStatusRef`，换人登录可能弹上一用户的"任务完成"toast |
| M34 | `TemplateManager.tsx:501-506` | comfyui 模板切插件引擎后 `workflow_file`/`param_mapping`/`output_config` 被 UI 隐藏但仍随保存提交 |
| M35 | `DynamicForm.tsx:571-584` | required 的列表（非文件）字段提交空数组时前端静默放行，报错留给后端（注释表明有意为之，建议补"请先填写"提示） |
| M36 | `base.css:23-32` | `[data-slot]` 死样式（全 src 无使用点），可删 |
| M37 | `AssetGallery.tsx:516-576` 等三个弹窗 | 无 Escape 关闭/焦点圈定，键盘用户不友好 |
| M38 | `types/index.ts:198` vs `:14` | `FieldSpec.options: string[]` 与 `TemplateField.options: (string\|number\|boolean)[]` 类型漂移（平台已支持布尔选项） |

### 5.5 测试 / 文档 / 工程卫生
| # | 位置 | 问题 |
|---|------|------|
| M39 | `verify_batch_archive.py:449` | 模拟 `receive()` 误调用时 `asyncio.sleep(3600)` 让测试挂 1 小时而非快速失败，改抛 `AssertionError` |
| M40 | `verify_plugin_loader.py:188-199` | 模块顶层 import 即登录 admin、注册 member、起假服务，被 import 就打到 live 服务，移进 `main()` |
| M41 | `smoke_fixes.py:110-119,151` | 向真实 `WORKFLOW_DIR` 上传 `evil_smoke.json` 后不清理；硬编码机器绝对路径 `E:\xx\...` |
| M42 | `requirements.txt` | 全部只给下限无上限、无 lockfile（重建环境不可复现）；`passlib`/`python-jose` 维护停滞（靠 `bcrypt<4` 绕兼容），建议评估 `pyjwt`/`pwdlib` 或至少加上限 |
| M43 | `frontend/verify_f*.sh`、根 `tests/ui_verify*.sh`、`tests/.ui-snap.yaml` | bash 杂项混在 frontend；快照把 14 个测试残留模板名固化为 UI 期望状态（陈旧）；建议删除或移入 `docs/archive/` |
| M44 | `README.md:137-145` | 测试清单不符：37 个 verify 只列 36（缺 `verify_download_tickets.py`），13 个 `test_*.py`（现单元级主力）零提及 |

## 6. 测试盲区

| # | 路径 | 为什么重要 | 建议 |
|---|------|-----------|------|
| T1 | 存量库列迁移（`database.py:21-41`） | 缺列老库 + 含 `\uXXXX` 转义的数据跑 `init_db` 是真实升级风险最高处，目前无测试 | 建"缺列 + 转义 JSON"种子库，跑 `init_db`，断言列补齐、JSON 已解码、重复执行幂等 |
| T2 | 模板导入/导出往返（`templates.py:419-519`） | 去重复用/overwrite 保留 id/重名改名/缺引擎告警/版本拒绝，目前仅 live 脚本覆盖，逻辑是纯 DB+文件操作 | 下沉为离线测试 |
| T3 | 插件上传校验（`plugin_loader.py`） | 语法错/依赖白名单/子类检测/保留名拒绝仅 live 覆盖，校验核心是纯函数 | 离线化后可无服务运行 |
| T4 | JWT 吊销完整 HTTP 链路 | `test_auth_and_task_retention.py` 只测 `token_version_matches` 函数；"改密→旧 token 401"与"老 token 无 `ver` + 老用户 `token_version` NULL"迁移路径未走真实依赖链 | 补 in-process HTTP 测试 |
| T5 | 整应用 ASGI 层 | 无 in-process 测试挂起整个 FastAPI app；路由前缀/查询参数名拼错只能靠 live 脚本发现 | 加最小冒烟：登录→建模板→提交→列素材 |

## 7. 修复优先级建议

- **P0（立即）**：C1（误删真实数据的陷阱，改动几行 + docstring）。
- **P1（安全/数据，先修）**：I1（`/prepare` 准入）、I3（登录限流）、I5（破坏性脚本自检）、I15/I16（测试碰真实库/杀错进程）、I2（minio 下载 500）、I4（JWT 取舍至少成文）。
- **P2（正确性/稳定性）**：I6（头竞态）、I7（轮询容错）、I10（停机阻塞）、I14（submit 错误体）、I8/I9/I11/I12/I13（前端四件 + 上传 401）、I17（DB 池/WAL）。
- **P3（后续迭代）**：全部 Minor + 测试盲区 T1–T5。

## 8. 架构与流程建议

1. 列表 fetch 统一"序号计数 / cancelled 标志"模式（AssetGallery/TaskPanel/TemplateManager），一次修掉同类请求乱序（I9 及未遂案例）。
2. Content-Disposition 生成收敛为一个 helper（现有 FileResponse、`templates.py:413` 手工 quote、`tasks.py:261` 裸拼三套写法）。
3. 给"认证 JSON POST"加全局兜底上限（如 8MB），而非逐路径枚举准入（I1 的根因；`/api/tasks`、`/api/templates/import` 同样无界）。
4. 依赖引入 lockfile（`pip-tools`/`uv`），`passlib`/`python-jose` 替换列入 P2（M42）。
5. `test_*.py` + 离线 verify 收进一个 runner（如 `tests/run_offline.ps1`），README 给出"一条命令跑完不碰任何真实资源"的入口；live/破坏性脚本归档并加 I5 自检。
6. 前端 `alert()` 收敛进 `notify.ts` toast 体系（批量删除多行汇总尤其适合）。
7. 为 I6/I7 补对应回归测试：并发 cancel vs execute 入口、mock `get_status` 抛一次 `ConnectError` 后恢复；为 I10 补 `verify_shutdown` 类脚本（任务在跑时 shutdown，断言限时完成）。

## 9. 附录：分域评估

| 域 | 结论 | 说明 |
|----|------|------|
| 后端核心与安全 | 修复后可发布 | 前次修复（token 吊销/迁移/时区/票据）全部复核通过；新增 I3/I17 与小量 Minor |
| 后端 API 层 | 修复后可发布 | 无越权/IDOR/注入；I1/I2 + 11 个 Minor |
| 引擎与服务 | 修复后可发布 | M5/M6/H1/M9/404 终态/sys.modules 清理均修对；新引入 I6–I10 中的引擎侧问题 |
| 前端 | 修复后可发布 | 前次修复（开窗时序/退避/必填校验/配色）全部验证正确；6 个 Important 中 4 个是小改动 |
| 测试/契约/文档 | 修复后可发布 | 应用契约一致、核心竞态/准入/吊销测试扎实；1 Critical（脚本陷阱）+ 4 Important（测试基建卫生） |

**综合结论：修复后可发布。** 应用代码无阻断性缺陷；P0/P1 共 8 项（多数改动 <20 行）修完即可发布，P2/P3 排入后续迭代。

## 10. 修复记录与回归结论（2026‑11‑02）

### 10.1 修复完成度
本审查的 **C1、I1–I17、M1–M44、测试盲区 T1–T5 全部按 TDD 修完**（先 RED 后 GREEN，未用子代理）。要点落点：
- **C1**：`cleanup_test_rows.py` 前缀改显式 allow‑list + `PROTECTED_TEMPLATE_NAMES` 双保险 + `__main__` 守卫（`test_cleanup_prefixes.py` 5/5）。
- **I1–I7/I15/I16**：`/prepare` 64KB 准入；RFC5987 `attachment_disposition()`；`login_guard` 5 次锁 900s→429；live 脚本 `require_live_ok()` 自检；`verify_tag_*`/端口匹配改临时资源。
- **I6/I7/I10**：execute/resume 的 pending→running 持 `thumbnail_lifecycle` 锁 + 重读；`_poll_result` 60s 宽限 + 判败前 `provider.cancel`；`cancel_background_tasks()` + `close_all_providers(lease_timeout=)` + lifespan 收尾。
- **I17**：`_build_engine()` 显式 `pool_size` + WAL + `busy_timeout`。
- **Minor M1–M44 / T1–T5**：全量落地，新增回归测试 `test_legacy_db_migration`、`test_template_roundtrip`、`test_plugin_upload_validation`、`test_auth_revocation_http`、`test_app_asgi_smoke`。

### 10.2 回归验证（本轮全量，均离线/临时资源，不碰真实库与服务）
| 类别 | 结果 |
|------|------|
| 后端单元 `test_*.py`（35 套） | 全绿 |
| 离线 verify `verify_*.py`（19 套：archive/download_tickets/error_boundary/filestore/size_gate/json_* /media_*/multi_image/param_coercion/progress/provider_lifecycle/queue_auth/schema_idempotent/tag_* /video_outputs） | 全绿 |
| 前端 `npm run test:state`（task5/list_view/modal_a11y） | 36/36 |
| 前端 `tsc -b` + `vite build` | 通过 |

> live/破坏性 verify（`verify_b5_b6`、`verify_cancel`、`verify_p0_fixes` 等 15 套）需一次性可丢弃实例 + `COMFYUI_STUDIO_LIVE_TESTS_OK=1`，本轮未跑；其离线契约已由 `test_verify_b5_b6_contract` 等覆盖。

### 10.3 全量回归中新发现并已修复的次生问题
1. **live 守卫位置**：`cleanup_test_rows.py`/`verify_b5_b6.py` 的 `require_live_ok()` 原在模块级，导致被单测 `import`/`exec` 时误 `sys.exit`；改为仅在实际执行（`__main__` / 仅当 B6 指向生产默认基址）时触发，纯 in‑process / 打 fake 的路径不受限。
2. **子进程编码**：`test_verify_b5_b6_contract.py` 用 `text=True` 让父进程按本机 locale（GBK）解码子进程 UTF‑8 输出 → reader 线程 `UnicodeDecodeError`、`stdout` 变 `None`；改为按字节捕获后 `decode("utf-8")`。
3. **进度追踪缺方法**：`ProgressTracker` 补 `tracked()`（返回登记 prompt 数，配合 `_MAX_TRACKED=256` 上限），`verify_progress` 恢复。
4. **素材名去重查询**：`_save_asset` 新增 `db.execute(select(Asset.file_name)…)`；`verify_error_boundary` 的 `FakeAssetDb` 补 `execute()`。
5. **`.scalars().first()` 不兼容**：`tasks.py` 两处素材单查改用 `scalar_one_or_none()`（M7 唯一索引保证 ≤1 行，且与 fake DB 契约一致），`verify_media_reliability` 38 项恢复。
6. **轮询失败吞掉异常破坏错误边界**：I7 宽限后 `_poll_result` 不再向上抛 `get_status` 异常，`resume_task` 的「原始 traceback 进日志」契约丢失；在宽限判败处补 `logger.error(... exc_info)` 并把 `engine_name` 透传，`resume_task` 入口 commit 失败改持久化 `safe_detail(e)`，`verify_error_boundary` 恢复。
7. **Starlette 断连监听**：新版 `FileResponse` 流式时循环 `receive()` 等 `http.disconnect`；`verify_batch_archive` 的 fake `receive` 由「第二次即报错」改为「阻塞直到任务组取消」，13 项恢复。

### 10.4 结论
**回归全绿，维持「修复后可发布」结论。** 上述 7 项次生问题均已在验证阶段发现并修复，无遗留 RED。后续 live/破坏性 verify 在有可丢弃实例时按 `COMFYUI_STUDIO_LIVE_TESTS_OK=1` 执行即可。
