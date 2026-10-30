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
- 内置引擎：`comfyui`、`generic_api`（可配置的通用 HTTP 生成 API）；参考实现见 `backend/examples/provider_agnes.py`

**素材库与存储**
- 生成产物自动落盘入库，缩略图、筛选、批量删除
- 存储后端可切换：本机目录 / MinIO（S3 兼容），业务代码不感知（`app/filestore.py`）
- 素材可反选为生成输入（含跨引擎传输）

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

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # 按需修改
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

首次启动自动创建默认管理员 `admin`：
- 配置了 `ADMIN_INITIAL_PASSWORD` 则用该密码；
- 未配置则生成随机密码并在控制台**打印一次**，登录后请立即修改。

API 文档：http://127.0.0.1:8000/docs

### 2. 前端（开发模式）

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173，/api 自动代理到 :8000
```

### 3. 生产部署

```bash
cd frontend && npm run build   # 产物在 frontend/dist
# 然后正常启动后端即可 —— dist 存在时由后端直接托管
```

### 4. 接入 ComfyUI（可选）

`.env` 里配置 `COMFYUI_BASE_URL`（默认 `http://127.0.0.1:8188`）。不接 ComfyUI 也可以纯用云端引擎插件跑。

## 配置

配置在 `backend/.env`（模板见 `backend/.env.example`），关键项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `SECRET_KEY` | JWT 签名密钥，生产必配 | 空（启动临时生成，重启失效） |
| `ADMIN_INITIAL_PASSWORD` | 首次启动的管理员密码 | 空（随机生成打印一次） |
| `DATABASE_URL` | 数据库连接串 | SQLite 本地文件 |
| `COMFYUI_BASE_URL` | ComfyUI 服务地址 | `http://127.0.0.1:8188` |
| `STORAGE_BACKEND` | `local` 或 `minio` | `local` |
| `MINIO_*` | MinIO 端点/密钥/桶（`minio` 时生效） | — |
| `MAX_CONCURRENT_TASKS` | 全局生成并发上限 | `2` |
| `TASK_TIMEOUT_SECONDS` | 单任务最长等待（超时取消） | `1800` |
| `CORS_ORIGINS` | 允许的跨域来源，生产收紧为前端域名 | `*` |

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

后端测试是独立脚本（不依赖 pytest），直接运行：

```bash
cd backend
python tests/verify_plugin_loader.py    # 插件加载与注册（42 项断言）
python tests/verify_multi_image.py      # 单字段多图：字段声明 / 列表保序 / 多图进请求体（离线，不用起服务）
python tests/verify_grsai_plugins.py    # Grsai 三插件线格式联调：异步轮询 / 状态映射 / 产物下载（假服务端）
python tests/verify_queue_and_auth.py   # 队列闸门与鉴权
python tests/verify_filestore.py        # 存储抽象
python tests/verify_template_export.py  # 模板导出/导入：自包含工作流、去重、重名、缺引擎告警
# ...其余 verify_*.py 同理，全部通过输出 PASS
```

`backend/tests/e2e_*.py` 为端到端脚本（起服务真实跑任务），按需执行。

## 目录结构

```
backend/
  app/
    api/          # REST 路由（tasks/templates/assets/auth/plugins/stats/uploads）
    engines/      # 引擎抽象：base / comfyui / generic_api / plugin_loader / factory
    services/     # 任务链路（task_service）、工作流解析
    models/       # SQLAlchemy 模型
    filestore.py  # 存储抽象（local / MinIO 双后端）
  examples/       # Provider 插件模板与参考实现
  tests/          # verify_*.py 验证脚本 + e2e
  workflows/      # 工作流 JSON
frontend/
  src/            # React + TS 源码
docs/             # 设计文档、开发规范（provider-dev-guide.md）
```

## 安全说明

- `backend/.env` 含密钥与初始密码，已在 `.gitignore` 中排除，**不要提交**
- Provider 插件是服务端可执行的 Python，等同于代码部署权限，仅限管理员操作（上传有审计记录：上传者 + SHA-256）
- 生产环境请配置独立的 `SECRET_KEY`，并把 `CORS_ORIGINS` 收紧
