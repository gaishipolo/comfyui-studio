# 部署与运维

面向已经跑通本地开发、要交付给团队使用的场景。两种方式二选一：**Docker**（推荐，跨平台）或 **Windows 服务**。

---

## 0. 先理解一个前提：单机单进程

任务执行、并发闸门（`_run_slots` 信号量）、取消标记、一次性下载票据、Provider 实例缓存**全部在进程内存里**。

这意味着：

- ✅ 一个进程同时托管 API 和前端页面，部署简单、无需 nginx
- ✅ 进程重启时能收割遗留任务：有引擎任务号的**接回去继续跑**，没提交成功的标成可重试
- ❌ **不能起多副本**（`--scale app=2`、多机负载均衡都不行）。任务提交在 A 进程、轮询也在 A 进程，B 进程完全不知道它的存在，会表现为「任务永远 running」

要横向扩展得先把执行层拆成独立 worker + 外部队列（PG/Redis），那是另一次改造。当前形态下的正确做法是：**单机 + 足够大的并发上限**（`MAX_CONCURRENT_TASKS`，默认 2，按引擎显存调）。

---

## 1. Docker

### 构建与启动

```bash
# 在仓库根目录
docker compose up -d --build
```

首次启动做三件事：建库、创建 `admin`、把镜像内置的 4 个工作流种子复制进数据卷。**管理员初始密码只打印一次**，从日志里取：

```bash
docker logs comfyui-studio | grep "Default admin"
```

不想用随机密码就在 `backend/.env` 里设 `ADMIN_INITIAL_PASSWORD`（仅首次建库时生效）。

### 数据都在一个卷里

```
./data/
  workbench.db        # SQLite 主库（同目录还有 -wal / -shm）
  storage/            # 素材文件
  workflows/          # 工作流 JSON
```

备份就是打包这一个目录。容器重建、升级镜像都不会动它。

> **为什么 `workflows` 也要挂卷**：模板表里存的是工作流文件名。如果卷是空的，镜像里的文件会被空卷盖掉，表现是「模板能列出来，点提交才报错」——很难查。所以 entrypoint 会检测卷为空并补种子，日志里会打印 `[Entrypoint] 数据卷为空，已初始化工作流种子`。

### 连 ComfyUI：容器里的 127.0.0.1 不是你的机器

这是最容易踩的坑。`COMFYUI_BASE_URL=http://127.0.0.1:8188` 在容器里指向**容器自己**，一定连不上，`/api/health` 会一直显示 `disconnected`。

```bash
# backend/.env 或 compose 的 environment
COMFYUI_BASE_URL=http://192.168.77.155:8888   # 用局域网 IP
```

Windows 上如果 ComfyUI 跑在宿主机，用 `http://host.docker.internal:8188`。

### 只想要镜像、自己写 compose

```bash
docker build -t comfyui-studio:latest .
docker run -d --name comfyui-studio -p 8000:8000 \
  -e APP_ENV=production \
  -e SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e COMFYUI_BASE_URL=http://192.168.77.155:8888 \
  -v "$PWD/data:/app/backend/data" \
  comfyui-studio:latest
```

---

## 2. Windows 服务

需要 [NSSM](https://nssm.cc/download)（把 `win64\nssm.exe` 放进 `PATH`）。直接注册 `python.exe` 不行——Windows 服务要求进程响应服务控制协议，普通脚本会报「服务没有响应控制功能」。

```powershell
# 首次：构建前端（服务托管的是构建产物，不是 vite dev server）
cd frontend
npm ci
npm run build

# 注册并启动
cd ..\deploy\windows
.\install-service.ps1

# 日常
.\manage.ps1 status        # 服务状态 + 打 /api/ready
.\manage.ps1 restart
.\manage.ps1 logs -Tail 100
.\manage.ps1 backup        # 调 backend\scripts\backup.py --keep 7

# 卸载（不删数据库和素材）
.\uninstall-service.ps1
```

`install-service.ps1` 做的事：工作目录设为 `backend\`、日志写到 `logs\service.*.log` 并按 10MB 滚动、崩溃 5 秒后自动重启、`APP_ENV=production`、开机自启。

指定解释器和端口：

```powershell
.\install-service.ps1 -Port 8080 -Python "C:\Python313\python.exe"
```

---

## 3. 配置自检（启动时自动执行）

`APP_ENV=production` 时，关键配置不合格会**直接中止启动**，而不是带着隐患跑。日志里会有 `[Preflight]` 段落，每条都带修复提示。

| 检查项 | production | development |
|---|---|---|
| `SECRET_KEY` 未配置 | **ERROR 中止** | 警告 |
| `SECRET_KEY` 短于 32 字符 | 警告 | 警告 |
| `CORS_ORIGINS` 含 `*` | 警告 | 警告 |
| `FRONTEND_DIST` 缺 `index.html` | **ERROR 中止** | 警告 |
| `STORAGE_DIR` 不可写 | **ERROR 中止** | **ERROR 中止** |
| 数据库目录不可写 | **ERROR 中止** | **ERROR 中止** |
| `MAX_CONCURRENT_TASKS < 1` | **ERROR 中止** | 警告 |
| 找不到 ffmpeg | 警告 | 警告 |

为什么 `SECRET_KEY` 必须配：留空时程序会临时生成一个，进程一重启**所有登录态立即失效**，用户全部被踢回登录页。本地开发无所谓，生产就是事故。

存储和数据库目录不可写在两种模式下都是 ERROR——那不是「配置不理想」，是「一定会坏」。

跳过检查（不建议）：`APP_ENV=development`。

---

## 4. 两个探针，别用错

| 端点 | 回答的问题 | 用途 | 失败意味着 |
|---|---|---|---|
| `GET /api/health` | 进程活着吗 | 存活探针 / 进程守护 | 该重启 |
| `GET /api/ready` | 现在能接业务流量吗 | 监控 / 负载均衡 | **别重启**，先查依赖 |

`/api/ready` 做三项真实检查，不是读配置：

```json
{"status":"ready","checks":{"database":"ok","storage":"ok","frontend":"ok"}}
```

- `database`：真跑一次 `SELECT 1`（2 秒上限）。文件锁死、磁盘满会在这里暴露
- `storage`：真写一个临时文件再删。`os.access` 在容器挂载卷上不可靠（root 无视权限位）
- `frontend`：真找 `index.html`。缺了就是 503 页面

任一项失败返回 **HTTP 503**，`status` 为 `not_ready`。

`/api/health` 故意不检查依赖：引擎全挂也返回 ok。如果它去探测数据库，数据库一慢就会触发误重启，越重启越糟。

---

## 5. 反向代理与真实 IP（关键）

如果前面挂了 nginx / 网关 / 云负载均衡，**必须让应用拿到真实客户端 IP**。

原因：登录限速（`app/login_guard.py`）按 `(用户名, IP)` 计数，连错 N 次锁定。拿不到真实 IP 时所有人共用网关地址，**一个人输错密码会把全公司锁住**；反过来攻击者换账号就能绕过。

应用侧已经开了 `--proxy-headers --forwarded-allow-ips *`（Dockerfile 和 Windows 脚本里都有）。

nginx 侧必须传：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 200m;   # 素材上传，按 UPLOAD 上限调
}
```

> `--forwarded-allow-ips *` 表示「信任任何来源的 X-Forwarded-For」。只部署在可信内网时安全；**直接暴露公网时**要改成网关的实际网段，否则客户端可以自己伪造 `X-Forwarded-For` 绕过限速。

---

## 6. 备份与恢复

### 备份

```bash
cd backend
python scripts/backup.py --keep 7            # 备份到 backend/backups/，只留最近 7 份
python scripts/backup.py --out D:/backups    # 指定备份盘
python scripts/backup.py --skip-storage      # 素材太大时只备数据库
```

Docker 里：

```bash
docker compose exec app python scripts/backup.py --keep 7
```

**为什么不用 `cp workbench.db`**：SQLite 开了 WAL，直接拷主库会漏掉 `-wal` 里还没 checkpoint 的事务，拿到的是「看起来完整、实际少数据」的文件。脚本用 `sqlite3` 的 backup API，服务照常写库也能拿到一致快照。

每次备份产出：

```
backups/20261103-095625/
  workbench.db      # 一致性快照
  storage/          # 素材副本（--skip-storage 时没有）
  manifest.json     # 时间、来源路径、文件数、字节数
```

建议配成计划任务（Windows）或 cron（Linux），`--keep 7` 防磁盘写满。

### 恢复

```bash
# 1. 停服
docker compose down          # 或 .\manage.ps1 stop

# 2. 备份现场（别跳过这步，恢复错了还有退路）
mv data/workbench.db data/workbench.db.broken

# 3. 放回快照。注意删掉旧的 -wal / -shm，否则会和新库对不上
cp backups/20261103-095625/workbench.db data/
rm -f data/workbench.db-wal data/workbench.db-shm

# 4. 素材（如果备份里带 storage/）
rsync -a backups/20261103-095625/storage/ data/storage/

# 5. 启动
docker compose up -d
```

启动后用 `/api/ready` 确认三项都 ok，再登录看数据。

---

## 7. 升级

```bash
# 1. 备份（必做）
python scripts/backup.py --keep 7

# 2. Docker
docker compose up -d --build

# 3. Windows 服务
cd frontend && npm ci && npm run build
cd ..\deploy\windows && .\manage.ps1 restart
```

数据库 schema 迁移在启动时自动执行，**幂等**（`app/database.py` 的 `_COLUMN_MIGRATIONS` 与 JSON 列转义改写都设计成可重复执行），不需要手工跑 SQL。

启动时的遗留任务处理：有引擎任务号的接回去继续等结果（不会重新提交，prompt 已经在引擎上），没提交成功的标成 `error` 可点重试。所以**升级时正在跑的长视频任务不会白跑**。

---

## 8. 排障对照表

| 现象 | 大概率原因 | 怎么确认 |
|---|---|---|
| 启动即退出，日志有 `[Preflight] ... ERROR` | 关键配置缺失 | 按提示逐条修；`APP_ENV=development` 可临时跳过 |
| 页面 503「前端未构建」 | `FRONTEND_DIST` 下没有 `index.html` | 重新 `npm run build` |
| `/api/ready` 里 `database` 报错 | 库文件被锁 / 磁盘满 / 目录只读 | 查磁盘余量、目录权限 |
| `/api/ready` 里 `storage` 报错 | 卷挂载权限不对 | 容器内 `touch /app/backend/data/storage/x` 试 |
| 服务活着但 `/api/health` 一直 `disconnected` | 引擎地址不对（容器里用了 127.0.0.1） | 改成局域网 IP，或 `host.docker.internal` |
| 任务提交成功但一直 running | 引擎连不上 / 该引擎任务丢失 | 看任务详情里的 `_request` 快照和引擎日志 |
| 所有人同时被锁登录 | 反代没还原真实 IP | 查 nginx 的 `X-Forwarded-For` 配置 |
| 视频素材没有封面图 | 缺 ffmpeg | 装 `imageio-ffmpeg` 或系统 ffmpeg；启动日志有 WARN |
| 模板能列出来但提交报错 | 工作流文件不在 `WORKFLOW_DIR` | 看目录里有没有对应 JSON；Docker 首次启动看 entrypoint 日志 |
