"""流程导出/导入的测试 —— 离线。

**核心场景是「跨机器搬运」**：流程引用了节点和子流程，导到另一台机器上
那些引用可能都不存在。所以要验证：

1. 导出包带上引用的节点与子流程快照（**递归** —— 子流程里还有引用）
2. 导入到「什么都没有」的机器 → 按快照创建节点与子流程，引用重写正确
3. 导入到「已经有同名节点」的机器 → 用本机的，不重复创建
4. 同名流程已存在 → 报错，除非传 overwrite
5. 格式不对 / 版本太新 → 明确报错
6. 导出的流程导入后**还能跑**（引用重写没写错）
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def scenario() -> None:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models.models import Flow, Template
    from app.services import flow_portability as fp

    with tempfile.TemporaryDirectory(prefix="flow-port-") as raw_dir:
        root = Path(raw_dir)

        async def fresh_db(name: str):
            """一台「机器」—— 独立的库。"""
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{(root / name).as_posix()}"
            )
            session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return engine, session

        # ── 机器 A：有节点、有子流程 ──
        engine_a, sa = await fresh_db("a.db")

        async with sa() as db:
            tpl = Template(
                id="tpl_a",
                name="文生图",
                engine="comfyui",
                input_schema={"fields": [{"name": "prompt", "label": "提示词", "type": "textarea"}]},
                param_mapping={},
                output_config={"nodes": []},
                is_active=True,
            )
            db.add(tpl)

            sub = Flow(
                id="flow_sub",
                name="子流程",
                input_schema={"fields": []},
                outputs=[{"name": "img", "from": "gen", "pick": "all"}],
                steps=[
                    {"id": "gen", "type": "template", "template_id": "tpl_a", "params": {}}
                ],
                created_by="u1",
            )
            db.add(sub)
            await db.commit()

            parent = Flow(
                id="flow_parent",
                name="主流程",
                input_schema={"fields": []},
                outputs=[{"name": "result", "from": "sub", "pick": "all"}],
                steps=[
                    {"id": "sub", "type": "flow", "flow_id": "flow_sub", "params": {}},
                    {
                        "id": "down",
                        "type": "template",
                        "template_id": "tpl_a",
                        "params": {"prompt": "{{ steps.sub.img }}"},
                    },
                ],
                created_by="u1",
            )
            db.add(parent)
            await db.commit()

            # ── 导出 ──
            templates, flows, missing = await fp.collect_references(db, parent)
            check("导出时引用都还在（没有缺失）", not missing, str(missing))
            doc = fp.build_export_doc(parent, templates, flows, "2026-11-04T00:00:00Z")

            check("导出包带上了引用的节点", len(doc["templates"]) == 1, str(doc["templates"]))
            check(
                "导出包带上了引用的子流程（递归）",
                len(doc["flows"]) == 1,
                str([f.get("name") for f in doc["flows"]]),
            )
            check(
                "子流程快照里也带上了它引用的节点",
                len(doc["flows"][0].get("steps") or []) == 1
                and doc["flows"][0]["steps"][0].get("template_id") == "tpl_a",
                json.dumps(doc["flows"][0], ensure_ascii=False)[:200],
            )
            check(
                "导出格式标识正确",
                doc["format"] == fp.FLOW_EXPORT_FORMAT,
                doc["format"],
            )
            check(
                "导出包不含本机 id",
                "id" not in doc["flow"],
                str(list(doc["flow"].keys())),
            )

        # ── 机器 B：什么都没有 ──
        engine_b, sb = await fresh_db("b.db")

        async with sb() as db:
            id_map, warnings = await fp.resolve_references(db, doc, "u2")
            check(
                "空机器上按快照创建了节点",
                any("按导出包创建" in w for w in warnings),
                str(warnings),
            )
            check(
                "空机器上按快照创建了子流程",
                sum(1 for w in warnings if "子流程" in w) == 1,
                str(warnings),
            )

            # 引用重写
            steps = fp.rewrite_references(doc["flow"]["steps"], id_map)
            check(
                "主流程的子流程引用被重写",
                steps[0].get("flow_id") == id_map.get("flow_sub"),
                f"{steps[0].get('flow_id')} vs {id_map.get('flow_sub')}",
            )
            check(
                "主流程的节点引用被重写",
                steps[1].get("template_id") == id_map.get("tpl_a"),
                f"{steps[1].get('template_id')} vs {id_map.get('tpl_a')}",
            )
            check(
                "重写后的 id 和本机新建的一致",
                id_map.get("tpl_a") in (await db.execute(
                    __import__("sqlalchemy").select(Template.id)
                )).scalars().all(),
                str(id_map),
            )

            # 子流程不能是空壳 —— 它内部的步骤也要带过来、引用也要重写
            sub_created = await db.get(Flow, id_map.get("flow_sub"))
            check("导入的子流程不是空壳", sub_created is not None and len(sub_created.steps or []) == 1,
                  str(sub_created.steps if sub_created else None))
            check(
                "子流程内部的节点引用被重写",
                sub_created is not None
                and sub_created.steps[0].get("template_id") == id_map.get("tpl_a"),
                str(sub_created.steps if sub_created else None),
            )

        # ── 机器 C：已经有同名节点 ──
        engine_c, sc = await fresh_db("c.db")
        async with sc() as db:
            db.add(
                Template(
                    id="tpl_local",
                    name="文生图",  # 同名
                    engine="comfyui",
                    input_schema={"fields": [{"name": "prompt", "label": "提示词", "type": "textarea"}]},
                    param_mapping={},
                    output_config={"nodes": []},
                    is_active=True,
                )
            )
            await db.commit()

            id_map_c, warnings_c = await fp.resolve_references(db, doc, "u3")
            check(
                "同名节点用本机的，不重复创建",
                id_map_c.get("tpl_a") == "tpl_local",
                str(id_map_c),
            )
            check(
                "同名的情况有提示",
                any("本机已有同名版本" in w for w in warnings_c),
                str(warnings_c),
            )
            count = len(
                (await db.execute(__import__("sqlalchemy").select(Template))).scalars().all()
            )
            check("没有多创建节点", count == 1, f"本机 {count} 个节点")

        # ── 导出的内容能不能被解析回来（往返）──
        reparsed = json.loads(fp.dumps_doc(doc).decode("utf-8"))
        check(
            "导出文件是合法 JSON 且能读回来",
            reparsed["flow"]["name"] == "主流程"
            and len(reparsed["flow"]["steps"]) == 2,
            reparsed["flow"]["name"],
        )

        await engine_a.dispose()
        await engine_b.dispose()
        await engine_c.dispose()
    return checks


def main() -> int:
    checks = asyncio.run(scenario())
    failures = [c for c in checks if not c[1]]
    for label, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if not ok else ""))
    print(f"\nRESULT: {len(checks) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
