"""验证标签落库 + 转义过滤（用真实 Asset 模型与真实 DB）"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, cast, String
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models.models import Asset
from app.api.assets import _escape_like


def clean(s: str) -> str:
    return s.replace("✅", "[OK]").replace("❌", "[FAIL]").replace("⚠️", "[WARN]")


async def main():
    print("=== 标签落库 + 过滤验证（真实模型）===")
    DB = Path(__file__).resolve().parents[1] / "workbench.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # 1. 写入含中文与下划线的标签
        a = Asset(task_id="persist-1", file_name="t.png", file_path="x/t.png",
                  mime_type="image/png", tags=["image", "smoke_标签"])
        db.add(a)
        await db.commit()
        await db.refresh(a)
        aid = a.id
        print(f"1. 写入资产 {aid}")

        # 2. 读回
        r = await db.execute(select(Asset).where(Asset.id == aid))
        got = r.scalar_one()
        print(f"2. 读回 tags = {got.tags!r}  类型={type(got.tags).__name__}")
        print(f"   {clean('✅' if got.tags == ['image', 'smoke_标签'] else '❌')} 标签落库")

        # 3. 原始 SQL 字符串
        r = await db.execute(select(cast(Asset.tags, String)).where(Asset.id == aid))
        raw = r.scalar_one()
        print(f"3. 原始 cast 字符串 = {raw!r}")

        # 4. 精确复现 assets.py 的过滤（含转义）
        tag = "smoke_标签"
        pat = f'%"{_escape_like(tag)}"%'
        r = await db.execute(select(Asset).where(
            Asset.tags.cast(type_=Asset.file_name.type).ilike(pat, escape="\\")
        ))
        matched = r.scalars().all()
        print(f"4. 转义过滤 pattern={pat!r} -> {len(matched)} 条")
        print(f"   {clean('✅' if matched else '❌')} 转义后中文+下划线标签命中")

        # 5. 下划线转义是否真的生效（% 是单字符通配）
        pat2 = f'%"{_escape_like("smoke")}%"'   # 不带下划线的前缀
        r = await db.execute(select(Asset).where(
            Asset.tags.cast(type_=Asset.file_name.type).ilike(pat2, escape="\\")
        ))
        n2 = len(r.scalars().all())
        print(f"5. 反向检查 pattern={pat2!r} -> {n2} 条（应为 0，否则转义未生效）")
        print(f"   {clean('✅' if n2 == 0 else '❌')} 转义生效")

        # 6. 清理
        await db.delete(got)
        await db.commit()
        print("6. 已清理测试资产")

    await engine.dispose()

    # 7. 同时确认 async_session 用的 DB 文件
    from app.database import async_session, engine as app_engine
    url = str(app_engine.url)
    print(f"\n7. app.database 引擎 URL = {url}")
    print(f"   {clean('✅' if 'workbench.db' in url else '❌')} 指向同一 DB")


asyncio.run(main())
