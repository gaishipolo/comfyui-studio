"""启动前配置自检回归（app/preflight.py）。

覆盖三件事：
1. 生产模式把关键缺失升级为 error（拒绝启动），开发模式只警告
2. 存储/数据库目录不可写在**任何**模式下都是 error —— 那不是配置不理想，是必然会坏
3. 体检结果里必须带可操作的修复提示，否则运维看到 ERROR 也不知道改什么
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.preflight import collect_issues, format_issues, is_production  # noqa: E402


def _kwargs(root: Path, **overrides):
    """一份全部合规的配置，测试只覆盖关心的那一项。"""
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    db_dir = root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    base = dict(
        env="production",
        secret_key="s" * 40,
        cors_origins="https://studio.example.com",
        frontend_dist=dist,
        storage_dir=root / "storage",
        database_url=f"sqlite+aiosqlite:///{(db_dir / 'workbench.db').as_posix()}",
        max_concurrent=2,
        ffmpeg_available=True,
    )
    base.update(overrides)
    return base


def _levels(issues, key):
    return [item.level for item in issues if item.key == key]


def test_env_aliases():
    assert is_production("production")
    assert is_production("  PROD ")
    assert not is_production("development")
    assert not is_production("")


def test_clean_config_has_no_issues():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp)))
    assert issues == [], [f"{i.level}:{i.key}" for i in issues]


def test_production_rejects_empty_secret_key():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), secret_key=""))
    assert "error" in _levels(issues, "SECRET_KEY")


def test_development_downgrades_secret_key_to_warning():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), env="development", secret_key=""))
    assert _levels(issues, "SECRET_KEY") == ["warning"]


def test_short_secret_key_warns_in_both_modes():
    for env in ("production", "development"):
        with tempfile.TemporaryDirectory() as tmp:
            issues = collect_issues(**_kwargs(Path(tmp), env=env, secret_key="abc123"))
        assert _levels(issues, "SECRET_KEY") == ["warning"], env


def test_production_requires_frontend_build():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "no-dist"
        issues = collect_issues(**_kwargs(Path(tmp), frontend_dist=missing))
    assert "error" in _levels(issues, "FRONTEND_DIST")


def test_development_only_warns_about_missing_frontend():
    # 本地用 vite dev server 时没有 dist 是正常的
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "no-dist"
        issues = collect_issues(**_kwargs(Path(tmp), env="development", frontend_dist=missing))
    assert _levels(issues, "FRONTEND_DIST") == ["warning"]


def test_unwritable_storage_is_error_in_both_modes():
    # 用一个同名文件占住路径，mkdir 必然失败 —— 跨平台，不依赖 chmod
    for env in ("production", "development"):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "storage-is-a-file"
            blocker.write_text("x", encoding="utf-8")
            issues = collect_issues(**_kwargs(Path(tmp), env=env, storage_dir=blocker))
        assert _levels(issues, "STORAGE_DIR") == ["error"], env


def test_unwritable_database_dir_is_error():
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "db-is-a-file"
        blocker.write_text("x", encoding="utf-8")
        issues = collect_issues(
            **_kwargs(Path(tmp), database_url=f"sqlite+aiosqlite:///{blocker.as_posix()}/workbench.db")
        )
    assert _levels(issues, "DATABASE_URL") == ["error"]


def test_cors_wildcard_warns():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), cors_origins="*"))
    assert _levels(issues, "CORS_ORIGINS") == ["warning"]


def test_max_concurrent_floor():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), max_concurrent=0))
    assert "error" in _levels(issues, "MAX_CONCURRENT_TASKS")


def test_missing_ffmpeg_is_only_a_warning():
    # 视频封面退化，不影响生成与图片素材
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), ffmpeg_available=False))
    assert _levels(issues, "FFMPEG") == ["warning"]


def test_ffmpeg_unknown_skips_check():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), ffmpeg_available=None))
    assert _levels(issues, "FFMPEG") == []


def test_non_sqlite_database_skips_dir_check():
    # 用 PostgreSQL 之类时不该拿 SQLite 的目录规则去要求它
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), database_url="postgresql+asyncpg://h/db"))
    assert _levels(issues, "DATABASE_URL") == []


def test_format_includes_hint_and_counts():
    with tempfile.TemporaryDirectory() as tmp:
        issues = collect_issues(**_kwargs(Path(tmp), secret_key="", cors_origins="*"))
    text = format_issues(issues, env="production")
    assert "1 项必须修复" in text, text
    assert "1 项建议处理" in text, text
    assert "[ERROR] SECRET_KEY" in text
    assert "→" in text, "每条问题都要带修复提示"


def test_format_returns_empty_when_clean():
    assert format_issues([], env="production") == ""


def main() -> int:
    tests = [
        test_env_aliases,
        test_clean_config_has_no_issues,
        test_production_rejects_empty_secret_key,
        test_development_downgrades_secret_key_to_warning,
        test_short_secret_key_warns_in_both_modes,
        test_production_requires_frontend_build,
        test_development_only_warns_about_missing_frontend,
        test_unwritable_storage_is_error_in_both_modes,
        test_unwritable_database_dir_is_error,
        test_cors_wildcard_warns,
        test_max_concurrent_floor,
        test_missing_ffmpeg_is_only_a_warning,
        test_ffmpeg_unknown_skips_check,
        test_non_sqlite_database_skips_dir_check,
        test_format_includes_hint_and_counts,
        test_format_returns_empty_when_clean,
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
