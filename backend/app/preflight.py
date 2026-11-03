"""启动前配置自检 —— 生产模式下缺关键配置就拒绝启动。

为什么要有这一层：这个应用是单进程部署（任务执行、并发闸门、下载票据都在进程内存里），
启动失败时运维只有 uvicorn 的 traceback 可看，很难定位是「SECRET_KEY 没配」还是
「存储卷只读」。所以在建库、恢复插件、收割任务**之前**先把配置体检一遍，
把问题按「必须修」和「建议改」分开，并且每条都给出可操作的修复提示。

设计取舍：
- `collect_issues` 只接收参数、不读全局 settings —— 单测能直接构造各种坏配置。
- 检查项本身不做副作用（除了写一个临时探针文件再删掉），可以反复调用。
- development 环境一律降级为 warning：本地开发不该被生产要求卡住。
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Level = Literal["error", "warning"]

# SECRET_KEY 低于这个长度就不该当生产密钥用。32 字符 = 256 bit 熵的下限体感值，
# 比密码学要求宽松（HS256 本身不要求长度），目的是拦住 "abc123" 这类手填值。
_MIN_SECRET_LEN = 32

_PRODUCTION_ALIASES = {"production", "prod"}


def is_production(env: str) -> bool:
    return env.strip().lower() in _PRODUCTION_ALIASES


@dataclass(frozen=True)
class Issue:
    level: Level
    key: str
    message: str
    hint: str = ""


def _sqlite_file(database_url: str) -> Path | None:
    """从 DATABASE_URL 取出 SQLite 文件路径；非 SQLite 或内存库返回 None。"""
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw = database_url[len(prefix) :].strip()
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def dir_writable(path: Path) -> str | None:
    """目录能否创建/写入。可写返回 None，否则返回失败原因。

    用 tempfile 在目标目录里落一个探针文件 —— `os.access` 在容器挂载卷上
    不可靠（root 无视权限位），实际写一次才是真的。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=path, prefix=".preflight-", suffix=".tmp")
        os.close(fd)
        os.unlink(probe)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def collect_issues(
    *,
    env: str,
    secret_key: str,
    cors_origins: str,
    frontend_dist: Path,
    storage_dir: Path,
    database_url: str,
    max_concurrent: int,
    ffmpeg_available: bool | None = None,
) -> list[Issue]:
    """体检当前配置，返回全部问题（不抛异常）。

    `ffmpeg_available` 传 None 表示不检查 —— 由调用方用 `thumbs.ffmpeg_exe()`
    判断后传入，避免本模块反向依赖缩略图实现。
    """
    production = is_production(env)
    issues: list[Issue] = []

    def add(level: Level, key: str, message: str, hint: str = "") -> None:
        # 开发环境把 error 降级：本地没有 dist、没配密钥都是正常的
        issues.append(Issue("error" if production and level == "error" else "warning",
                            key, message, hint))

    # ── 安全 ────────────────────────────────
    key = (secret_key or "").strip()
    if not key:
        add(
            "error",
            "SECRET_KEY",
            "SECRET_KEY 未配置，启动时会临时生成，进程一重启所有登录态立即失效",
            "生成一个随机值写进 backend/.env："
            'python -c "import secrets;print(secrets.token_urlsafe(48))"',
        )
    elif len(key) < _MIN_SECRET_LEN:
        add(
            "warning",
            "SECRET_KEY",
            f"SECRET_KEY 只有 {len(key)} 个字符，短于建议的 {_MIN_SECRET_LEN}",
            "生产环境请换成长随机串，避免被离线穷举",
        )

    origins = [o.strip() for o in (cors_origins or "").split(",") if o.strip()]
    if "*" in origins:
        add(
            "warning",
            "CORS_ORIGINS",
            "CORS_ORIGINS 为 *，任意站点都能发起跨域请求",
            "单进程部署前后端同源时，收紧为实际访问域名（逗号分隔）",
        )

    # ── 前端产物 ─────────────────────────────
    index = Path(frontend_dist) / "index.html"
    if not index.is_file():
        add(
            "error",
            "FRONTEND_DIST",
            f"找不到前端产物：{index}",
            "在 frontend/ 执行 npm ci && npm run build；"
            "或用 FRONTEND_DIST 指向已有构建目录",
        )

    # ── 存储 ────────────────────────────────
    storage_err = dir_writable(Path(storage_dir))
    if storage_err:
        issues.append(Issue(
            "error",
            "STORAGE_DIR",
            f"存储目录不可写：{storage_dir}（{storage_err}）",
            "检查目录权限或容器卷挂载；素材写入失败会让所有任务在收尾阶段报错",
        ))

    db_file = _sqlite_file(database_url)
    if db_file is not None:
        db_parent = db_file.parent if str(db_file.parent) else Path(".")
        db_err = dir_writable(db_parent)
        if db_err:
            issues.append(Issue(
                "error",
                "DATABASE_URL",
                f"数据库所在目录不可写：{db_parent}（{db_err}）",
                "SQLite 需要在同目录创建 -wal / -shm 文件，目录只读会导致写库失败",
            ))

    # ── 运行参数 ─────────────────────────────
    if max_concurrent < 1:
        add(
            "error",
            "MAX_CONCURRENT_TASKS",
            f"MAX_CONCURRENT_TASKS={max_concurrent}，至少为 1",
            "引擎显存有限时建议 1~2，别配 0",
        )

    if ffmpeg_available is False:
        add(
            "warning",
            "FFMPEG",
            "未找到 ffmpeg，视频素材不会有封面图（会退回拉整个文件显示首帧）",
            "装系统 ffmpeg、pip install imageio-ffmpeg，或用 FFMPEG_PATH 指定路径",
        )

    return issues


def format_issues(issues: list[Issue], *, env: str) -> str:
    """把体检结果排成可读文本；没有问题返回空串。"""
    if not issues:
        return ""
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    lines = [
        f"[Preflight] 配置自检（APP_ENV={env}）："
        f"{len(errors)} 项必须修复，{len(warnings)} 项建议处理"
    ]
    for item in errors:
        lines.append(f"  [ERROR] {item.key}: {item.message}")
        if item.hint:
            lines.append(f"          → {item.hint}")
    for item in warnings:
        lines.append(f"  [WARN ] {item.key}: {item.message}")
        if item.hint:
            lines.append(f"          → {item.hint}")
    return "\n".join(lines)
