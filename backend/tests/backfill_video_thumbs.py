"""维护脚本：给存量视频素材补缩略图

新落盘的视频会自动抽帧（task_service._save_asset），但历史素材没有缩略图 ——
素材库卡片仍会去拉整个 mp4 当封面。这个脚本扫一遍视频素材，缺的补上。

默认 dry-run，加 --apply 才写存储。只写缩略图文件，不动数据库、不动原视频。

用法：
  python tests/backfill_video_thumbs.py            # 看看会补哪些
  python tests/backfill_video_thumbs.py --apply
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.filestore import open_asset, read_asset_bytes, save_asset_file  # noqa: E402
from app.thumbs import ffmpeg_exe, make_video_thumb, thumb_key  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "workbench.db"
APPLY = "--apply" in sys.argv


def main() -> int:
    exe = ffmpeg_exe()
    if not exe:
        print("没找到 ffmpeg：装 imageio-ffmpeg（pip install imageio-ffmpeg）或设 FFMPEG_PATH")
        return 1
    print(f"ffmpeg: {exe}")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "select task_id, file_name, file_path, file_size from assets "
        "where mime_type like 'video/%' order by created_at"
    ).fetchall()
    print(f"视频素材 {len(rows)} 个")

    done = skipped = failed = 0
    for r in rows:
        key = thumb_key(r["task_id"], r["file_name"])
        if open_asset(key) is not None:
            skipped += 1
            continue
        data = read_asset_bytes(r["file_path"])
        if not data:
            print(f"  [跳过] 原文件读不到：{r['file_path']}")
            failed += 1
            continue
        thumb = make_video_thumb(data, Path(r["file_name"]).suffix)
        if not thumb:
            print(f"  [失败] 抽帧为空：{r['file_name']}")
            failed += 1
            continue
        print(f"  [待补] {r['file_name'][:40]:42s} {r['file_size'] / 1024:8.0f}KB → {len(thumb) / 1024:5.0f}KB")
        done += 1
        if APPLY:
            save_asset_file(key, thumb, content_type="image/jpeg")

    print(f"\n有缩略图跳过 {skipped}，{'已补' if APPLY else '待补'} {done}，失败 {failed}")
    if not APPLY and done:
        print("确认无误后加 --apply 执行")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
