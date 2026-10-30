"""并发闸门 + 启动收拾 + 账号管理验证（P0 #2 / P0 #3）。

背景一（P0 #2）：`settings.MAX_CONCURRENT_TASKS` 原本是**死配置**——没人读它，
批量提交 N 个就同时打引擎 N 个 prompt、开 N 个轮询协程。而且后台执行是进程内
asyncio 协程，进程一死任务永远停在 running，前端只能一直转圈。

背景二（P0 #3）：admin 初始密码是启动时打到控制台的随机串，而此前没有任何改密
接口，等于密码永远改不掉；也没有「不能把自己关掉」这类保护。

本脚本覆盖：
  A. 并发闸门（结构 + 行为：峰值并发不超上限，且排队不丢任务）
  B. 后台执行登记表（spawn 去重、跑完清登记、异常也不泄漏）
  C. 重启接回 resume_task（**不重复 submit**，用已有 prompt_id 收尾）
  D. 启动收拾 reap_stale_tasks（两分支处置 + 不碰终态 + 真的接回去跑完）
  E. 账号管理（改密校验旧密码、重置密码权限、自锁保护、最后管理员保护）
  F. 回归守卫（闸门不能被绕过、启动必须走 reap、MAX_CONCURRENT_TASKS 不再是死配置）

全部用临时 SQLite + 假 provider，不碰 workbench.db，不连真引擎，可重复跑。
"""
import asyncio
import inspect
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

import app.services.task_service as ts
from app.engines.base import GenerationResult, GenerationProvider, TaskStatus
from app.models.models import Base, Task, Template, User
from app.services.task_service import TaskService
from app.schemas.auth_schemas import ChangePasswordRequest, ResetPasswordRequest, UserUpdateRequest
from app.api.auth_api import change_own_password, reset_user_password, update_user
from app.auth import hash_password, verify_password, require_admin
from fastapi import HTTPException

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


async def expect_http(label, coro, status, detail_contains=""):
    """跑一个会抛 HTTPException 的协程，核对状态码与文案。"""
    try:
        await coro
    except HTTPException as e:
        ok = e.status_code == status and detail_contains in str(e.detail)
        report(label, ok, f"got {e.status_code}: {e.detail}")
        return
    except Exception as e:  # noqa: BLE001
        report(label, False, f"抛了别的异常: {type(e).__name__}: {e}")
        return
    report(label, False, "没抛 HTTPException，直接过了")


# ── 假 provider：只做记账，不碰网络 ──────────────────────────
class FakeProvider(GenerationProvider):
    def __init__(self):
        self.submit_calls = []
        self.cancel_calls = []
        self.status_calls = []
        self.live = 0          # 当前有多少个 submit 同时在飞
        self.peak = 0          # 峰值并发
        self.hold = 0.0        # 每个 submit 停多久（用来制造重叠）
        self.result = GenerationResult(status=TaskStatus.DONE, outputs=[])

    async def submit(self, workflow, params):
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.submit_calls.append(workflow)
        try:
            if self.hold:
                await asyncio.sleep(self.hold)
        finally:
            self.live -= 1
        return f"p-{len(self.submit_calls)}"

    async def get_status(self, prompt_id):
        self.status_calls.append(prompt_id)
        return self.result

    async def cancel(self, prompt_id):
        self.cancel_calls.append(prompt_id)
        return True

    async def download_output(self, filename, subfolder="", output_type="output"):
        return b"x"

    async def health_check(self):
        return True


class RaisingProvider(FakeProvider):
    """execute_task 一调 submit 就炸，用来验证异常路径也不泄漏登记。"""

    async def submit(self, workflow, params):
        raise RuntimeError("boom")


# ── 测试库：临时 SQLite，跑完即弃 ──────────────────────────
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
TEST_DB_URL = f"sqlite+aiosqlite:///{_tmp.name}"
engine = create_async_engine(TEST_DB_URL)
TestSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# task_service 里是 `from app.database import async_session`，名字绑在它自己的命名空间，
# 所以改 ts.async_session 就能让 execute_task / resume_task / reap_stale_tasks 全走测试库。
ts.async_session = TestSession


async def fresh_db() -> AsyncSession:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return TestSession()


async def add_template(db, tid="tpl", name="测试模板"):
    db.add(Template(id=tid, name=name, engine="fake", workflow_file="", category="image"))
    await db.commit()
    return tid


async def add_task(db, tid, template_id, status="pending", provider_task_id=""):
    db.add(Task(id=tid, template_id=template_id, status=status,
                provider_task_id=provider_task_id, params={}))
    await db.commit()
    return tid


async def reload(model, pk):
    """读一条记录的**已提交**最终状态。

    每次开全新会话，两个坑都绕开：
      1. 写库的是别的会话（execute_task / resume_task / reap 各自开的），
         复用旧会话会命中身份映射里的旧对象，读到改写前的值；
      2. 千万别靠 rollback 刷新 —— rollback 会把会话里**所有**实例置为过期，
         下次同步读属性（比如 report 里拿 .status）就触发懒加载 IO，
         在异步上下文里直接 MissingGreenlet 炸掉。
    读完即脱会话，返回的对象属性已加载，随便读都不会再碰数据库。
    """
    async with TestSession() as s:
        obj = await s.get(model, pk)
        if obj is not None:
            s.expunge(obj)
        return obj


async def main():
    global ok_all

    # ════ A. 并发闸门 ═══════════════════════════════════════
    print("=== A. 并发闸门（MAX_CONCURRENT_TASKS 不再是死配置） ===")

    src = inspect.getsource(ts)
    report("MAX_CONCURRENT_TASKS 真的被读取", "MAX_CONCURRENT_TASKS" in src)
    report("闸门是个 Semaphore", isinstance(ts._run_slots, asyncio.Semaphore),
           type(ts._run_slots).__name__)
    import app.config as cfg
    want = max(1, int(cfg.settings.MAX_CONCURRENT_TASKS))
    report("闸门初值 == MAX_CONCURRENT_TASKS", getattr(ts._run_slots, "_value", None) == want,
           f"_value={getattr(ts._run_slots, '_value', None)} want={want}")

    # 行为：5 个任务同时来，峰值并发必须被压在上限内，且一个都不能丢
    db = await fresh_db()
    await add_template(db, "tpl-gate", "闸门模板")
    for i in range(5):
        await add_task(db, f"tg{i}", "tpl-gate")
    prov = FakeProvider()
    prov.hold = 0.12          # 让先到的两个在 submit 里重叠，峰值才测得出来
    ts.get_provider = lambda engine: prov

    await asyncio.gather(*[TaskService.execute_task(f"tg{i}") for i in range(5)])
    report(f"5 个并发任务峰值并发 ≤ {want}", prov.peak <= want, f"peak={prov.peak}")
    report("闸门没把并行压成串行（峰值 = 上限）", prov.peak == want, f"peak={prov.peak}")
    report("排队不丢任务：5 个都提交给了引擎", len(prov.submit_calls) == 5,
           f"submitted={len(prov.submit_calls)}")
    done_n = 0
    for i in range(5):
        t = await reload(Task, f"tg{i}")
        done_n += (t.status == "done")
    report("5 个都跑完了（done）", done_n == 5, f"done={done_n}/5")

    # ════ B. 后台执行登记表 ═════════════════════════════════
    print("=== B. 后台执行登记表（spawn_task） ===")

    orig_execute = TaskService.execute_task
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_execute(task_id):
        started.set()
        await release.wait()

    TaskService.execute_task = staticmethod(slow_execute)
    first = await ts.spawn_task("t-dup")
    await asyncio.wait_for(started.wait(), timeout=2)
    second = await ts.spawn_task("t-dup")
    report("同一任务重复 spawn 返回 False（去重）", first and not second,
           f"first={first} second={second}")
    report("运行期间登记在册", "t-dup" in ts._running_tasks)
    release.set()
    await asyncio.sleep(0.05)
    report("跑完后登记自动清掉", "t-dup" not in ts._running_tasks)

    # 异常路径也不能泄漏登记（finally 兜底）
    started.clear()
    release.set()

    async def boom_execute(task_id):
        raise RuntimeError("boom")

    TaskService.execute_task = staticmethod(boom_execute)
    await ts.spawn_task("t-boom")
    await asyncio.sleep(0.1)
    report("异常退出也不泄漏登记", "t-boom" not in ts._running_tasks)
    TaskService.execute_task = staticmethod(orig_execute)

    # ════ C. resume_task：接回去，但绝不重复提交 ═══════════
    print("=== C. resume_task（重启接回不重复 submit） ===")

    db = await fresh_db()
    await add_template(db, "tpl-res", "接回模板")
    await add_task(db, "tr1", "tpl-res", status="running", provider_task_id="p-resume")
    prov = FakeProvider()
    prov.result = GenerationResult(status=TaskStatus.DONE, outputs=[])
    ts.get_provider = lambda engine: prov

    await TaskService.resume_task("tr1")
    t = await reload(Task, "tr1")
    report("接回后任务跑完（done）", t.status == "done", f"status={t.status}")
    report("接回**没有**重新 submit（不重复占显存）", prov.submit_calls == [],
           f"submit_calls={len(prov.submit_calls)}")
    report("用的是重启前那个 prompt_id", prov.status_calls == ["p-resume"],
           f"status_calls={prov.status_calls}")

    await add_task(db, "tr2", "tpl-res", status="running", provider_task_id="")  # 没提交成
    prov2 = FakeProvider()
    ts.get_provider = lambda engine: prov2
    await TaskService.resume_task("tr2")
    t2 = await reload(Task, "tr2")
    report("没有 provider_task_id 的接不回去 → 原样不动", t2.status == "running",
           f"status={t2.status}")
    report("那种情况不碰引擎", prov2.status_calls == [] and prov2.submit_calls == [])

    await add_task(db, "tr3", "tpl-res", status="done", provider_task_id="p-old")
    await TaskService.resume_task("tr3")
    t3 = await reload(Task, "tr3")
    report("终态任务不被接回改写", t3.status == "done", f"status={t3.status}")

    # ════ D. reap_stale_tasks：启动收拾两分支 ═══════════════
    print("=== D. reap_stale_tasks（启动收拾遗留任务） ===")

    db = await fresh_db()
    await add_template(db, "tpl-reap", "收拾模板")
    await add_task(db, "rp1", "tpl-reap", status="running", provider_task_id="p-reap")
    await add_task(db, "rp2", "tpl-reap", status="pending", provider_task_id="")
    await add_task(db, "rp3", "tpl-reap", status="done", provider_task_id="p-done")
    await add_task(db, "rp4", "tpl-reap", status="error", provider_task_id="")
    await add_task(db, "rp5", "tpl-reap", status="cancelled", provider_task_id="")

    spawned = []
    orig_spawn = ts.spawn_task

    async def fake_spawn(task_id, resume=False):
        spawned.append((task_id, resume))
        return True

    ts.spawn_task = fake_spawn
    prov = FakeProvider()
    ts.get_provider = lambda engine: prov
    outcome = await TaskService.reap_stale_tasks()
    ts.spawn_task = orig_spawn

    report("有 prompt_id 的归入「接回」", outcome["resumed"] == ["rp1"], str(outcome))
    report("接回要走 resume=True（不能重走提交）", ("rp1", True) in spawned, str(spawned))
    report("没提交成的归入「失败」", outcome["failed"] == ["rp2"], str(outcome))

    rp2 = await reload(Task, "rp2")
    report("没提交成的标成 error", rp2.status == "error", f"status={rp2.status}")
    report("失败文案要说清可以重试", "重试" in (rp2.error_msg or ""), rp2.error_msg)
    report("失败任务要有收尾时间", rp2.finished_at is not None)

    rp3 = await reload(Task, "rp3")
    rp4 = await reload(Task, "rp4")
    rp5 = await reload(Task, "rp5")
    report("终态任务一律不碰（done/error/cancelled）",
           (rp3.status, rp4.status, rp5.status) == ("done", "error", "cancelled"),
           f"{rp3.status}/{rp4.status}/{rp5.status}")

    # 端到端：真的接回去跑完（用户选的策略是「尽量接回去跑完」）
    db = await fresh_db()
    await add_template(db, "tpl-e2e", "端到端模板")
    await add_task(db, "rp-live", "tpl-e2e", status="running", provider_task_id="p-live")
    prov = FakeProvider()
    ts.get_provider = lambda engine: prov
    await TaskService.reap_stale_tasks()
    await asyncio.sleep(0.3)          # 让 spawn 出来的协程跑完
    rp_live = await reload(Task, "rp-live")
    report("重启后接回的任务真跑完了（done）", rp_live.status == "done",
           f"status={rp_live.status}")
    report("接回全程没再 submit", prov.submit_calls == [],
           f"submit_calls={len(prov.submit_calls)}")
    report("接回用的正是遗留的 prompt_id", prov.status_calls == ["p-live"],
           str(prov.status_calls))

    # ════ E. 账号管理 ═══════════════════════════════════════
    print("=== E. 账号管理（P0 #3） ===")

    db = await fresh_db()
    admin = User(id="u-admin", username="admin", password_hash=hash_password("oldpw123"),
                 role="admin", is_active=True)
    member = User(id="u-member", username="bob", password_hash=hash_password("memberpw1"),
                  role="member", is_active=True)
    db.add_all([admin, member])
    await db.commit()

    # 改自己的密码
    await expect_http("旧密码不对 → 400",
                      change_own_password(ChangePasswordRequest(old_password="wrong", new_password="newpw123"),
                                          admin, db),
                      400, "当前密码不正确")
    await expect_http("新旧密码相同 → 400",
                      change_own_password(ChangePasswordRequest(old_password="oldpw123", new_password="oldpw123"),
                                          admin, db),
                      400, "相同")
    await change_own_password(ChangePasswordRequest(old_password="oldpw123", new_password="newpw123"), admin, db)
    report("改密成功后新密码生效", verify_password("newpw123", admin.password_hash))
    report("改密成功后旧密码作废", not verify_password("oldpw123", admin.password_hash))
    report("普通成员也能改自己的密码（不必是 admin）",
           await _member_can_change(db, member))

    # 权限：重置他人密码只给 admin
    try:
        await require_admin(member)
        report("member 不能重置他人密码 → 403", False, "require_admin 放行了")
    except HTTPException as e:
        report("member 不能重置他人密码 → 403", e.status_code == 403, str(e.detail))

    await reset_user_password("u-member", ResetPasswordRequest(new_password="resetpw1"), admin, db)
    report("admin 重置他人密码生效", verify_password("resetpw1", member.password_hash))
    await expect_http("重置不存在的用户 → 404",
                      reset_user_password("u-ghost", ResetPasswordRequest(new_password="abc123"), admin, db),
                      404, "not found")

    # 密码长度下限
    try:
        ChangePasswordRequest(old_password="x", new_password="12345")
        report("新密码少于 6 位 → 校验拒绝", False, "居然构造成功了")
    except ValidationError:
        report("新密码少于 6 位 → 校验拒绝", True)
    try:
        ResetPasswordRequest(new_password="12345")
        report("重置密码少于 6 位 → 校验拒绝", False, "居然构造成功了")
    except ValidationError:
        report("重置密码少于 6 位 → 校验拒绝", True)

    # 自锁保护
    await expect_http("不能停用自己的账号 → 400",
                      update_user("u-admin", UserUpdateRequest(is_active=False), admin, db),
                      400, "不能停用")
    await expect_http("不能取消自己的管理员身份 → 400",
                      update_user("u-admin", UserUpdateRequest(role="member"), admin, db),
                      400, "不能取消")
    after = await reload(User, "u-admin")
    report("被拦下后账号没被改动", after.is_active and after.role == "admin")

    # 最后管理员保护（兜底网）。正常情况下自锁规则会先拦住「对自己下手」，
    # 这条只在别的调用路径对**唯一**启用管理员下手时才轮得到，所以用一个
    # 不在库里的调用者来逼出这个分支，确保兜底网本身没坏。
    ghost = SimpleNamespace(id="u-ghost-admin", role="admin")
    await expect_http("最后一个启用管理员不能被降级 → 400",
                      update_user("u-admin", UserUpdateRequest(role="member"), ghost, db),
                      400, "至少一个启用的管理员")
    await expect_http("最后一个启用管理员不能被停用 → 400",
                      update_user("u-admin", UserUpdateRequest(is_active=False), ghost, db),
                      400, "至少一个启用的管理员")

    # 正向：有两个启用管理员时，可以降级其中一个
    admin2 = User(id="u-admin2", username="admin2", password_hash=hash_password("adminpw2"),
                  role="admin", is_active=True)
    db.add(admin2)
    await db.commit()
    await update_user("u-admin2", UserUpdateRequest(role="member"), admin, db)
    a2 = await reload(User, "u-admin2")
    report("两个启用管理员时可降级其中一个", a2.role == "member", f"role={a2.role}")

    # 普通成员可正常停用
    await update_user("u-member", UserUpdateRequest(is_active=False), admin, db)
    m = await reload(User, "u-member")
    report("普通成员可停用", m.is_active is False)

    # 显示名可改
    await update_user("u-member", UserUpdateRequest(display_name="小波"), admin, db)
    m = await reload(User, "u-member")
    report("显示名可改", m.display_name == "小波", m.display_name)

    # ════ F. 回归守卫 ═══════════════════════════════════════
    print("=== F. 回归守卫 ===")

    main_src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    report("启动必须走 reap_stale_tasks（接回去跑完）", "reap_stale_tasks" in main_src)
    report("不能再用旧的 recover_stale_tasks（它一律标失败）",
           "recover_stale_tasks" not in main_src)

    esrc = inspect.getsource(TaskService.execute_task)
    rsrc = inspect.getsource(TaskService.resume_task)
    report("execute_task 必须套在 _run_slots 里", "_run_slots" in esrc)
    report("resume_task 必须套在 _run_slots 里（否则接回能绕过闸门）", "_run_slots" in rsrc)
    report("两个入口都走 _finish_task（收尾不变量只写一处）",
           "_finish_task" in esrc and "_finish_task" in rsrc)

    asrc = inspect.getsource(ts.spawn_task)
    report("spawn_task 必须去重（_running_tasks 挡重复启动）", "_running_tasks" in asrc)

    csrc = inspect.getsource(TaskService.update_user) if hasattr(TaskService, "update_user") else \
        inspect.getsource(update_user)
    report("不能停用自己", "不能停用自己的账号" in csrc)
    report("不能降级自己", "不能取消自己的管理员身份" in csrc)
    report("保住最后一个启用管理员", "至少一个启用的管理员" in csrc)

    cpsrc = inspect.getsource(change_own_password)
    report("改密必须校验旧密码", "verify_password(data.old_password" in cpsrc)
    report("改密不允许新旧相同", "不能与当前密码相同" in cpsrc)

    # 探活与引擎诊断：前者必须快且匿名，后者含异常详情只能给 admin
    hsrc = (Path(__file__).resolve().parents[1] / "app" / "api" / "health.py").read_text(encoding="utf-8")
    report("探活必须封顶（下游引擎卡住不能拖死 /health）",
           "asyncio.wait_for" in hsrc and "_PROBE_TIMEOUT" in hsrc)
    report("引擎探测并发（6 个串行会排 12 秒队）", "asyncio.gather" in hsrc)
    report("/engines 需要登录（匿名探测要挡住）", "get_current_user" in hsrc)
    report("引擎异常详情只给 admin（内网地址/上游报错）",
           'user.role == "admin"' in hsrc)
    report("引擎异常详情脱敏（抹掉 URL）", "_URL_RE.sub" in hsrc)
    report("/health 保持匿名可访问（探活不该要 token）", "async def health():" in hsrc)

    await engine.dispose()

    print()
    print("ALL PASS" if ok_all else "SOME FAILED")
    return 0 if ok_all else 1


async def _member_can_change(db, member) -> bool:
    """普通成员改自己的密码应当成功。"""
    try:
        await change_own_password(
            ChangePasswordRequest(old_password="memberpw1", new_password="memberpw2"),
            member, db,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    (member 改密失败: {e})")
        return False
    return verify_password("memberpw2", member.password_hash)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
