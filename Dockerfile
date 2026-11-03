# syntax=docker/dockerfile:1
#
# 单镜像单进程部署：构建期把前端打出来，运行期一个 uvicorn 同时托管 API 和页面。
# 与代码结构一致 —— 任务执行、并发闸门、下载票据都在进程内存里，所以不要
# 用 `docker compose up --scale app=2` 起多副本，那样两个进程会各跑各的任务队列。

# ── 阶段 1：前端构建 ──────────────────────────────
FROM node:22-alpine AS web

WORKDIR /build
# 先只拷依赖清单：源码改动不会让 npm ci 这层缓存失效
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ── 阶段 2：运行时 ───────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 镜像里一律按生产校验：缺 SECRET_KEY / 前端产物时直接启动失败
    APP_ENV=production

# bcrypt 3.x 是 passlib 1.7.4 的唯一兼容版本，它需要编译好的 wheel；
# slim 镜像不带 gcc，所以这里只依赖 manylinux wheel，不装构建工具链。
# 如果哪天 requirements 加了纯源码包，构建会在这一步报错 —— 那是要显式加
# build-essential 的信号，不要偷偷放宽依赖版本。
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./

# 前端产物放在 backend 的兄弟目录，正好匹配 FRONTEND_DIST 的默认值 ../frontend/dist
COPY --from=web /build/dist /app/frontend/dist

# 工作流种子：workflows 会挂成持久卷，首次挂载时卷是空的 —— 必须留一份
# 镜像内的副本，由 entrypoint 判断卷为空再复制进去。否则模板绑定的工作流
# 文件会「在镜像里存在、但被空卷覆盖掉」，表现是模板能列出来却提交不了。
RUN mkdir -p /app/backend/storage /app/backend/data \
    && cp -a /app/backend/workflows /opt/workflows-seed

COPY deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 运行时数据目录：数据库、素材、工作流都挂在 /app/backend/data 一个卷下，
# 备份就是打包这一个目录。数据库单独放子目录是因为 SQLite 要在同目录写
# -wal/-shm，挂单个文件会在容器重建时丢事务日志。
VOLUME ["/app/backend/data"]

EXPOSE 8000

# --proxy-headers + --forwarded-allow-ips：放在网关/反代后面时必须还原真实 IP，
# 否则 request.client.host 全是网关地址，登录限速的「按 IP 锁定」会退化成
# 「一人输错锁住所有人」。只部署在可信内网时用 * 是安全的；若直接暴露公网，
# 改成网关的实际网段。
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
