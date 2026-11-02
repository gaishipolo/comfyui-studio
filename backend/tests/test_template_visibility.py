"""M8 回归：停用模板只对管理员可见。"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.templates import get_template, list_templates  # noqa: E402
from app.models.models import Base, Template  # noqa: E402


async def test_inactive_templates_are_admin_only() -> None:
    with tempfile.TemporaryDirectory(prefix="template-visibility-") as raw_dir:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(Path(raw_dir) / 'test.db').as_posix()}"
        )
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                db.add_all([
                    Template(id="active", name="Active", is_active=True),
                    Template(id="inactive", name="Inactive", is_active=False),
                ])
                await db.commit()
                member = SimpleNamespace(id="member", role="member")
                admin = SimpleNamespace(id="admin", role="admin")

                try:
                    await list_templates(include_inactive=True, user=member, db=db)
                except HTTPException as exc:
                    assert exc.status_code == 403, exc.status_code
                else:
                    raise AssertionError("member listed inactive templates")

                visible = await list_templates(include_inactive=False, user=member, db=db)
                assert [template.id for template in visible] == ["active"]

                try:
                    await get_template("inactive", user=member, db=db)
                except HTTPException as exc:
                    assert exc.status_code == 404, exc.status_code
                else:
                    raise AssertionError("member read inactive template by id")

                assert (await get_template("inactive", user=admin, db=db)).id == "inactive"
        finally:
            await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(test_inactive_templates_are_admin_only())
    except BaseException as exc:
        print(f"[FAIL] test_inactive_templates_are_admin_only: {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 1 failed")
        raise SystemExit(1)
    print("[PASS] test_inactive_templates_are_admin_only")
    print("\nRESULT: 1 passed / 0 failed")
