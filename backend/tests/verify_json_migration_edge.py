r"""迁移边界用例：用户文本里含字面 \u / 反斜杠 / emoji / 嵌套结构，验证迁移不改坏数据"""
import asyncio, json, sys, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import _unescape_json_columns


def clean(s: str) -> str:
    return s.replace("✅", "[OK]").replace("❌", "[FAIL]").replace("⚠️", "[WARN]")


# 关键边界：用户真实输入里可能出现的"看起来像转义"的内容
EDGE_VALUES = {
    "literal_backslash_u": r"路径是 C:\users\xx",        # 单反斜杠 + u
    "literal_uXXXX": r"字面文本 \u1234 不是转义",          # 用户手打的 \u1234
    "double_backslash": r"正则 \\d+ 和 \\\\w",
    "emoji": "🎉 表情 cool 🚀",
    "chinese_mix": "中文English混合提示词",
    "nested": {"a": ["x", {"b": "字面\\u0041"}], "c": {"d": "深度"}},
    "json_string_in_json": '{"这是": "内嵌JSON字符串"}',
    "empty_str": "",
    "null_val": None,
}


async def main():
    DB = Path(__file__).resolve().parents[1] / "test_edge.db"
    if DB.exists():
        DB.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE assets (id VARCHAR(32) PRIMARY KEY, tags TEXT, meta TEXT)"))

    U = uuid.uuid4().hex[:8]
    ids = {}

    async with engine.begin() as conn:
        for key, val in EDGE_VALUES.items():
            rid = f"{U}_{key}"
            ids[key] = rid
            payload = {"payload": val, "note": f"{U}_{key}"}
            # 用默认 ensure_ascii=True 存（模拟老库最坏情况：全转义）
            raw = json.dumps(payload, ensure_ascii=True)
            await conn.execute(text(
                "INSERT INTO assets (id, tags, meta) VALUES (:i, :t, :m)"),
                {"i": rid, "t": json.dumps([key], ensure_ascii=True), "m": raw})

    # 记录迁移前的逻辑值（json.loads 后的 Python 对象）
    before = {}
    async with engine.begin() as conn:
        for key, rid in ids.items():
            r = await conn.execute(text("SELECT meta FROM assets WHERE id=:i"), {"i": rid})
            before[key] = json.loads(r.scalar_one())

    # 跑两次迁移（验证幂等 + 边界）
    for round_no in (1, 2):
        async with engine.begin() as conn:
            await conn.run_sync(_unescape_json_columns)

        after, ok_all = {}, True
        async with engine.begin() as conn:
            for key, rid in ids.items():
                r = await conn.execute(text("SELECT meta FROM assets WHERE id=:i"), {"i": rid})
                raw = r.scalar_one()
                try:
                    after[key] = json.loads(raw)
                except Exception as e:
                    print(f"  {clean('❌')} 第{round_no}轮 {key}: JSON 解析失败 {e}")
                    ok_all = False
                    continue

                # 关键断言：迁移必须保持逻辑值完全不变
                same = (after[key] == before[key])
                if not same:
                    ok_all = False
                    print(f"  {clean('❌')} 第{round_no}轮 {key}: 内容被改动!")
                    print(f"        前: {before[key]!r}")
                    print(f"        后: {after[key]!r}")
                elif round_no == 1:
                    # 展示存储形态
                    is_ascii = "\\u" in raw
                    sample = EDGE_VALUES[key]
                    shown = repr(sample)[:42]
                    print(f"  {clean('✅')} {key:22} 存储含转义={is_ascii!s:5} 值={shown}")

        print(f"  -> 第{round_no}轮 {'全部无损' if ok_all else '存在损坏'}\n")

    # 专项：字面 \u1234 必须原样保留，不能被"解码"成 A
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT meta FROM assets WHERE id=:i"),
                               {"i": ids["literal_uXXXX"]})
        got = json.loads(r.scalar_one())["payload"]
    ok_literal = (got == r"字面文本 \u1234 不是转义")
    print(f"{clean('✅' if ok_literal else '❌')} 字面 \\u1234 未被误解码 -> {got!r}")

    await engine.dispose()
    DB.unlink()
    print(f"\n结论: {'迁移对边界数据安全' if ok_literal else '迁移会破坏边界数据 —— 必须修'}")
    return 0 if ok_literal else 1


sys.exit(asyncio.run(main()))
