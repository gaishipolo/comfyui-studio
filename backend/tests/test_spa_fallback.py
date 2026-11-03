"""M5 回归：残缺前端构建应返回友好 503，而不是 FileResponse 运行期 500。"""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main as app_main  # noqa: E402


async def test_existing_dist_without_index_is_503() -> None:
    with tempfile.TemporaryDirectory(prefix="empty-dist-") as raw_dir:
        with patch.object(app_main, "_DIST", Path(raw_dir)):
            try:
                await app_main.spa("")
            except HTTPException as exc:
                assert exc.status_code == 503, exc.status_code
                assert "index.html" in str(exc.detail)
            else:
                raise AssertionError("empty dist returned FileResponse instead of 503")


async def test_dist_path_is_absolute() -> None:
    # 相对路径会让 spa() 里的 is_relative_to 恒为 False，静态资源全部回落成
    # index.html —— 浏览器按严格 MIME 拒载 JS，页面白屏
    assert app_main._DIST.is_absolute(), app_main._DIST


async def test_static_asset_is_served_before_index_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="dist-") as raw_dir:
        root = Path(raw_dir)
        (root / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (root / "assets").mkdir()
        script = root / "assets" / "app.js"
        script.write_text("console.log(1)", encoding="utf-8")
        with patch.object(app_main, "_DIST", root.resolve()):
            resp = await app_main.spa("assets/app.js")
        assert Path(getattr(resp, "path", "")).resolve() == script.resolve(), resp


async def test_wildcard_cors_never_enables_credentials() -> None:
    origins, credentials = app_main._cors_options("*")
    assert origins == ["*"]
    assert credentials is False

    origins, credentials = app_main._cors_options(
        "http://localhost:5173, http://127.0.0.1:5173"
    )
    assert origins == ["http://localhost:5173", "http://127.0.0.1:5173"]
    assert credentials is True


async def main() -> int:
    try:
        await test_existing_dist_without_index_is_503()
        await test_dist_path_is_absolute()
        await test_static_asset_is_served_before_index_fallback()
        await test_wildcard_cors_never_enables_credentials()
    except BaseException as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 4 failed")
        return 1
    print("[PASS] test_existing_dist_without_index_is_503")
    print("[PASS] test_dist_path_is_absolute")
    print("[PASS] test_static_asset_is_served_before_index_fallback")
    print("[PASS] test_wildcard_cors_never_enables_credentials")
    print("\nRESULT: 4 passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
