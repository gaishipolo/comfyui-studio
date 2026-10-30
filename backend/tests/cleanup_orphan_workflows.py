"""清理没被任何模板引用的工作流文件（默认 dry-run）

工作流文件只增不减：测试跑一次、导入一次就可能留下副本，删模板也不清文件。
时间长了 `backend/workflows/` 里会有一堆没人知道干什么的 JSON。

**默认只列不删**，确认无误再加 --apply。判断依据是「没有任何模板的
workflow_file 指向它」—— 注意这也会列出「刚上传还没建模板」的文件，
所以删除前请自己看一眼。

用法：
    python tests/cleanup_orphan_workflows.py
    python tests/cleanup_orphan_workflows.py --apply
    python tests/cleanup_orphan_workflows.py --pattern evil_smoke_ --apply
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import async_session  # noqa: E402
from app.models.models import Template  # noqa: E402


async def referenced_files() -> set[str]:
    async with async_session() as db:
        rows = (await db.execute(select(Template.name, Template.workflow_file))).all()
    return {wf for _, wf in rows if wf}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列）")
    ap.add_argument("--pattern", default="", help="只处理文件名包含该子串的（如 evil_smoke_）")
    args = ap.parse_args()

    refs = asyncio.run(referenced_files())
    orphans = []
    for path in sorted(settings.WORKFLOW_DIR.glob("*.json")):
        if path.name in refs:
            continue
        if args.pattern and args.pattern not in path.name:
            continue
        orphans.append(path)

    if not orphans:
        print("没有未被引用的工作流文件")
        return 0

    total = sum(p.stat().st_size for p in orphans)
    print(f"未被任何模板引用 {len(orphans)} 个，共 {total / 1024:.1f} KB：")
    for p in orphans:
        print(f"  {p.name}  {p.stat().st_size / 1024:.1f} KB")

    if not args.apply:
        print("\n这是 dry-run。确认无误后加 --apply 删除。")
        return 0

    for p in orphans:
        try:
            p.unlink()
            print(f"  已删除 {p.name}")
        except OSError as e:
            print(f"  删除失败 {p.name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
