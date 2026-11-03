# ComfyUI Studio 独立代码复审（2026-11-03）

**复审基线**：`6aabe54` + 工作区未提交改动（10 个已改文件 + `asset_retention.py` / `quota.py` / `test_asset_trash.py`）
**复审范围**：后端 `backend/app/` 全部 41 个模块（8,173 行）、前端 `frontend/src/` 全部组件（7,410 行）
**复审方式**：逐文件通读，**不采信 2026-10-30 / 11-01 / 11-02 三份报告的任何结论**；所有结论均对照当前源码核实，关键项交叉验证（grep 调用点、核对前后端契约、检查依赖版本、统计测试实况）
**环境证明**：本机 Python 3.13.14，`websockets` 实装 15.0.1；`fastapi`/`sqlalchemy`/`pydantic`/`httpx` 未安装，**无法在本机跑测试**（见 P2-13）

---

## 一、结论

**架构与核心链路质量高，但当前处于「后端已演进、前端未跟上」的半成品状态。**

- 任务状态机、缩略图、批量下载、路径穿越防护、Provider 生命周期、插件加载这几块的工程质量**明显高于同类内部工具**，且注释解释的是「为什么」而不是「做了什么」。
- 11-02 报告之后又进了两批改动（`7b9f5dc..HEAD` 共 93 文件 +4790/−2975，以及工作区未提交的回收站 + 配额），**这三轮改动都没有经过任何 review**。问题几乎全部集中在这两批改动的前后端衔接处。
- 发现 **2 个 P0**、**11 个 P1**、**13 个 P2**。P0 都是「功能做完了但前端没接」导致的确定性故障，不是设计缺陷。

**最严重的一条**：素材软删除的返回契约改了，前端没改，导致**每一次删除素材都会弹出一个红色错误框**（原因显示"未知"），批量删除则全部误报。这是用户每天都会碰到的路径。

---

## 二、P0 — 阻断

### P0-1 素材删除契约破裂：每次删除都弹「磁盘文件未能清理」

| 位置 | 证据 |
|---|---|
| `backend/app/api/assets.py:697-750` | `delete_asset` 返回 `{"message": "已移入回收站", "id", "deleted_at"}` |
| `frontend/src/api/client.ts:341-345` | 类型声明仍是 `{ message; id; file_removed: boolean; file_error: string }` |
| `frontend/src/components/AssetGallery.tsx:236` | 单删：`if (!res.file_removed) { alertDialog({ title: '记录已删除，但磁盘文件未能清理', ... 原因：${res.file_error || '未知'} }) }` |
| `frontend/src/components/AssetGallery.tsx:269` | 批量删：`if (!res.file_removed) fileLeft.push(...)`，循环里**每个 id 都会进误报列表** |

**后果**：`res.file_removed` 是 `undefined`，`!undefined === true`。单删必弹一个 tone=error 的对话框、原因显示"未知"；批量删 N 个就弹"N 个记录已删除，但磁盘文件未能清理"。

**同时文案已成谎言**：`AssetGallery.tsx:218` 与 `:257` 都写「此操作不可撤销，文件会一并从磁盘删除」，实际后端已改为软删除（文件仍在盘上、仍占配额、可恢复）。

**修法**：`deleteAsset` 返回类型改为 `{ message: string; id: string; deleted_at: string }`，删掉两处 `file_removed` 分支；确认框文案改为「移入回收站（可在回收站恢复；彻底删除才释放空间）」。

### P0-2 回收站 / 配额后端已完整实现，前端零接入

后端已经做完 5 个端点，**前端 `src/` 全局搜索 `quota` / `trash` / `restoreAsset` / `purgeAsset` 结果为空**，`types/index.ts` 也没有 `deleted_at` / `storage_quota_bytes`。

| 后端已有 | 位置 |
|---|---|
| `GET /api/assets/quota` | `assets.py:416` |
| `GET /api/assets/trash` | `assets.py:428` |
| `DELETE /api/assets/trash`（清空） | `assets.py:453` |
| `POST /api/assets/{id}/restore` | `assets.py:497` |
| `DELETE /api/assets/{id}/purge` | `assets.py:515` |

**后果是一个闭合的坏循环**：
1. 删除素材 → 进回收站、从列表消失 → **前端没有任何入口能看见或恢复它**；
2. 配额仍按磁盘占用计算（`quota.py:43` 刻意不过滤 `deleted_at`）→ 用户删了文件空间没释放；
3. 上传超额时收到的 403 文案是「请先在素材库清理文件（**彻底删除**才释放配额）」（`quota.py:75`）→ **而这个操作在界面上根本不存在**；
4. 管理员也没法给成员设配额：后端 `UserUpdateRequest` 已支持 `storage_quota_bytes`（`auth_schemas.py`），前端 `updateUser`（`client.ts:126-130`）不接受该字段、`User` 类型没有它、用户管理表格也没有这一列。

**修法**：补 5 个 client 方法 + `Asset`/`User` 类型字段；素材库顶部加配额条、增加回收站视图（恢复 / 彻底删除 / 清空）；用户管理加"配额"列。

---

## 三、P1 — 应修

### P1-1 `websockets` 依赖约束与代码不匹配，12.x 环境下进度静默降级

- `backend/requirements.txt:12`：`websockets>=12.0,<16.0`
- `backend/app/engines/comfyui_progress.py:195`：`from websockets.asyncio.client import connect`

`websockets.asyncio.client` 是 **13.0 才引入**的新 asyncio 实现，12.x 没有。而该导入位于 `_run()` 的 **while 循环之外**：

```python
async def _run(self) -> None:
    from websockets.asyncio.client import connect   # ← 循环外，抛了没人接
    backoff = 1.0
    while not self._closed:
        try: ...
```

`ensure_running()` 只检查 `import websockets`（12.x 也能通过），随后 `create_task(self._run())`；一旦 `_run` 在导入处抛 `ImportError`，任务直接死掉，`self._task` 已非空不会重试，只留一条 "Task exception was never retrieved" 日志。**表现是进度永远停在粗粒度（3%/35%），不报错**。

当前本机装的是 15.0.1，所以没触发；但约束允许 12.x，换环境就会命中。

**修法**：约束改 `websockets>=13.0,<16.0`；同时把导入移进 `try` 或加 fallback，避免静默死。

### P1-2 前端全站时间比真实时间早 8 小时

后端统一存 **naive UTC**（`timeutils.py:21-27`），Pydantic 序列化为 `"2026-11-02T16:00:00"`（无 `Z`/偏移）。按 ECMAScript 规范，带时间但无偏移的字符串按**本地时间**解析，UTC+8 下所有时间都少 8 小时；凌晨 0–8 点创建的记录连日期都差一天。

| 位置 | 代码 |
|---|---|
| `TaskPanel.tsx:407` | `new Date(task.created_at).toLocaleString('zh-CN')` |
| `AssetGallery.tsx:621` | `new Date(asset.created_at).toLocaleDateString('zh-CN')` |
| `AssetGallery.tsx:682` | `new Date(detail.created_at).toLocaleString('zh-CN')` |
| `GenerateWorkspace.tsx:248` | `new Date(task.created_at).toLocaleTimeString('zh-CN')` |
| `UserManager.tsx:281` | `u.created_at.slice(0, 10)`（取的是 UTC 日期） |

**修法**：抽一个 `parseServerTime(s)`（无时区后缀时补 `Z`），全站统一走它。

### P1-3 软删除隔离有 4 处遗漏

已核实的**正确**部分：`list_assets`、`get_asset`、`GET /file`、`GET /thumb`、`list_tags`、批量归档 `_prepare_archive`、`get_task_assets`、`delete_asset`、`purge_asset`、`restore_asset` 都正确带了 `deleted_at` 判断；`tasks.py:203/282`（重试、删任务）**有意不滤**，因为必须连回收站一起清——这是对的。

**遗漏的写操作**：

| 位置 | 问题 |
|---|---|
| `assets.py:614-620` `add_tag` | 只判 `asset is None`，可给回收站里的素材加标签 |
| `assets.py:645-651` `remove_tag` | 同上 |
| `assets.py:683-689` `share_asset` | 同上，**可把回收站素材共享给团队** |
| `uploads.py:123-129` `upload_from_asset` | 只判 `None` + `can_view_asset`，回收站素材仍可被当作参考输入转发到引擎 |

前端拿不到回收站素材的 id，正常 UI 走不到；但语义上与「回收站里的东西不可操作」矛盾，且 `share_asset` 这条会把已删素材重新挂上 `is_shared`。

**修法**：以上 4 处统一加 `if asset.deleted_at is not None: 404`（写操作按现有约定返回 404）。

### P1-4 模板导出的权限与详情接口不一致

- `templates.py:131` `get_template`：`if not template or (not template.is_active and user.role != "admin"): 404`
- `templates.py:395-397` `export_template`：只 `db.get`，**不检查 `is_active`**，且只要求登录

普通用户可以对停用模板调 `/export`，拿到**完整配置 + 内嵌工作流 JSON**（节点、模型名、参数映射）。`get_template` 已经挡住了，导出这条漏了——属于修复不彻底。

**修法**：`export_template` 补同样的 `is_active` 判断。

### P1-5 配额功能零测试

- 回收站：`backend/tests/test_asset_trash.py` 有 10 个用例（软删、恢复、404、purge 前置、权限隔离、标签过滤、过期清理、days=0 关闭）
- 配额：**`backend/tests/` 全局搜索 `quota` / `ensure_quota` 无任何命中**

而 `quota.py` 有 5 条容易写错的规则：边界等于、超限、管理员豁免、`quota=0` 不限、回收站计入。这块是新增的核心功能，却是唯一没有测试的。

**修法**：补 `test_quota.py`，至少覆盖上述 5 条 + `used_bytes` 不过滤 `deleted_at`。

### P1-6 `empty_trash` 与 `asset_retention` 的分批策略不一致

- `asset_retention.py:9-11` 注释明确：本机 `sitecustomize` 给 `Path.unlink()` 套了批量删除守卫，累计超阈值会 `raise SystemExit`，所以做成 **50 个/批 × 最多 20 批**。
- `assets.py:463-471` `empty_trash`：一次性 `select` 全部回收站记录、逐条 `db.delete`、`commit` 后再逐个删文件——**不分批**。

它确实 catch 了 `SystemExit`（`assets.py:480`），但顺序是**先 commit 删 DB 记录、再删文件**。一旦某个文件删不掉（被占用 / 触发守卫），这些文件就成为**永久孤儿**：`asset_retention` 只扫 `deleted_at` 非空的 DB 记录，记录已经没了，再也扫不到。

**修法**：`empty_trash` 复用 `purge_expired` 的分批逻辑（或直接抽一个共享的 `_purge_assets`）。

### P1-7 前端列表无分页，数据被后端默认值静默截断

- `client.ts:283`：`listTasks = (status?) => request('/tasks' + (status ? '?status=' : ''))` —— **不传 limit**，后端默认 `limit=50`（`tasks.py:77`，上限 200）
- `AssetGallery` 传 `limit: '100'`，但同样是单页、无"加载更多"、无 offset

后果：任务页超过 50 条后旧任务不可见且无法翻页；`GenerateWorkspace.refreshSessionTasks` 只在最近 50 条里找会话 id，**较早的会话任务会从"本会话任务"面板凭空消失**；素材超过 100 个后旧素材不可见。

**修法**：`listTasks` 显式带 `limit` 并加分页；会话任务改用 `batch_id` 查询（后端已支持 `batch_id` 参数）。

### P1-8 上传类接口先 `resp.json()` 再判 `ok`

| 位置 | 代码 |
|---|---|
| `client.ts:155-157` | `const data = await resp.json(); if (!resp.ok) throw new Error(data?.detail \|\| '上传失败')` |
| `client.ts:209-211` | 同上（`uploadInput`） |
| `client.ts:519-520` | 同上（`uploadPlugin`） |

413（反代 `client_max_body_size`）、502/504 返回的是 HTML，`resp.json()` 先抛 `SyntaxError`，用户看到 `上传失败：Unexpected token '<', "<!DOCTYPE "...`。视频 200MB 场景最容易撞反代限制。

**修法**：先判 `resp.ok`，失败时 `try { json } catch { text }` 取 message，兜底带状态码。

### P1-9 `loadInventory` 无请求序号，快速切模板会串台

`TemplateManager.tsx:153-176`：`setInventory(await getWorkflowInventory(filename))`，无序号 / 无 AbortController。

先点模板 A（解析慢）再点模板 B（解析快）：B 先落地，A 的响应后到覆盖 `inventory`，此时 `editing` 是 B 但下拉列的是 **A 的节点**。管理员按这些选项配好 `param_mapping` 保存，就是一份指向错误节点的映射（后端只校验节点存在，不校验语义）。

同文件已有 `createLatestRequestGate()`（`listViewState.ts:17`）可复用。

### P1-10 管理员无法设置成员存储配额

- 后端 `auth_schemas.py` 的 `UserUpdateRequest` 已支持 `storage_quota_bytes`
- 前端 `client.ts:126-130` `updateUser` 的类型没有该字段，`UserManager.tsx` 也没有对应输入

整个配额体系只能靠手改数据库。这条与 P0-2 同源，但即使补了回收站 UI，没有配额设置入口，功能仍不完整。

### P1-11 `_poll_result` 的 `float(result.progress)` 无保护

`task_service.py:603-605`：

```python
if result.progress != task.progress:
    task.progress = float(result.progress)
    await db.commit()
```

`result.progress` 来自第三方插件（`GenerationResult.progress: float = 0.0`）。若插件返回 `None` / 非数值字符串，`float()` 抛异常 → 被 `execute_task` 外层 `except` 捕获 → **整个任务判 error**，而引擎侧其实还在正常跑。

**修法**：`try: progress = min(100.0, max(0.0, float(result.progress))) except (TypeError, ValueError): progress = task.progress`。

---

## 四、P2 — 可改进

| # | 位置 | 问题 |
|---|---|---|
| P2-1 | `preflight.py:132-151` | `STORAGE_DIR` / `DATABASE_URL` 不可写时**硬编码** `Issue("error", ...)`，不走 `add()` 的 development 降级；而模块 docstring 与 `add()` 都声明「development 一律降级为 warning」。结果：本地开发时目录只读会直接拒绝启动，与文档不符（虽然这两个确实致命） |
| P2-2 | `request_limits.py:29-52` | 准入只覆盖 4 个路径。`POST /api/tasks`（`params` 无大小限制）、`POST /api/templates/import`（内嵌整个工作流）无 body 上限，认证用户可发超大 JSON 吃内存 |
| P2-3 | `plugins.py:113-115` | `code = await file.read()` 后判 512KB —— 与 H5 已修掉的「先读后校验」同模式。admin-only，风险低，但与项目自己的标准不一致 |
| P2-4 | `assets.py:536-545`、`assets.py:463-471`、`asset_retention.py:52-56` | 三处都是「先 commit 删记录、再删文件」，删文件失败即永久孤儿。`asset_retention` 有分批，另两处没有 |
| P2-5 | `UsagePanel.tsx:99-102` | 「上月」`dateTo = localMonthStart(0)`；后端 `parse_day(end=True)` 把纯日期解成当天 23:59:59，所以**本月 1 号的数据会同时出现在上月和本月** |
| P2-6 | `client.ts:177`（导出）、`:488`（下载） | `a.click()` 后立刻 `URL.revokeObjectURL(url)`；同文件 `openAssetInNewTab` 特意留 60s 并注释了原因。Firefox/Safari 偶发 0 字节（**存疑**，未真机复现） |
| P2-7 | `filestore.py:177-179` | `LocalFileStore.exists` 未 catch `_resolve` 的 `OSError`，而 `read_bytes`/`open` 都 catch 并转 `FileStoreUnavailableError`。不一致 → 极端情况下 500 而非 503 |
| P2-8 | `base.py:105` + `comfyui.py:349-358` | `download_output -> bytes` 契约决定大产物全内存（200MB 视频 = 200MB 字节）。`provider-dev-guide.md` 应写明这个约束 |
| P2-9 | `quota.py:43-49` 对比 `uploads.py:143-157` | `used_bytes` 用 DB 的 `file_size` 求和；而 `uploads.py` 自己注释「file_size 可能为 0 或已经过时」并改用存储后端查权威大小。同一字段一边信一边不信，口径轻微不一致 |
| P2-10 | `quota.py:52-76` | 配额是「读-判-写」无锁，两个并发上传都能通过 → 轻微超额。文档已声明「准入控制，不是硬限流」，属已知取舍，记录备查 |
| P2-11 | `AssetGallery.tsx:98-100` | 选中集只在筛选变化时清空；`TaskPanel.tsx:55-59` 已按存活 id 过滤。素材被别处删除后 `selected` 仍留着，批量删除会对已消失 id 逐个 404 |
| P2-12 | `App.tsx:138`、`TaskPanel.tsx:92`、`GenerateWorkspace.tsx:110` | 三处同时 3s 轮询 `/api/tasks`，响应乱序会让进度显示抖动 |
| P2-13 | `requirements.txt` | 只有区间约束、无 lockfile。本机 `fastapi`/`sqlalchemy`/`pydantic`/`httpx` 均未安装，**测试无法直接运行**——「回归全绿」在当前工作副本上无法独立复现 |
| P2-14 | `README.md:156` | 声称 36 个 `test_*.py`，实际 **37 个**（`test_asset_trash.py` 未计入）；`verify_*.py` 38 个与实况一致 |
| P2-15 | `models.py:99-102` | `Task.status` 的 comment 只写 `pending/running/done/error`，缺 `cancelled`（终态实际有四类） |
| P2-16 | `client.ts:51-55`、`:434-438`、XHR `:257-261` | 401 一律 `clearToken(); window.location.reload()`。reload 立即销毁执行栈，`throw new Error('登录已过期')` 的文案**没有任何 UI 能接住**，用户被无解释地踢回登录页，正在填的表单全丢 |

---

## 五、已核实无问题（下轮不必重审）

以下均逐行对照源码确认，**不是**照抄历史报告：

**任务链路**（`task_service.py`）
- `pending→running` 持 `thumbnail_lifecycle` 锁 + 重读 + 状态判断，堵住头端竞态（`:332-342`）
- `_finish_task` 在锁内重读、检查 `_TERMINAL_STATUSES` 与 `_cancel_requested` 才提交 `done`（`:275-292`）
- submit 返回后补一刀取消引擎，防止「取消早于拿到 prompt_id」的漏网（`:390-395`）
- `_poll_result` 60s 宽限 + 判败前先 `provider.cancel` + 超时也 cancel（`:568-616`）
- `_background_tasks` 强引用 + `add_done_callback` 自注销（`:99-102`）
- `cancel_background_tasks` 有超时，不会让 graceful shutdown 等 30 分钟（`:106-133`）
- staged assets 失败时 `rollback` + 清文件，不留半成品（`:293-295`）

**缩略图**（`asset_media.py` + `thumbs.py`）
- 全局 `Semaphore(1)` 串行解码 + `Image.MAX_IMAGE_PIXELS = 100M`（`asset_media.py:41`、`thumbs.py:24`）
- 首访 3s 超时回退原图、生成任务后台继续（`asset_media.py:227-247`）
- 按 `derived_key` 去重、`WeakValueDictionary` 生命周期锁、ETag + max-age（`asset_media.py:100-118`、`:196-208`）
- `wait_for_task_completion` 用 `asyncio.wait` 而非 `wait_for(shield())`，取消语义正确（`thumbs.py:139-181`）

**安全**
- `safe_key` 拒绝绝对路径/盘符/`..`，`_resolve` 再 `resolve()` + `is_relative_to` 纵深防御，`open()` 检查 `S_ISREG`（`filestore.py:70-95,158-167,228-231`）
- 下载票据：64-bit id + 256-bit secret、`hmac.compare_digest`、消费即删、Cookie `Path` 限定到单票 + `HttpOnly` + `SameSite=Strict`（`download_tickets.py`）
- 插件加载：AST 依赖白名单、保留名 `{"comfyui","generic_api"}`、只认本文件定义的类、`sys.modules` 用完即清、单插件失败不拖垮主服务（`plugin_loader.py`）
- 上传：`read_upload_bounded` 分块读 + 即时封顶；`from-asset` 用 `handle.size` 权威大小且读盘前判（`upload_rules.py:67-89`、`uploads.py:143-179`）
- 登录：5 次失败锁 900s、锁定期失败不续期、停用账号也计入失败、改密递增 `token_version` 立即吊销旧 token（`login_guard.py`、`auth.py:52-59`、`auth_api.py:47-63,104,123`）
- `safe_detail` 三层脱敏（URL / Authorization / secret 赋值），上传与引擎链路共用
- `attachment_disposition` 走 RFC 5987，中文文件名不再 500（`disposition.py`）
- 前端无 `dangerouslySetInnerHTML` / `innerHTML`，无 XSS 注入面（已全量 grep）

**并发/资源**
- Provider `lease` 引用计数 + `retire` 延迟关闭 + `close_all_providers(lease_timeout)`（`factory.py`）
- 批量下载：临时文件 + 分块写 + 50 个/1GB 双上限 + 票据，内存与文件数无关（`assets.py:62-133`）
- 被删/重试时源文件与缩略图一起清（`thumbs.py:229-256`）

**前端**
- 上传事务的 generation + pending 计数，正确处理「上传中被替换 / 卸载后 setState / objectURL 泄漏」（`DynamicForm.tsx:106-215`、`uploadTransaction.ts`）
- 登录 401 被正确排除在「会话过期」之外，登录失败能显示后端 detail（`client.ts:62-67`）
- `openAssetInNewTab` 在同步栈先 `window.open` 再 fetch，规避弹窗拦截（`client.ts:462-478`）
- `TaskPanel` 已有 `loadFailed` 错误态（`:50-61`），不是「错误渲染成空态」
- Tailwind token 的 oklch 通道值写法与 config 引用匹配，构建产物正确（**本轮未复跑构建**，仅源码比对）

---

## 六、修复优先级

**立即（用户每天都会撞到）**
1. P0-1 删除契约 —— 改类型 + 删两处 `file_removed` 分支 + 改文案（约 10 行）
2. P0-2 回收站/配额接入（client 5 个方法 + 类型 + 素材库配额条/回收站视图 + 用户管理配额列）

**紧随（正确性/环境）**
3. P1-1 websockets 约束改 `>=13.0`（1 行，但换环境才暴露）
4. P1-2 时间统一 `parseServerTime`（一处 helper + 5 个调用点）
5. P1-3 四处软删除隔离补齐（4 行）
6. P1-4 导出补 `is_active` 判断（1 行）
7. P1-5 补 `test_quota.py`
8. P1-6 `empty_trash` 分批

**排期**
9. P1-7~P1-11 前端分页/上传错误体/请求序号/配额字段/进度容错
10. P2 全部

**建议流程**：`7b9f5dc` 之后那 93 文件的重构（素材独立化、UI v3、部署收口）从未被审过，建议按本次同样标准再过一轮；本次修完 P0/P1 后，把 `test_quota.py` 和回收站 UI 一起做，避免「后端有、前端无」的第三次发生。

---

*本报告独立于 10-30 / 11-01 / 11-02 三份历史报告，所有行号与结论均基于 `6aabe54` + 当前工作区源码。标注「存疑」的两条未取得运行期证据。*

---

## 七、修复记录（2026-11-03 同日）

本节记录针对上文清单的修复。**验证方式**：`backend/tests/test_*.py` 38 个文件全绿、`frontend` `tsc -b` + `vite build` 通过、`npm run test:state` 38 条全绿，并起了一个临时实例用 Playwright 实跑「上传 → 删除 → 回收站 → 恢复 → 超配额上传」全链路（截图核对，无 console 错误）。

### P0

| 项 | 处理 |
|---|---|
| P0-1 删除契约 | `client.ts` 的 `deleteAsset` 返回类型改为 `{message,id,deleted_at}`；`AssetGallery` 删掉两处 `!res.file_removed` 误报分支；确认框文案改为「移入回收站 / 可恢复 / 空间不立即释放」；批量按钮改名「移入回收站 (N)」 |
| P0-2 回收站 / 配额接入 | `client.ts` 新增 `listTrash`/`restoreAsset`/`purgeAsset`/`emptyTrash`/`getQuota`；`types` 补 `Asset.deleted_at`、`User.storage_quota_bytes`、`StorageQuota`；素材库加配额条（含百分比 + 进度条 + 满额置灰上传）与回收站视图（恢复 / 彻底删除 / 清空 / `remaining` 提示）；用户管理加「存储配额」列（GB 编辑，onBlur 提交；管理员显示「不限」） |

### P1

| 项 | 处理 |
|---|---|
| P1-1 websockets | `requirements.txt` 改 `>=13.0`；`ensure_running` 改为探测真正用到的 `websockets.asyncio.client`；`_run` 的导入加 `ImportError` 兜底 |
| P1-2 时间少 8 小时 | 新增 `src/time.ts`（`parseServerTime` 缺时区补 `Z`、纯日期按日历日处理），替换 5 处格式化调用；补单测 |
| P1-3 软删除隔离 | `add_tag`/`remove_tag`/`share_asset`/`upload_from_asset` 统一 `deleted_at is not None → 404`；补测试 |
| P1-4 导出越权 | `export_template` 补 `not is_active and role != admin → 404` |
| P1-5 配额零测试 | 新增 `tests/test_quota.py`（8 条：回收站计入、边界等于、超限文案、admin 豁免、0=不限、摘要形状、端点、human_bytes） |
| P1-6 empty_trash 不分批 | 抽出 `asset_retention.purge_matching()`，`purge_expired` 与 `empty_trash` 共用（50/批 × 20 批），返回 `remaining` 提示未清完 |
| P1-7 列表截断 | `listTasks` 支持 `limit/offset/batch_id`；任务面板加「加载更早的任务」（上限 200）；会话任务改用 `getTask(id)` 补拉掉出窗口的任务 |
| P1-8 上传先 json 后判 ok | 新增 `responseErrorMessage`（先读 text 再 JSON.parse，兜底状态码），三处上传接口改「先判 ok」 |
| P1-9 loadInventory 串台 | 加 `createLatestRequestGate()` 请求序号 |
| P1-10 配额设置入口 | 随 P0-2 一并补齐（`updateUser` 支持 `storage_quota_bytes`） |
| P1-11 进度字段容错 | `_poll_result` 的 `float(progress)` 夹到 0..100，异常时保持原值 |

### P2（顺手修）

- 准入扩到 `POST /api/tasks`（1MB）与 `/api/templates/import`（32MB）
- `plugins.py` 改为先看 `file.size` 再 `read_upload_bounded`（分块 + 即时封顶）
- `LocalFileStore.exists` 的 `OSError` 转 `FileStoreUnavailableError`（503 而非 500）
- 「上月」右界改 `localMonthEnd(-1)`，不再把本月 1 号算进上月
- 导出/单文件下载的 `revokeObjectURL` 延后 60s
- 素材 `selected` 随列表刷新剔除失效 id
- `preflight` docstring 与实现对齐（存储/DB 不可写即便 dev 也拒绝启动）
- `Task.status` 注释补 `cancelled`；README 测试计数 36 → 38

### 修复过程中新发现的问题（本条最严重）

**`main.py` 的 SPA 静态托管在默认配置下完全失效**：`_DIST = Path(settings.FRONTEND_DIST)` 保留了相对路径，而防穿越判断用的是 `candidate.is_relative_to(_DIST)`，`candidate` 是 `resolve()` 过的绝对路径 —— 绝对 vs 相对恒为 False，**每个 `/assets/*.js` `/assets/*.css` 请求都回落成 `index.html`**。浏览器按严格 MIME 校验拒绝执行，页面白屏。默认 `FRONTEND_DIST=../frontend/dist` 正好是相对路径，所以「单进程免 nginx 部署」这条路是断的；容器里若传绝对路径则不会暴露。已改为 `Path(...).resolve()`，并补 `test_dist_path_is_absolute` / `test_static_asset_is_served_before_index_fallback` 两条回归。

### 仍然保留的已知取舍（未改）

- 配额并发 TOCTOU（文档已声明「准入控制，非硬限流」）
- `used_bytes` 用 DB `file_size` 求和，与 `uploads.py` 查存储后端权威大小的口径不一致
- 401 仍是 `clearToken + reload`，正在填的表单会丢（改动涉及全局交互，单独排期）
- `download_output -> bytes` 契约决定大产物全内存（属 Provider 接口设计，需配文档说明）
- 三处 3s 轮询并存（`App` / `TaskPanel` / `GenerateWorkspace`）

