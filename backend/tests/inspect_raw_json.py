r"""直接查真实库 JSON 列的原始存储值，确认是否仍有 \uXXXX 转义"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from app.database import engine


async def main():
    print("=== 真实库 JSON 列原始值抽查 ===\n")

    async with engine.begin() as conn:
        for table, col in [("assets", "tags"), ("assets", "meta"),
                           ("tasks", "params"), ("tasks", "result"),
                           ("templates", "input_schema"),
                           ("templates", "param_mapping"),
                           ("templates", "output_config")]:
            r = await conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
            total = r.scalar_one()

            r = await conn.execute(text(
                f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'))
            raws = [row[0] for row in r.fetchall()]

            # 统计真正含 \uXXXX 转义序列的行（用 Python 检测，避开 SQL 转义地狱）
            escaped = [x for x in raws
                       if isinstance(x, str) and any(
                           f"\\u{ord(c):04x}" in x for c in x
                           if ord(c) > 127)]

            # 有非 ASCII 内容的行
            non_ascii = [x for x in raws
                         if isinstance(x, str) and any(ord(c) > 127 for c in x)]

            print(f"{table}.{col}: 共 {total} 行 / 非空 {len(raws)} 行")
            print(f"   含非 ASCII 内容: {len(non_ascii)} 行")
            print(f"   仍以 \\uXXXX 转义存储: {len(escaped)} 行")

            for x in non_ascii[:3]:
                print(f"   样本: {x[:110]!r}")
            print()

    # 端到端：模拟 API 标签筛选
    print("=== 端到端标签筛选（真实库）===")
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT DISTINCT je.value FROM assets a, json_each(a.tags) je "
            "ORDER BY je.value"))
        all_tags = [row[0] for row in r.fetchall()]
        print(f"库中全部标签: {all_tags}")

        if all_tags:
            t = all_tags[0]
            r = await conn.execute(text(
                "SELECT COUNT(*) FROM assets a WHERE EXISTS ("
                "SELECT 1 FROM json_each(a.tags) je WHERE lower(je.value) = lower(:t))"),
                {"t": t})
            print(f"按标签 {t!r} 筛选 -> {r.scalar_one()} 条")


asyncio.run(main())
