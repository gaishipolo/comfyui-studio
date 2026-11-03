"""按引擎分组的并发闸门回归。

背景：`_run_slots` 曾经是**全局** Semaphore(MAX_CONCURRENT_TASKS)，包住整个
execute_task。ComfyUI 占的是本地 GPU，一张卡同时跑不了两个 prompt，全局闸门
是对的；但云端 API 供应商不占显存，它的限制只在 HTTP 连接和上游速率上 ——
让它跟 GPU 抢槽位毫无道理，「ComfyUI 在跑视频」和「万相在出图」会互相堵。

不变量：
1. 没声明 / 声明 0 的引擎 → 共用 `_run_slots`（GPU 语义不变）
2. 声明了 max_concurrency=N → 自己的 Semaphore(N)，不占也不被占
3. 同一引擎名多次取 → 同一个实例（每次新建等于没限流）
4. 两个声明引擎互相独立
5. 未知引擎不在这里抛错（错误留给 acquire_provider 落库给前端看）
6. 行为：API 池被打满时，GPU 任务照样能立刻开跑
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.engines import factory  # noqa: E402
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus  # noqa: E402
from app.models.models import Task, Template  # noqa: E402
from app.services import task_service as ts  # noqa: E402

GPU_ENGINE = "fake-gpu-engine"
API_ENGINE = "fake-api-engine"
API_LIMIT = 3

_results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))


class _FakeBase(GenerationProvider):
    def __init__(self, config=None):
        super().__init__(config)
        self.live = 0
        self.peak = 0
        self.hold = 0.0
        self.submits: list[str] = []

    async def submit(self, ctx, params):
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.submits.append(ctx.get("_template_name", ""))
        try:
            if self.hold:
                await asyncio.sleep(self.hold)
        finally:
            self.live -= 1
        return f"p-{len(self.submits)}"

    async def get_status(self, task_id):
        return GenerationResult(status=TaskStatus.DONE, outputs=[])

    async def download_output(self, filename, subfolder="", output_type="output"):
        return b"x"

    async def health_check(self):
        return True


class FakeGpu(_FakeBase):
    """不声明 max_concurrency —— 模拟 ComfyUI：共用全局 GPU 槽池。"""

    name = GPU_ENGINE
    label = "假 GPU"


class FakeApi(_FakeBase):
    """声明自己的并发预算 —— 模拟云端 API 供应商。"""

    name = API_ENGINE
    label = "假云端 API"
    max_concurrency = API_LIMIT


_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
engine = create_async_engine(f"sqlite+aiosqlite:///{_tmp.name}")
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# task_service 里 `from app.database import async_session` 是自己的命名空间绑定，
# 改它就等于让 execute_task / _peek_engine 全走测试库
ts.async_session = Session


async def fresh_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def add_case(template_id: str, task_id: str, engine_name: str) -> None:
    async with Session() as db:
        db.add(
            Template(
                id=template_id,
                name=template_id,
                engine=engine_name,
                workflow_file="",
                category="image",
                input_schema={"fields": []},
                param_mapping={},
                output_config={},
                is_active=True,
            )
        )
        await db.commit()
        db.add(
            Task(
                id=task_id,
                template_id=template_id,
                status="pending",
                params={},
            )
        )
        await db.commit()


def test_undeclared_engine_shares_global_pool() -> None:
    factory.register_provider(GPU_ENGINE, FakeGpu)
    try:
        check(
            "没声明 max_concurrency 的引擎共用 _run_slots",
            ts._slots_for(GPU_ENGINE) is ts._run_slots,
            f"got={ts._slots_for(GPU_ENGINE)!r}",
        )
    finally:
        factory.unregister_provider(GPU_ENGINE)


def test_declared_engine_gets_own_pool() -> None:
    factory.register_provider(API_ENGINE, FakeApi)
    try:
        slots = ts._slots_for(API_ENGINE)
        check("声明了 max_concurrency 的引擎不共用全局池", slots is not ts._run_slots)
        check(
            f"独立池容量 = 声明值 {API_LIMIT}",
            getattr(slots, "_value", None) == API_LIMIT,
            f"_value={getattr(slots, '_value', None)}",
        )
    finally:
        factory.unregister_provider(API_ENGINE)


def test_same_engine_reuses_one_pool() -> None:
    factory.register_provider(API_ENGINE, FakeApi)
    try:
        first = ts._slots_for(API_ENGINE)
        second = ts._slots_for(API_ENGINE)
        check("同一引擎多次取到同一个信号量", first is second)
    finally:
        factory.unregister_provider(API_ENGINE)


def test_two_declared_engines_are_independent() -> None:
    class FakeApi2(FakeApi):
        name = "fake-api-engine-2"
        max_concurrency = 5

    factory.register_provider(API_ENGINE, FakeApi)
    factory.register_provider("fake-api-engine-2", FakeApi2)
    try:
        a = ts._slots_for(API_ENGINE)
        b = ts._slots_for("fake-api-engine-2")
        check("两个声明引擎各自独立", a is not b and a is not ts._run_slots)
        check(
            "各自容量取自己的声明值",
            (getattr(a, "_value", None), getattr(b, "_value", None)) == (API_LIMIT, 5),
            f"a={getattr(a, '_value', None)} b={getattr(b, '_value', None)}",
        )
    finally:
        factory.unregister_provider(API_ENGINE)
        factory.unregister_provider("fake-api-engine-2")


def test_unknown_engine_falls_back_instead_of_raising() -> None:
    try:
        slots = ts._slots_for("engine-that-does-not-exist")
        check("未知引擎退回共用池而不抛错", slots is ts._run_slots)
    except Exception as exc:  # noqa: BLE001
        check("未知引擎退回共用池而不抛错", False, f"{type(exc).__name__}: {exc}")


async def _wait_until_saturated(provider: _FakeBase, timeout: float = 2.0) -> int:
    """等并发数不再上涨，返回稳定下来的峰值。

    不用「live >= 期望值」当条件：那样测的就不是分池有没有生效，而是分池
    容量够不够。这里等它自然饱和，再拿峰值去和两个容量比 —— 共用池会停在
    MAX_CONCURRENT_TASKS，独立池会停在声明的 API_LIMIT，一眼能分辨。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = -1
    stable_since: float | None = None
    while loop.time() < deadline:
        await asyncio.sleep(0.02)
        if provider.live != last:
            last = provider.live
            stable_since = loop.time()
        elif stable_since is not None and loop.time() - stable_since > 0.15 and last >= 1:
            return last
    return -1


async def test_api_pool_does_not_block_gpu() -> None:
    """API 池被打满时，GPU 任务必须能立刻开跑。

    这是分池的全部意义。不分池的话 GPU 任务会排在 _run_slots 后面，
    得等 API 任务跑完一轮才能开始 —— 明明两者毫不相干。
    """
    factory.register_provider(GPU_ENGINE, FakeGpu)
    factory.register_provider(API_ENGINE, FakeApi)
    try:
        await fresh_db()
        gpu_prov: FakeGpu = factory.get_provider(GPU_ENGINE)  # type: ignore[assignment]
        api_prov: FakeApi = factory.get_provider(API_ENGINE)  # type: ignore[assignment]
        gpu_prov.hold = 0.0
        api_prov.hold = 0.8

        gpu_started = asyncio.Event()

        original_gpu_submit = gpu_prov.submit

        async def gpu_submit(ctx, params):
            gpu_started.set()
            return await original_gpu_submit(ctx, params)

        gpu_prov.submit = gpu_submit  # type: ignore[method-assign]

        # 任务数要多于任何可能的池容量，才能看出饱和点停在哪
        for i in range(6):
            await add_case(f"tpl-api-{i}", f"task-api-{i}", API_ENGINE)
        await add_case("tpl-gpu", "task-gpu", GPU_ENGINE)

        api_jobs = [
            asyncio.create_task(ts.TaskService.execute_task(f"task-api-{i}"))
            for i in range(6)
        ]
        peak = await _wait_until_saturated(api_prov)
        check(
            "API 池饱和在声明的容量（不是全局 GPU 容量）",
            peak == API_LIMIT,
            f"peak={peak} 期望={API_LIMIT}（全局池容量 {settings.MAX_CONCURRENT_TASKS}）",
        )

        gpu_job = asyncio.create_task(ts.TaskService.execute_task("task-gpu"))
        try:
            # API 每个 submit 占 0.8s；若 GPU 被同一个池堵住，至少等一轮才动
            await asyncio.wait_for(gpu_started.wait(), timeout=0.4)
            check("API 池打满时 GPU 任务不被堵（0.4s 内开工）", True)
        except asyncio.TimeoutError:
            check(
                "API 池打满时 GPU 任务不被堵（0.4s 内开工）",
                False,
                "0.4 秒内没开始提交，说明还在跟 API 抢槽位",
            )
        await asyncio.gather(gpu_job, *api_jobs, return_exceptions=True)
    finally:
        factory.unregister_provider(GPU_ENGINE)
        factory.unregister_provider(API_ENGINE)


async def main() -> int:
    test_undeclared_engine_shares_global_pool()
    test_declared_engine_gets_own_pool()
    test_same_engine_reuses_one_pool()
    test_two_declared_engines_are_independent()
    test_unknown_engine_falls_back_instead_of_raising()
    await test_api_pool_does_not_block_gpu()

    failed = [r for r in _results if not r[1]]
    print(f"\nRESULT: {len(_results) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
