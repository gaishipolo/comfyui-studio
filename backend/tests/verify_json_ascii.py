"""验证自定义 JSON 类型（ensure_ascii=False）可让中文 LIKE 命中"""
import asyncio, sys, json, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import String, Text, select, text
from sqlalchemy.types import TypeDecorator, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


def clean(s: str) -> str:
    return s.replace("✅", "[OK]").replace("❌", "[FAIL]").replace("⚠️", "[WARN]")


class JSONStore(TypeDecorator):
    """JSON 列，非 ASCII 字符原样存储，便于 SQL LIKE 搜索。

    impl 必须用 Text 而非 JSON —— impl=JSON 会再序列化一次造成双重编码。
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "t_ascii"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    meta: Mapped[dict] = mapped_column(JSONStore, default=dict)
    tags: Mapped[list] = mapped_column(JSONStore, default=list)


async def main():
    DB = Path(__file__).resolve().parents[1] / "test_ascii.db"
    if DB.exists():
        DB.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    U = uuid.uuid4().hex[:8]
    async with Session() as db:
        db.add(Row(id=U, meta={"prompt": f"{U}酷女孩提示词"},
                   tags=[f"{U}_smoke_标签"]))
        await db.commit()

        # 1. 原始存储值
        r = await db.execute(text("SELECT meta, tags FROM t_ascii WHERE id=:i"), {"i": U})
        m, t = r.one()
        print(f"1. 原始 meta = {m!r}")
        print(f"   原始 tags = {t!r}")
        ok_ascii = "\\u" not in m and "\\u" not in t
        print(f"   {clean('✅' if ok_ascii else '❌')} 无 \\uXXXX 转义")

        # 2. 中文 LIKE 命中 meta
        r = await db.execute(text(
            "SELECT COUNT(*) FROM t_ascii WHERE lower(CAST(meta AS VARCHAR)) "
            "LIKE lower(:p) ESCAPE '\\'"
        ), {"p": f"%{U}%酷女孩%"})
        n = r.scalar_one()
        print(f"2. 中文 LIKE 命中 meta -> {n}  期望 1  {clean('✅' if n == 1 else '❌')}")

        # 3. 中文 LIKE 命中 tags（对照：旧写法）
        r = await db.execute(text(
            "SELECT COUNT(*) FROM t_ascii WHERE lower(CAST(tags AS VARCHAR)) "
            "LIKE lower(:p) ESCAPE '\\'"
        ), {"p": f'%"{U}_smoke_标签"%'})
        n = r.scalar_one()
        print(f"3. 中文 LIKE 命中 tags -> {n}  期望 1  {clean('✅' if n == 1 else '❌')}")

        # 4. ORM 读回为原生 Python 类型
        r = await db.execute(select(Row).where(Row.id == U))
        row = r.scalar_one()
        ok_type = isinstance(row.meta, dict) and isinstance(row.tags, list)
        ok_val = row.meta.get("prompt") == f"{U}酷女孩提示词"
        print(f"4. ORM 读回 meta={row.meta!r} tags={row.tags!r}")
        print(f"   {clean('✅' if (ok_type and ok_val) else '❌')} 类型与取值正确")

        # 5. json_each 仍然可用
        r = await db.execute(text(
            "SELECT COUNT(*) FROM t_ascii a WHERE EXISTS ("
            "SELECT 1 FROM json_each(a.tags) je WHERE je.value = :t)"
        ), {"t": f"{U}_smoke_标签"})
        n = r.scalar_one()
        print(f"5. json_each 精确匹配 -> {n}  期望 1  {clean('✅' if n == 1 else '❌')}")

    await engine.dispose()
    DB.unlink()
    print("\n结论: ensure_ascii=False + json_each 双管齐下")


asyncio.run(main())
