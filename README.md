# ComfyUI 工作台

多引擎 AI 生成平台：统一的任务队列 + 模板化出图/出视频 + 素材库，底层引擎可插拔——内置 ComfyUI 与通用 HTTP API，也能通过上传 Provider 插件热接入任意云厂商（如 Agnes Image 2.5 Flash），不改一行平台代码。

## 功能

**生成与任务**
- 任务队列：并发闸门、进度轮询、超时自动取消、僵尸任务收割、手动取消（独立 `cancelled` 终态）
- 模板系统：一套表单驱动不同引擎；「从能力带入」按引擎声明自动生成表单字段，值域即 API 原生值（零映射层）
- 模板导出 / 导入：导出为自包含 JSON（配置 + 内嵌工作流），换台机器导入即可用；重名可覆盖或另存，缺引擎只告警不拒绝
- 参数列表化：`FieldSpec.multiple` 与类型正交，一个字段可收多张图 / 多段音频（不用开 `image2/audio2` 这种槽位）
- ComfyUI 深度支持：工作流 JSON 解析、参数注入映射、出图节点识别、workflow-inventory

**多引擎插件机制**
- 一家供应商 = 一个 `GenerationProvider` 子类 `.py`，管理页上传即注册生效（不发版、不重启）
- 能力声明（`capabilities`）驱动模板表单；运行配置（API Key 等）存 DB 不进代码
- 内置引擎仅 `comfyui`；其他供应商一律走插件机制（参考实现：`backend/examples/provider_agnes.py`、`provider_grsai*.py`）。早期的 `generic_api` 通用适配器已移除

**素材库与存储**
- 素材独立于任务：生成产物自动入库，也可**直接上传**本地文件（不经过引擎），之后作为生成输入复用
- 缩略图双轨：视频落盘时用 ffmpeg 抽帧，图片首访懒生成（带并发闸门与像素上限）
- 标签体系、按类型/场景/创建人/日期筛选、批量删除
- 批量下载走**一次性票据 + 浏览器原生流式 ZIP**，不把整包读进页面内存
- 素材可手动共享给团队（共享 ≠ 共同所有：可见可下载，不可改）
- 存储后端可切换：本机目录 / MinIO（S3 兼容），业务代码不感知（`app/filestore.py`）

**用户与统计**
- bcrypt + JWT，admin / member 角色，改密与密码重置
- 数据按用户隔离，可手动共享给他人
- 用量统计（任务数、存储占用、生成耗时），时间段切换，仅 admin 可见，只统计不拦截

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI + SQLAlchemy(async) + SQLite(aiosqlite)，uvicorn |
| 前端 | React 18 + TypeScript + Vite + Tailwind CSS |
| 存储 | 本地目录（默认）或 MinIO / S3 兼容对象存储 |
| 引擎 | ComfyUI（WebSocket/HTTP）、任意 HTTP 生成 API（插件接入） |

单进程部署：后端检测到 `frontend/dist` 会直接托管前端静态资源，一个 uvicorn 进程即可对外服务，无需 nginx。

## 快速开始

要求：Python 3.11+，Node.js 20+（仅开发前端时需要）。

### 1. 后端

后端必须从 `backend/` 工作目录启动；SQLite、存储、工作流和前端构建目录的相对路径都以这里为基准。

PowerShell：

```powershell
cd backend
python -m venv .venv
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Linux / macOS：

```bash
cd backend
python3 -m venv .venv
cp .env.example .env
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动前按需修改 `backend/.env`。不要从仓库根目录直接运行 uvicorn。

首次启动自动创建默认管理员 `admin`：
- 配置了 `ADMIN_INITIAL_PASSWORD` 则用该密码；
- 未配置则生成随机密码并在控制台**打印一次**，登录后请立即修改。

API 文档：http://127.0.0.1:8000/docs

### 2. 前端（开发模式）

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173，/api 自动代理到 :8000
```

### 3. 生产部署

**单机单进程**（任务执行、并发闸门、下载票据都在进程内存里，不支持多副本）。两条路径，完整步骤见 [docs/deployment.md](docs/deployment.md)：

```bash
# Docker（推荐）
docker compose up -d --build
docker logs comfyui-studio | grep "Default admin"   # 首次启动的随机管理员密码

# 或 Windows 服务（需要 NSSM）
cd frontend && npm ci && npm run build
cd ../deploy/windows && .\install-service.ps1
```

部署时设 `APP_ENV=production`：启动前会做配置自检，`SECRET_KEY` 未配置、前端产物缺失、存储目录不可写等问题会**直接拒绝启动**并给出修复提示（见 `app/preflight.py`）。

两个探针分工不同：`/api/health` 只回答「进程活着」（引擎全挂也返回 ok，给进程守护用），`/api/ready` 检查数据库/存储/前端产物是否可用（给监控和负载均衡用，失败返回 503）。

### 4. 接入 ComfyUI（可选）

`.env` 里配置 `COMFYUI_BASE_URL`（默认 `http://127.0.0.1:8188`）。不接 ComfyUI 也可以纯用云端引擎插件跑。

> 容器部署时 `127.0.0.1` 指向容器自身，必须改成局域网 IP 或 `host.docker.internal`。

## 配置

配置在 `backend/.env`（模板见 `backend/.env.example`），关键项：

所有相对路径均按后端启动时的工作目录解析，因此应从 `backend/` 启动。当前数据库实现为 SQLite + aiosqlite；配置模板不承诺 PostgreSQL 支持。

| 变量 | 说明 | 默认 |
|---|---|---|
| `APP_ENV` | `development` / `production`。production 时启动前做配置自检，关键项缺失拒绝启动 | `development` |
| `SECRET_KEY` | JWT 签名密钥，生产必配 | 空（启动临时生成，重启失效） |
| `ADMIN_INITIAL_PASSWORD` | 首次启动的管理员密码 | 空（随机生成打印一次） |
| `DATABASE_URL` | 数据库连接串 | SQLite 本地文件 |
| `COMFYUI_BASE_URL` | ComfyUI 服务地址 | `http://127.0.0.1:8188` |
| `STORAGE_BACKEND` | `local` 或 `minio` | `local` |
| `MINIO_*` | MinIO 端点/密钥/桶（`minio` 时生效） | — |
| `MAX_CONCURRENT_TASKS` | 全局生成并发上限 | `2` |
| `TASK_TIMEOUT_SECONDS` | 单任务最长等待（超时取消） | `1800` |
| `CORS_ORIGINS` | 允许的跨域来源，生产收紧为前端域名 | `*` |
| `FFMPEG_PATH` | `Settings` 字段；指定 ffmpeg 可执行文件 | 空（自动查找） |

## 开发 Provider 插件

对接一家新的生成服务只需写一个 Python 文件：

1. 读 `docs/provider-dev-guide.md`（标准与契约）
2. 抄一个现成参考实现：

   | 文件 | 适配的 API 形态 | 值得借鉴的点 |
   |---|---|---|
   | `provider_template.py` | 假想「示例云」骨架 | 最小可运行结构 |
   | `provider_agnes.py` | Agnes 图像（同步阻塞） | 同步 API 伪装成异步、`extra_body` 嵌套、多图参考 |
   | `provider_grsai.py` | Grsai Nano Banana（异步轮询） | `replyType: async` + `GET /v1/api/result` 真轮询、失败也可能带 400 |
   | `provider_grsai_gpt_image.py` | Grsai GPT-Image 2/2.5 | 同一参数名在不同模型下语义不同（比例 vs 像素）怎么拆字段 |
   | `provider_grsai_minimax_h3.py` | Grsai MiniMax H3（视频） | 多图数组、时长与分辨率联动校验、大文件下载超时 |

3. 管理页「引擎插件」上传 `.py` → 配置 API Key → 建模板时选引擎/能力 →「从能力带入」生成表单

上传即在服务端执行该 Python 文件，请只给 admin 上传权限（平台已默认如此）。

## 测试

所有命令从 `backend/` 运行。

### 单元测试（离线一条命令跑完，不碰任何真实资源）

`backend/tests/test_*.py`（45 个，不依赖 pytest）：鉴权吊销、请求准入、任务终态竞态、插件恢复守卫、输入校验、下载票据、登录限速、启动配置自检、清理脚本守卫、live 脚本自检、端口匹配、素材回收站、存储配额与默认配额、按引擎分组的并发闸门、重启后排队任务重排、图片缩略图预生成、随机数字段 schema 与 -1 哨兵展开、媒体 MIME 的跨平台一致性、整应用 ASGI 冒烟（登录→建模板→提交→列素材）等。逐个直接运行即可：

```powershell
Get-ChildItem tests\test_*.py | ForEach-Object { & .\.venv\Scripts\python.exe $_.FullName; if ($LASTEXITCODE -ne 0) { exit 1 } }
```

全部用临时 SQLite / 假 Provider / 假 HTTP，不连真实服务、不碰 `workbench.db`。

### 持续集成（CI）

`.github/workflows/ci.yml`：push 到 `main` 和每个 PR 自动触发，两个 job 并行，约 2–3 分钟。

- `backend / py3.13`（ubuntu，与生产镜像同版本）：装 `requirements-dev.txt`，先 `ruff check .`（秒级，先挂掉能省几分钟测试等待），再逐个跑 `tests/test_*.py`。测试脚本会跑完**全部**再汇总失败，不会第一个失败就中断 —— 一次构建能看到所有问题。
- `frontend / node22`：`npm ci` → `npm run lint` → `npm run test:state` → `npm run build`（`build` 内含 `tsc -b`，类型错误在这里挂）。

只跑离线单元测试。`verify_*.py` 里需要真实 ComfyUI、需要起 FastAPI 服务的那些仍在本地回归，不进 CI。

### 代码质量门禁（lint）

| 位置 | 工具 | 命令 | 规则集 |
|---|---|---|---|
| 后端 | [ruff](https://docs.astral.sh/ruff/) 0.17.3 | `ruff check .` | `backend/ruff.toml` |
| 前端 | eslint 10 + typescript-eslint | `npm run lint` | `frontend/eslint.config.js` |

两个配置都遵循同一条取舍：**只启用「报出来每一条都值得改」的规则**。ruff 刻意不启用 `UP`/`SIM`/`PIE` 这类现代化建议（不影响正确性，只持续产噪音）；eslint 刻意只开 `rules-of-hooks` 与 `exhaustive-deps` 两条，不展开 react-hooks 插件面向 React 19 Compiler 的 16 条规则集。假阳性一旦成堆，输出就会被习惯性忽略，那比不检查更糟。

配置里逐条写了每个 ignore 的理由 —— 改动前先读那段注释。

后端 lint 需要开发依赖：

```powershell
pip install -r requirements-dev.txt
ruff check .
```

`ruff check .` 会连同 `plugins/` 目录一起扫 —— 那里可能有你上传的第三方插件，那部分代码也值得过一遍规范。CI 则只检查 git 跟踪的文件（`ruff check $(git ls-files '*.py' | grep -v '^tests/')`），因为上传的插件不属于本仓库，不该让 CI 因为别人的代码变红。

ruff 的版本**精确锁定**：lint 规则集在小版本之间会变，上游某天加一条新规则就会让 CI 无缘无故变红，而改的人根本不知道自己碰到了什么。升级 lint 工具应当是显式动作。

### verify 脚本（37 个 `verify_*.py`）

不依赖 pytest。以下是运行条件清单，不代表整套脚本已经在当前提交上全量通过；完整回归、UI 和真实生成证据由 Task 7 记录。

| 类别 | 数量 | 脚本 |
|---|---:|---|
| 离线 / 进程内 | 8 | `verify_cancel.py`、`verify_download_tickets.py`、`verify_from_asset_size_gate.py`、`verify_param_coercion.py`、`verify_progress.py`、`verify_provider_lifecycle.py`、`verify_schema_idempotent.py`、`verify_video_outputs.py` |
| 临时或隔离资源，不起 FastAPI | 13 | `verify_batch_archive.py`、`verify_error_boundary.py`、`verify_filestore.py`、`verify_filestore_live.py`、`verify_grsai_plugins.py`、`verify_json_ascii.py`、`verify_json_migration.py`、`verify_json_migration_edge.py`、`verify_media_reliability.py`、`verify_multi_image.py`、`verify_queue_and_auth.py`、`verify_tag_json_each.py`、`verify_tag_persist.py` |
| 隔离 FastAPI 服务 | 14 | `verify_asset_filters.py`、`verify_delete_assets_tasks.py`、`verify_delete_semantics.py`、`verify_frontend_contract.py`、`verify_multi_image_api.py`、`verify_p0_fixes.py`、`verify_plugin_loader.py`、`verify_prod_deploy.py`、`verify_schema_normalization.py`、`verify_template_export.py`、`verify_thumbnail_perf.py`、`verify_upload_kinds.py`、`verify_usage_stats.py`、`verify_user_isolation.py` |
| 隔离服务 + 真实 ComfyUI | 2 | `verify_b5_b6.py`、`verify_from_asset_upload.py` |
| 破坏性重启（不计入 38 个 verify 脚本） | - | `e2e_restart_recovery.py`（仅 Windows） |

`verify_tag_persist.py` / `verify_tag_json_each.py` 使用**临时数据库**，不碰真实 `workbench.db`（负向断言不受真实数据干扰，崩溃不留残留）。

**破坏性 / live 脚本必须带自检 flag**：会打真实实例、修改真实数据或杀进程的脚本（共 24 个，清单见 `tests/test_live_guard.py`）未显式设置 `COMFYUI_STUDIO_LIVE_TESTS_OK=1` 时**拒绝执行**——防止对「恰好还是默认密码」的在用实例误操作。服务脚本读取 `COMFYUI_STUDIO_TEST_BASE_URL`（值是服务 origin，不含 `/api`）；未设置时为兼容手工运行而默认使用 `http://127.0.0.1:8000`。例如：

```powershell
$env:COMFYUI_STUDIO_TEST_BASE_URL = "http://127.0.0.1:8765"
$env:COMFYUI_STUDIO_LIVE_TESTS_OK = "1"
.\.venv\Scripts\python.exe tests\verify_p0_fixes.py
```

前端从 `frontend/` 运行永久测试脚本；`test:state` 通过 `tsx` 在 Node 20+
执行 TypeScript 状态测试，不依赖实验性的类型剥离参数，`test:ui` 会先构建再用
项目声明的 Playwright 和系统 Edge 运行拦截式浏览器套件：

```powershell
npm ci
npm run test:state
npm run test:ui
```

服务脚本固定用 `admin` / `admin123`。只可对全新、可丢弃的数据库设置 `ADMIN_INITIAL_PASSWORD=admin123`；已有管理员时该变量不会重置密码。服务回归会创建、修改和删除数据库行、插件文件、工作流及素材，禁止指向用户正在使用的服务。

部分脚本还有额外前置条件：

- `verify_prod_deploy.py`：先在 `frontend/` 执行 `npm ci` 和 `npm run build`，服务的 `FRONTEND_DIST` 必须指向这份非空构建产物；隔离数据库还要至少有一个模板，才能验证业务响应不是空壳。
- `verify_template_export.py`：隔离服务中必须预置至少一个绑定现有工作流文件的模板，并让 `WORKFLOW_DIR` 指向对应工作流快照。
- `verify_b5_b6.py`：B5 完全在进程内运行，使用临时 SQLite、真实 `TaskService.execute_task` 和进程内假 Provider（`_B5FakeProvider`），不访问运行中服务、FileStore 或 generic/cloud HTTP。B6 才会真实提交生成任务；必须显式设置 `COMFYUI_STUDIO_B5_B6_TEMPLATE_ID` 为本轮预置模板的精确 ID。该模板必须是 `comfyui` 引擎、绑定 `image_flux2_klein_text_to_image.json`，参数映射必须精确为 `prompt -> 76.inputs.value`、`width -> 103.inputs.value`、`height -> 106.inputs.value`，输出节点必须仅为 `108`。
- `e2e_upload_generate.py`：需要真实 ComfyUI，并在任何请求前校验 `COMFYUI_STUDIO_E2E_IMAGE_PATH` 和 `COMFYUI_STUDIO_E2E_TEMPLATE_ID`；可用 `COMFYUI_STUDIO_E2E_ENGINE` 覆盖默认 `comfyui`。模板必须接受 `ref_image`、`prompt`、`width`、`height` 参数，图片路径必须是隔离测试夹具。

`verify_plugin_loader.py`、`verify_from_asset_upload.py`、`verify_usage_stats.py`、`verify_user_isolation.py` 仍直接打开 `backend/workbench.db`；`e2e_restart_recovery.py` 与维护脚本 `backfill_video_thumbs.py`、`cleanup_*.py`、`purge_test_residue.py`、`align_template_fields.py`、`fix_bool_select_options.py`、`smoke_fixes.py` 也有同样约束（这些脚本均已内置 `COMFYUI_STUDIO_LIVE_TESTS_OK` 自检，未设置即拒绝执行）。因此完整服务回归要在一次性 worktree 中使用该默认 SQLite 文件名。worktree 独占且数据库不受 Git 跟踪，测试后可删除，不会接触原工作区数据库。

`verify_filestore_live.py` 会先探测配置的 MinIO，再退回进程内 S3 线格式服务；不允许外连时不要运行它。`verify_multi_image.py` 会写配置的存储目录，也必须使用隔离 `STORAGE_DIR`。

真实 ComfyUI 分类会提交任务；`e2e_upload_generate.py` 也属于这一类。`e2e_restart_recovery.py` 是 **Windows-only、破坏性、仅限隔离 worktree** 的脚本，会终止并重启配置的本机回环端口；它拒绝远程 origin，不计入上述 36 个脚本，并明确排除在普通回归和稳定基线验证之外。UI、真实文生图和破坏性重启结果均不能由离线或服务契约脚本代替。

## 目录结构

```
backend/
  app/
    api/          # REST 路由（tasks/templates/assets/auth/plugins/stats/uploads）
    engines/      # 引擎抽象：base / comfyui / plugin_loader / factory
    services/     # 任务链路（task_service）、工作流解析
    models/       # SQLAlchemy 模型
    filestore.py  # 存储抽象（local / MinIO 双后端）
    preflight.py  # 启动前配置自检（APP_ENV=production 时严格）
  examples/       # Provider 插件模板与参考实现
  scripts/        # 运维脚本（backup.py 数据库 + 素材备份）
  tests/          # verify_*.py 验证脚本 + e2e
  workflows/      # 工作流 JSON
frontend/
  src/            # React + TS 源码
deploy/
  docker/         # 容器 entrypoint（数据卷初始化）
  windows/        # NSSM 服务注册、启停、日志、备份
docs/             # 设计文档、开发规范、部署运维（deployment.md）
Dockerfile
docker-compose.yml
```

## 安全说明

- `backend/.env` 含密钥与初始密码，已在 `.gitignore` 中排除，**不要提交**
- Provider 插件是服务端可执行的 Python，等同于代码部署权限，仅限管理员操作（上传有审计记录：上传者 + SHA-256）
- 生产环境请配置独立的 `SECRET_KEY`，并把 `CORS_ORIGINS` 收紧

### JWT 会话的已知取舍（有意识的设计决策，部署前请知悉）

- **token 存 localStorage**：XSS 可读取 token。前端当前无 CSP，若部署到不可信网络，建议前置反向代理加 CSP 头。
- **TTL 24 小时 + 登出不吊销**：登出只清本地，服务端旧 token 继续有效最多 24h。**改密码 / 管理员重置密码会立即吊销**该用户全部旧 token（`token_version` 机制，见 `app/auth.py`）。
- **登录限速**：同一「用户名 + 客户端 IP」连续 5 次失败锁定 15 分钟（`app/login_guard.py`，429 返回）。内存计数，进程重启清零。
- 如需「登出即失效」，可加服务端吊销表或缩短 `ACCESS_TOKEN_EXPIRE_HOURS`；当前按内部工具取舍，未实现。
