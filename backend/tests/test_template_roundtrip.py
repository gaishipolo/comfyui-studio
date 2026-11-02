"""T2 回归：模板导入/导出往返（离线，不起服务）。

覆盖：导出→导入还原、重名另存、overwrite 保留 id、缺引擎只告警、
版本过新拒绝、格式不符拒绝、非法内容 400。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import templates as templates_api  # noqa: E402
from app.api.templates import (  # noqa: E402
    TEMPLATE_EXPORT_FORMAT,
    TEMPLATE_EXPORT_VERSION,
    export_template,
    import_template,
)
from app.config import settings  # noqa: E402
from app.models.models import Base, Template  # noqa: E402
from app.schemas.schemas import TemplateImportPayload  # noqa: E402


WORKFLOW = {
    "1": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "placeholder"},
        "_meta": {"title": "Prompt"},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0]},
        "_meta": {"title": "Save"},
    },
}

ADMIN = SimpleNamespace(id="admin", role="admin")


def make_template(session, **overrides) -> Template:
    template = Template(
        id=overrides.pop("id", "tpl-1"),
        name=overrides.pop("name", "往返模板"),
        description="描述",
        category="image",
        engine="comfyui",
        workflow_file="tpl-1.json",
        param_mapping={"prompt": "1.inputs.text"},
        output_config={"nodes": ["9"]},
        input_schema={"fields": [{"name": "prompt", "label": "提示词", "type": "text"}]},
        **overrides,
    )
    session.add(template)
    return template


async def scenario() -> None:
    original_workflow_dir = settings.WORKFLOW_DIR
    with tempfile.TemporaryDirectory(prefix="tpl-roundtrip-") as raw_dir:
        root = Path(raw_dir)
        settings.WORKFLOW_DIR = root / "workflows"
        settings.WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
        (settings.WORKFLOW_DIR / "tpl-1.json").write_text(
            json.dumps(WORKFLOW, ensure_ascii=False), encoding="utf-8"
        )
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(root / 'test.db').as_posix()}"
        )
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                template = make_template(db)
                await db.commit()

                # ── 导出 ──
                response = await export_template("tpl-1", user=ADMIN, db=db)
                doc = json.loads(response.body.decode("utf-8"))
                assert doc["format"] == TEMPLATE_EXPORT_FORMAT, doc["format"]
                assert doc["version"] == TEMPLATE_EXPORT_VERSION
                assert doc["workflow"] == WORKFLOW, doc["workflow"]
                assert doc["template"]["param_mapping"] == {"prompt": "1.inputs.text"}
                assert "filename*=UTF-8''" in response.headers["content-disposition"]

                # ── 模拟导出到另一台机器：先删掉源模板 ──
                await db.delete(template)
                await db.commit()

                # ── 导入到空库 ──
                payload = TemplateImportPayload(**doc)
                imported = await import_template(
                    payload, overwrite=False, user=ADMIN, db=db
                )
                assert imported.template.name == "往返模板", imported.template.name
                assert imported.template.workflow_file.endswith(".json")
                assert (settings.WORKFLOW_DIR / imported.template.workflow_file).exists()
                imported_id = imported.template.id

                # ── 再导入一次：重名 → 另存，且工作流内容去重复用 ──
                again = await import_template(
                    TemplateImportPayload(**doc), overwrite=False, user=ADMIN, db=db
                )
                assert again.template.name != "往返模板", again.template.name
                assert again.template.name.startswith("往返模板"), again.template.name
                assert again.template.workflow_file == imported.template.workflow_file, (
                    "同内容工作流必须复用同一个文件"
                )
                assert any("同名" in w for w in again.warnings), again.warnings

                # ── overwrite：保留原 id（历史任务不断链）──
                overwritten = await import_template(
                    TemplateImportPayload(**doc), overwrite=True, user=ADMIN, db=db
                )
                assert overwritten.template.id == imported_id, (
                    overwritten.template.id, imported_id
                )
                assert any("覆盖" in w for w in overwritten.warnings), overwritten.warnings

                # ── 缺引擎：只告警不拒绝 ──
                missing_engine_doc = json.loads(json.dumps(doc))
                missing_engine_doc["template"]["engine"] = "not_installed_engine"
                missing = await import_template(
                    TemplateImportPayload(**missing_engine_doc),
                    overwrite=False,
                    user=ADMIN,
                    db=db,
                )
                assert any("not_installed_engine" in w for w in missing.warnings), (
                    missing.warnings
                )
                assert missing.template.engine == "not_installed_engine"

                # ── 版本过新：拒绝 ──
                newer = json.loads(json.dumps(doc))
                newer["version"] = TEMPLATE_EXPORT_VERSION + 1
                try:
                    await import_template(
                        TemplateImportPayload(**newer), overwrite=False, user=ADMIN, db=db
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400, exc.status_code
                else:
                    raise AssertionError("版本过新的导出文件应被拒绝")

                # ── 格式不符：拒绝 ──
                foreign = json.loads(json.dumps(doc))
                foreign["format"] = "other-system/template"
                try:
                    await import_template(
                        TemplateImportPayload(**foreign), overwrite=False, user=ADMIN, db=db
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400, exc.status_code
                else:
                    raise AssertionError("非本系统导出的文件应被拒绝")

                # ── 模板内容非法：400 ──
                broken = json.loads(json.dumps(doc))
                broken["template"]["input_schema"] = "not-a-dict"
                try:
                    await import_template(
                        TemplateImportPayload(**broken), overwrite=False, user=ADMIN, db=db
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400, exc.status_code
                else:
                    raise AssertionError("非法模板内容应被拒绝")
        finally:
            await engine.dispose()
            settings.WORKFLOW_DIR = original_workflow_dir


def main() -> int:
    try:
        asyncio.run(scenario())
    except BaseException as exc:
        print(f"[FAIL] test_template_export_import_roundtrip: {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 1 failed")
        return 1
    print("[PASS] test_template_export_import_roundtrip")
    print("\nRESULT: 1 passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
