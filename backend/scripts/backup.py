"""备份脚本 —— 数据库 + 素材目录，跨平台（Windows / Linux / 容器内都能跑）。

为什么不用 cp / 复制粘贴：
SQLite 默认开 WAL，直接拷 .db 会漏掉 -wal 里还没 checkpoint 的事务，
拿到的是个"看起来完整、实际少数据"的文件。这里用 sqlite3 的 backup API，
能在服务照常写库的情况下拿到一致快照（在线备份）。

用法（从 backend/ 运行）：
    python scripts/backup.py                    # 备份到 backend/backups/<时间戳>/
    python scripts/backup.py --keep 7           # 备份后只留最近 7 份
    python scripts/backup.py --out D:/backups   # 指定输出位置（如挂载的备份盘）
    python scripts/backup.py --skip-storage     # 只备数据库（素材太大时）

恢复步骤见 docs/deployment.md「备份与恢复」。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402


def sqlite_path(url: str) -> Path | None:
    """从 DATABASE_URL 解析出 SQLite 文件路径；非 SQLite 或内存库返回 None。"""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :].strip()
    if not raw or raw == ":memory:":
        return None
    path = Path(raw)
    # DATABASE_URL 里的相对路径以 backend/ 为基准（与 uvicorn 的工作目录一致），
    # 而脚本可能从任意目录被调用，所以这里显式拼到 BACKEND 上
    return path if path.is_absolute() else (BACKEND / path)


def backup_database(src: Path, dest: Path) -> dict:
    if not src.is_file():
        raise FileNotFoundError(f"数据库不存在：{src}")
    # backup API：源库可读即可，不需要独占锁
    with sqlite3.connect(str(src)) as source, sqlite3.connect(str(dest)) as target:
        source.backup(target)
    return {"source": str(src), "file": dest.name, "bytes": dest.stat().st_size}


def backup_storage(src: Path, dest: Path) -> dict:
    if not src.is_dir():
        return {"skipped": f"存储目录不存在：{src}"}
    shutil.copytree(src, dest)
    files = 0
    total = 0
    for item in dest.rglob("*"):
        if item.is_file():
            files += 1
            total += item.stat().st_size
    return {"source": str(src), "file": dest.name, "files": files, "bytes": total}


def prune(out: Path, keep: int) -> list[str]:
    """只保留最近 keep 份备份（按目录名的时间戳排序）。keep<=0 表示不清理。"""
    if keep <= 0:
        return []
    dirs = sorted(
        (d for d in out.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=lambda d: d.name,
        reverse=True,
    )
    removed: list[str] = []
    for stale in dirs[keep:]:
        try:
            shutil.rmtree(stale)
            removed.append(stale.name)
        except OSError as exc:
            # 备份盘上的旧文件被占用不该让本次备份失败
            print(f"[backup] 清理旧备份失败 {stale.name}: {exc}", file=sys.stderr)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份数据库与素材目录")
    parser.add_argument("--out", default=str(BACKEND / "backups"), help="备份输出目录")
    parser.add_argument("--keep", type=int, default=0, help="保留最近 N 份，0 表示不清理")
    parser.add_argument("--skip-storage", action="store_true", help="跳过素材目录（只备数据库）")
    args = parser.parse_args(argv)

    db_file = sqlite_path(settings.DATABASE_URL)
    if db_file is None:
        print(
            f"[backup] DATABASE_URL 不是 SQLite 文件库（{settings.DATABASE_URL}），"
            "本脚本只支持 SQLite；请用对应数据库自己的备份工具。",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out).resolve()
    target = out_root / stamp
    if target.exists():
        print(f"[backup] 目标已存在，稍后重试：{target}", file=sys.stderr)
        return 1
    target.mkdir(parents=True)

    manifest: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": settings.APP_VERSION,
        "database": backup_database(db_file, target / "workbench.db"),
        "storage": (
            {"skipped": "--skip-storage"}
            if args.skip_storage
            else backup_storage(Path(settings.STORAGE_DIR), target / "storage")
        ),
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    removed = prune(out_root, args.keep)
    print(f"[backup] 完成：{target}")
    print(f"[backup] 数据库 {manifest['database']['bytes']} 字节")
    storage = manifest["storage"]
    if "bytes" in storage:
        print(f"[backup] 素材 {storage['files']} 个文件 / {storage['bytes']} 字节")
    if removed:
        print(f"[backup] 已清理旧备份 {len(removed)} 份：{', '.join(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
