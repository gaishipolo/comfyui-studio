r"""清理真实库中的测试残留行（只删测试 task_id，不碰真实数据）"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from app.database import engine

# 测试脚本用过的 task_id 前缀
TEST_TASK_PREFIXES = ("persist-", "je-", "smoke-", "e2e-", "fix-")
# 测试脚本建的模板名前缀（任务/素材没有 DELETE 接口，只能按模板名级联清理）
TEST_TEMPLATE_PREFIXES = ("验证用-", "测试模板-")


async def main():
    async with engine.begin() as conn:
        # 1. 列出可疑行
        r = await conn.execute(text(
            "SELECT id, task_id, file_name, tags FROM assets ORDER BY created_at"))
        rows = r.fetchall()
        print("=== 当前 assets 全量 ===")
        for rid, tid, fname, tags in rows:
            print(f"  {rid[:10]}  task={tid!r}  file={fname!r}  tags={tags!r}")

        # 2. 识别测试残留
        victims = [rid for rid, tid, fname, tags in rows
                   if (tid or "").startswith(TEST_TASK_PREFIXES)]
        print(f"\n测试残留 {len(victims)} 条: {[v[:10] for v in victims]}")

        # 3. 同样清理孤儿 tasks（无模板关联或测试前缀）
        r = await conn.execute(text("SELECT id, template_id FROM tasks"))
        trows = r.fetchall()
        t_victims = [tid for tid, tpid in trows if (tid or "").startswith(TEST_TASK_PREFIXES)]
        print(f"测试残留 tasks {len(t_victims)} 条")

        # 4. 测试模板 + 级联（任务/素材没有 DELETE 接口，只能按模板名前缀定位）
        r = await conn.execute(text("SELECT id, name FROM templates"))
        tpl_rows = r.fetchall()
        tpl_victims = [(tid_, nm) for tid_, nm in tpl_rows
                       if (nm or "").startswith(TEST_TEMPLATE_PREFIXES)]
        print(f"测试残留 templates {len(tpl_victims)} 条: {[nm for _, nm in tpl_victims]}")
        tpl_ids = [tid_ for tid_, _ in tpl_victims]
        cascaded_tasks = [tid for tid, tpid in trows if tpid in tpl_ids]
        print(f"  级联 tasks {len(cascaded_tasks)} 条")

        # 5. 测试账号：验证脚本自己会删，但中途失败就会留下。
        #    它们没有任务、没有素材，但会污染「创建人」下拉和用量统计的用户列表。
        #    只删「deltest- 前缀 + 名下 0 任务」的，有数据的绝不碰。
        r = await conn.execute(text(
            "SELECT u.id, u.username, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.created_by = u.id) AS n "
            "FROM users u WHERE u.username LIKE 'deltest-%'"))
        u_victims = [(uid, nm) for uid, nm, n in r.fetchall() if n == 0]
        print(f"测试残留 users {len(u_victims)} 条: {[nm for _, nm in u_victims]}")

        if "--apply" not in sys.argv:
            print("\n[DRY-RUN] 加 --apply 参数才真正删除")
            return

        for rid in victims:
            await conn.execute(text("DELETE FROM assets WHERE id=:i"), {"i": rid})
        for tid in t_victims:
            await conn.execute(text("DELETE FROM assets WHERE task_id=:i"), {"i": tid})
            await conn.execute(text("DELETE FROM tasks WHERE id=:i"), {"i": tid})
        # 级联删：先素材，再任务，最后模板
        for tid in cascaded_tasks:
            await conn.execute(text("DELETE FROM assets WHERE task_id=:i"), {"i": tid})
            await conn.execute(text("DELETE FROM tasks WHERE id=:i"), {"i": tid})
        for tid_ in tpl_ids:
            await conn.execute(text("DELETE FROM templates WHERE id=:i"), {"i": tid_})
        for uid, _ in u_victims:
            await conn.execute(text("DELETE FROM users WHERE id=:i"), {"i": uid})

        r = await conn.execute(text("SELECT COUNT(*) FROM assets"))
        print(f"\n[APPLIED] 清理后 assets 共 {r.scalar_one()} 条")


asyncio.run(main())
