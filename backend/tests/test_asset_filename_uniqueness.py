"""M7/M9 回归：同任务素材文件名必须唯一，历史重复查询仍应容错。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.asset_names import unique_asset_name  # noqa: E402
from app.models.models import Asset  # noqa: E402


def test_asset_model_has_no_task_filename_unique_index() -> None:
    """`(task_id, file_name)` 唯一索引必须**不存在**。

    素材独立化后 `task_id` 可为空（用户上传没有来源任务），而 SQLite 认为
    NULL 互不相等 —— 那个唯一索引对上传素材完全失效，留着只会造成「生成素材
    受唯一约束、上传素材不受」的假安全感。去重改由 `unique_asset_name()`
    在应用层做（上传按 owner_id 查重）。
    """
    unique_columns = [
        {column.name for column in index.columns}
        for index in Asset.__table__.indexes
        if index.unique
    ]
    assert {"task_id", "file_name"} not in unique_columns, unique_columns

    # 归属查询的主力索引还在：素材库按 owner + 时间倒序翻页
    plain_columns = [
        tuple(column.name for column in index.columns)
        for index in Asset.__table__.indexes
        if not index.unique
    ]
    assert ("owner_id", "created_at") in plain_columns, plain_columns


def test_duplicate_names_receive_stable_suffix() -> None:
    used = {"clip.png", "clip_2.png"}
    assert unique_asset_name("clip.png", used) == "clip_3.png"
    assert unique_asset_name("../clip.png", set()) == "clip.png"


def test_generated_name_respects_column_width() -> None:
    long_name = f"{'x' * 300}.png"
    first = unique_asset_name(long_name, set())
    second = unique_asset_name(long_name, {first})
    assert len(first) <= 256, len(first)
    assert len(second) <= 256, len(second)
    assert first.endswith(".png")
    assert second.endswith("_2.png")


def main() -> int:
    tests = [
        test_asset_model_has_no_task_filename_unique_index,
        test_duplicate_names_receive_stable_suffix,
        test_generated_name_respects_column_width,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
