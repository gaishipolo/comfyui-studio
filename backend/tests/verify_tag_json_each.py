"""验证 json_each 标签过滤能正确解码 \\uXXXX 转义的 JSON 列"""
import asyncio, sys, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models.models import Asset


def clean(s: str) -> str:
    return s.replace("✅", "[OK]").replace("❌", "[FAIL]").replace("⚠️", "[WARN]")


async def main():
    print("=== json_each 标签过滤验证 ===")
    DB = Path(__file__).resolve().parents[1] / "workbench.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # 用唯一前缀避免与库里历史数据撞车
    U = f"z{uuid.uuid4().hex[:8]}"
    tags = [f"{U}_image", f"{U}_smoke_标签", f"{U}_a%b", f"{U}_c_d", f'{U}_he"llo']

    async with Session() as db:
        a = Asset(task_id="je-1", file_name="t.png", file_path="x/t.png",
                  mime_type="image/png", tags=tags)
        db.add(a)
        await db.commit()
        await db.refresh(a)
        aid = a.id
        print(f"1. 写入 {aid}")
        print(f"   tags={tags!r}")

        async def count(tag: str, ci: bool = False) -> int:
            if ci:
                sql = """
                    SELECT COUNT(*) FROM assets a WHERE EXISTS (
                        SELECT 1 FROM json_each(a.tags) je
                        WHERE lower(je.value) = lower(:t))
                """
            else:
                sql = """
                    SELECT COUNT(*) FROM assets a WHERE EXISTS (
                        SELECT 1 FROM json_each(a.tags) je WHERE je.value = :t)
                """
            r = await db.execute(text(sql), {"t": tag})
            return r.scalar_one()

        ok_all = True
        for tag, expect in [
            (f"{U}_smoke_标签", 1), (f"{U}_image", 1), (f"{U}_a%b", 1),
            (f"{U}_c_d", 1), (f'{U}_he"llo', 1),
            (f"{U}_smoke", 0),        # 部分匹配不应命中
            ("标签", 0),              # 部分匹配不应命中
            (f"{U}_IMAGE", 0),        # 默认大小写敏感
        ]:
            n = await count(tag)
            ok = (n == expect)
            ok_all &= ok
            print(f"2. exact({tag!r}) -> {n}  期望 {expect}  {clean('✅' if ok else '❌')}")

        n = await count(f"{U}_IMAGE", ci=True)
        ok = (n == 1)
        ok_all &= ok
        print(f"3. ci({U}_IMAGE) -> {n}  期望 1  {clean('✅' if ok else '❌')}")

        # 旧 LIKE 方案对照（应为 0，证实根因）
        r = await db.execute(text("""
            SELECT COUNT(*) FROM assets
            WHERE lower(CAST(tags AS VARCHAR)) LIKE lower('%"smoke_标签"%') ESCAPE '\\'
        """))
        n = r.scalar_one()
        print(f"4. 旧 LIKE 方案 -> {n} 条（预期 0，证实根因）  {clean('✅' if n == 0 else '❌')}")

        # ORM 组合查询（复现 API 层写法）
        r = await db.execute(
            select(Asset.id).where(
                text("EXISTS (SELECT 1 FROM json_each(assets.tags) je WHERE je.value = :t)")
            ), {"t": f"{U}_smoke_标签"}
        )
        ids = r.scalars().all()
        ok = (len(ids) == 1)
        ok_all &= ok
        print(f"5. ORM 组合查询 -> {len(ids)} 条  期望 1  {clean('✅' if ok else '❌')}")

        await db.delete(a)
        await db.commit()
        print(f"6. 已清理\n\n结果: {clean('✅ 全部通过' if ok_all else '❌ 存在失败')}")

    await engine.dispose()
    return 0 if ok_all else 1


sys.exit(asyncio.run(main()))
