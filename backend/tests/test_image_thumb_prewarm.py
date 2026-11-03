"""图片缩略图「落盘即生成」回归。

素材库卡片走 `/api/assets/{id}/thumb`，图片缩略图原本只在**首次访问**时懒
生成：`/thumb` 最多等 3 秒，等不到就回退原图（均值 2MB），生成任务在后台
继续。单用户完全没问题，但生成侧是单并发排队 —— 50 个人同时翻素材库时，
排在第 3 位之后就全都超时回退，缩略图等于白做。

所以改成落盘时就把缩略图做出来，卡片永远直接命中。这里钉住：
1. 上传一张图片，会**额外**写一份 JPEG 缩略图（键是 `thumb_key(原键)`）
2. 缩略图比原图小（真的是缩略图，不是原样拷贝）
3. 非图片（文本）不生成缩略图，不要为无用的解码付代价
4. 缩略图生成失败不能连累上传本身（素材必须存下来）
"""
import asyncio
import io
import os
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models.models import User  # noqa: E402
from app.thumbs import thumb_key  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))


def make_png(width: int = 900, height: int = 700) -> bytes:
    """造一张**难压缩**的图。

    渐变/纯色图 PNG 能压到几 KB，而 400px JPEG 反而更大，那种样本无法
    验证「缩略图更小」。用随机像素，保证压缩率接近 1，原图真的有 MB 级。
    """
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_wav() -> bytes:
    """0.1 秒静音 wav —— 用来验证「非图片不生成缩略图」。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


async def scenario() -> None:
    from app.main import app as fastapi_app
    from app.api import assets as assets_api

    with tempfile.TemporaryDirectory(prefix="thumb-prewarm-") as raw_dir:
        root = Path(raw_dir)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def override_get_db():
            async with sessions() as session:
                yield session

        fastapi_app.dependency_overrides[get_db] = override_get_db

        # 拦掉真实写盘：只记录「写了哪些键」，不碰 storage 目录（保持测试干净）
        saved: list[tuple[str, int, str | None]] = []
        original_save = assets_api.save_asset_file

        def spy_save(key: str, data: bytes, content_type: str | None = None) -> None:
            saved.append((key, len(data), content_type))

        assets_api.save_asset_file = spy_save  # type: ignore[assignment]
        try:
            async with sessions() as db:
                db.add(
                    User(
                        id="u1",
                        username="uploader",
                        password_hash=hash_password("pass1234"),
                        display_name="上传者",
                        role="admin",  # 管理员豁免配额，免得 20GB 默认值干扰
                    )
                )
                await db.commit()

            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "uploader", "password": "pass1234"},
                )
                check("测试用户能登录", login.status_code == 200, login.text[:120])
                headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

                png = make_png()
                upload = await client.post(
                    "/api/assets/upload",
                    files={"files": ("big.png", png, "image/png")},
                    headers=headers,
                )
                check("上传图片成功", upload.status_code == 200,
                      f"{upload.status_code} {upload.text[:200]}")

                source_keys = [k for k, _, ctype in saved if ctype and ctype.startswith("image/png")]
                thumb_keys = [k for k, _, ctype in saved if ctype == "image/jpeg"]

                check("原图写了一份", len(source_keys) == 1, f"saved={saved}")
                check("缩略图也写了一笔", len(thumb_keys) == 1, f"saved={saved}")
                if source_keys and thumb_keys:
                    check(
                        "缩略图键由原键派生（thumb_key）",
                        thumb_keys[0] == thumb_key(source_keys[0]),
                        f"thumb={thumb_keys[0]} expect={thumb_key(source_keys[0])}",
                    )
                    source_size = next(size for key, size, _ in saved if key == source_keys[0])
                    thumb_size = next(size for key, size, _ in saved if key == thumb_keys[0])
                    check(
                        "缩略图明显小于原图",
                        thumb_size < source_size / 4,
                        f"原图={source_size}B 缩略图={thumb_size}B",
                    )
                    thumb_bytes = b""
                    # 从 spy 里拿不到字节，单独再算一次，顺便验证尺寸上限
                    from app.thumbs import make_image_thumb

                    thumb_bytes = make_image_thumb(png) or b""
                    if thumb_bytes:
                        with Image.open(io.BytesIO(thumb_bytes)) as thumb_img:
                            check(
                                "缩略图长边 ≤ 400",
                                max(thumb_img.size) <= 400,
                                f"size={thumb_img.size}",
                            )
                            check("缩略图是 JPEG", thumb_img.format == "JPEG", f"{thumb_img.format}")

                # 非图片：不该生成缩略图（上传接口只收图片/音频/视频，用音频测）
                saved.clear()
                audio_upload = await client.post(
                    "/api/assets/upload",
                    files={"files": ("beep.wav", make_wav(), "audio/wav")},
                    headers=headers,
                )
                check("上传音频成功", audio_upload.status_code == 200, audio_upload.text[:160])
                check(
                    "音频不生成缩略图（不做无用解码）",
                    len(saved) == 1 and saved[0][2] == "audio/wav",
                    f"saved={saved}",
                )

                # 缩略图生成抛异常时，上传本身仍要成功
                def boom(_data: bytes, max_edge: int = 400) -> bytes | None:
                    raise RuntimeError("decode exploded")

                original_thumb = assets_api.make_image_thumb
                assets_api.make_image_thumb = boom  # type: ignore[assignment]
                saved.clear()
                try:
                    broken = await client.post(
                        "/api/assets/upload",
                        files={"files": ("broken.png", make_png(300, 200), "image/png")},
                        headers=headers,
                    )
                finally:
                    assets_api.make_image_thumb = original_thumb  # type: ignore[assignment]
                check(
                    "缩略图生成失败不连累上传",
                    broken.status_code == 200,
                    f"{broken.status_code} {broken.text[:160]}",
                )
                check(
                    "失败时只写原图",
                    len(saved) == 1 and saved[0][2] == "image/png",
                    f"saved={saved}",
                )
        finally:
            assets_api.save_asset_file = original_save  # type: ignore[assignment]
            fastapi_app.dependency_overrides.pop(get_db, None)
            await engine.dispose()


async def test_task_output_gets_thumb() -> None:
    """生成任务落盘的图片同样要带缩略图 —— 素材里绝大多数是引擎产物。

    走完整 execute_task（假 provider 返回一张难压缩的 PNG），直接检查存储目录
    里有没有以 `thumb_key()` 命名的文件，而不是只看代码里有没有这行调用。
    """
    from app.config import settings
    from app.engines import factory
    from app.engines.base import GenerationProvider, GenerationResult, TaskStatus
    from app.models.models import Asset, Task, Template
    from app.services import task_service as ts

    class ImageProvider(GenerationProvider):
        name = "fake-image-engine"
        label = "假出图引擎"

        async def submit(self, ctx, params):
            return "p1"

        async def get_status(self, task_id):
            return GenerationResult(
                status=TaskStatus.DONE,
                outputs=[{"filename": "out.png", "subfolder": "", "type": "output"}],
            )

        async def download_output(self, filename, subfolder="", output_type="output"):
            return make_png(800, 600)

        async def health_check(self):
            return True

    factory.register_provider("fake-image-engine", ImageProvider)
    original_storage = settings.STORAGE_DIR
    original_session = ts.async_session
    with tempfile.TemporaryDirectory(prefix="thumb-task-") as raw_dir:
        root = Path(raw_dir)
        settings.STORAGE_DIR = root / "storage"
        engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'task.db').as_posix()}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        ts.async_session = sessions  # type: ignore[assignment]
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                db.add(
                    Template(
                        id="tpl-img",
                        name="出图模板",
                        engine="fake-image-engine",
                        workflow_file="",
                        category="image",
                        input_schema={"fields": []},
                        param_mapping={},
                        output_config={},
                        is_active=True,
                    )
                )
                db.add(
                    Task(
                        id="task-img",
                        template_id="tpl-img",
                        status="pending",
                        params={},
                        created_by="",
                    )
                )
                await db.commit()

            await ts.TaskService.execute_task("task-img")

            async with sessions() as db:
                assets = (await db.execute(Asset.__table__.select())).fetchall()
            check("任务跑完落了一条素材", len(assets) == 1, f"assets={len(assets)}")
            if assets:
                # Row 的字段顺序按表定义，用 _mapping 按列名取更稳
                record = assets[0]._mapping
                rel_path = record["file_path"]
                thumb_path = thumb_key(rel_path)
                source_file = settings.STORAGE_DIR / rel_path
                thumb_file = settings.STORAGE_DIR / thumb_path
                check("原图落盘", source_file.exists(), f"{source_file}")
                check("缩略图随产物一起落盘", thumb_file.exists(), f"{thumb_file}")
                if thumb_file.exists():
                    check(
                        "缩略图比原图小",
                        thumb_file.stat().st_size < source_file.stat().st_size / 4,
                        f"原图={source_file.stat().st_size}B 缩略图={thumb_file.stat().st_size}B",
                    )
            await engine.dispose()
        finally:
            ts.async_session = original_session  # type: ignore[assignment]
            settings.STORAGE_DIR = original_storage
            factory.unregister_provider("fake-image-engine")


async def main() -> int:
    await scenario()
    await test_task_output_gets_thumb()
    failed = [r for r in _results if not r[1]]
    print(f"\nRESULT: {len(_results) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
