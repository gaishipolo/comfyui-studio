r"""验证 JSON 转义数据迁移：老库 \uXXXX 被改写为真实字符，且幂等"""
import asyncio, json, sys, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


def clean(s: str) -> str:
    return s.replace("✅", "[OK]").replace("❌", "[FAIL]").replace("⚠️", "[WARN]")


async def main():
    DB = Path(__file__).resolve().parents[1] / "test_migrate.db"
    if DB.exists():
        DB.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE assets (id VARCHAR(32) PRIMARY KEY, tags TEXT, meta TEXT)"))
        await conn.execute(text(
            "CREATE TABLE tasks (id VARCHAR(32) PRIMARY KEY, params TEXT, result TEXT)"))

    U = uuid.uuid4().hex[:8]
    # 模拟老数据：ensure_ascii=True 的转义形式
    old_tags = json.dumps(["image", f"{U}_smoke_标签"], ensure_ascii=True)
    old_meta = json.dumps({"prompt": f"{U}酷女孩提示词"}, ensure_ascii=True)
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO assets (id, tags, meta) VALUES (:i, :t, :m)"),
            {"i": U, "t": old_tags, "m": old_meta})
    print(f"1. 写入老格式 tags={old_tags!r}")
    print(f"   写入老格式 meta={old_meta!r}")

    # 跑迁移
    from app.database import _unescape_json_columns

    async with engine.begin() as conn:
        await conn.run_sync(_unescape_json_columns)
    print("2. 已执行迁移")

    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT tags, meta FROM assets WHERE id=:i"), {"i": U})
        new_tags, new_meta = r.one()
    print(f"3. 迁移后 tags={new_tags!r}")
    print(f"   迁移后 meta={new_meta!r}")

    ok1 = "\\u" not in new_tags and "\\u" not in new_meta
    ok2 = json.loads(new_tags) == ["image", f"{U}_smoke_标签"]
    ok3 = json.loads(new_meta)["prompt"] == f"{U}酷女孩提示词"
    print(f"   {clean('✅' if ok1 else '❌')} 无转义残留")
    print(f"   {clean('✅' if ok2 else '❌')} tags 内容无损")
    print(f"   {clean('✅' if ok3 else '❌')} meta 内容无损")

    # 中文 LIKE 现在应命中
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT COUNT(*) FROM assets WHERE lower(CAST(meta AS VARCHAR)) "
            "LIKE lower(:p) ESCAPE '\\'"), {"p": f"%{U}%酷女孩%"})
        n_meta = r.scalar_one()
        r = await conn.execute(text(
            "SELECT COUNT(*) FROM assets WHERE EXISTS ("
            "SELECT 1 FROM json_each(assets.tags) je WHERE je.value = :t)"),
            {"t": f"{U}_smoke_标签"})
        n_tag = r.scalar_one()
    ok4 = (n_meta == 1)
    ok5 = (n_tag == 1)
    print(f"4. 中文 LIKE 命中 meta -> {n_meta}  期望 1  {clean('✅' if ok4 else '❌')}")
    print(f"5. json_each 命中 tags -> {n_tag}  期望 1  {clean('✅' if ok5 else '❌')}")

    # 幂等性：再跑一次不应报错、不应改动
    async with engine.begin() as conn:
        await conn.run_sync(_unescape_json_columns)
        r = await conn.execute(text("SELECT tags FROM assets WHERE id=:i"), {"i": U})
        after = r.scalar_one()
    ok6 = (after == new_tags)
    print(f"6. 幂等性 -> {'无变化' if ok6 else '被改动!'}  {clean('✅' if ok6 else '❌')}")

    # 脏数据不误伤
    bad_id = "badrow"
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO assets (id, tags, meta) VALUES (:i, :t, :m)"),
            {"i": bad_id, "t": "not json at all", "m": None})
        await conn.run_sync(_unescape_json_columns)
        r = await conn.execute(text("SELECT tags FROM assets WHERE id=:i"), {"i": bad_id})
        still = r.scalar_one()
    ok7 = (still == "not json at all")
    print(f"7. 脏数据保留 -> {still!r}  {clean('✅' if ok7 else '❌')}")

    await engine.dispose()
    DB.unlink()

    all_ok = all([ok1, ok2, ok3, ok4, ok5, ok6, ok7])
    print(f"\n结果: {clean('✅ 全部通过' if all_ok else '❌ 存在失败')}")
    return 0 if all_ok else 1


sys.exit(asyncio.run(main()))
