#!/bin/sh
# 容器启动前的数据卷初始化。
#
# 只有一个职责：工作流目录挂的是持久卷，首次挂载时卷是空的，会把镜像里
# 内置的工作流文件「盖掉」。模板表里存的是工作流文件名，文件不见了的表现
# 是模板能列出来、点提交才报错 —— 很难查。所以卷为空时先补种子。
set -eu

DATA_DIR="${DATA_DIR:-/app/backend/data}"
SEED_DIR="/opt/workflows-seed"

mkdir -p "$DATA_DIR/storage" "$DATA_DIR/workflows"

if [ -d "$SEED_DIR" ] && [ -z "$(ls -A "$DATA_DIR/workflows" 2>/dev/null || true)" ]; then
    cp -a "$SEED_DIR/." "$DATA_DIR/workflows/"
    echo "[Entrypoint] 数据卷为空，已初始化工作流种子：$(ls "$DATA_DIR/workflows" | wc -l) 个文件"
fi

exec "$@"
