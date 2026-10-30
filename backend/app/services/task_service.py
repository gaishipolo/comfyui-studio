"""任务服务 — 任务提交、执行、状态管理"""
import uuid
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.models import Task, Template, Asset
from app.engines.factory import get_provider
from app.engines.base import GenerationResult, TaskStatus
from app.config import settings
from app.filestore import save_asset_file
from app.media import guess_mime


# 取消登记表：cancel_task() 写入，执行协程在轮询间隙读。
# 用**协作式**取消而不是 asyncio.Task.cancel() 硬砍 —— 那一刀随时可能落在
# db.commit() 或 httpx 请求半途上，把数据库会话留在脏状态；协作式只在
# 安全点退出，代价是最多晚 TASK_POLL_INTERVAL（2 秒）才停。
_cancel_requested: set[str] = set()


def _cancel_requested_for(task_id: str) -> bool:
    return task_id in _cancel_requested


def clear_cancel_request(task_id: str) -> None:
    """重试前清掉取消登记。

    不清的话，重试后的执行协程一进轮询就看到「已取消」，
    直接退出，任务永远停在 pending —— 这个坑比取消本身更隐蔽。
    """
    _cancel_requested.discard(task_id)


# 后台执行登记表。原来散在 api/tasks.py，但「谁在跑」是执行层的状态，
# 挪到这里让 reap_stale_tasks / resume_task 能共用同一份。
_running_tasks: set[str] = set()

# 并发闸门。settings.MAX_CONCURRENT_TASKS 以前是**死配置**（没人读它），
# 结果一次批量提交 N 个就同时打引擎 N 个 prompt、开 N 个轮询协程。
# ComfyUI 本来就串行执行，闸门放在我们这边能让排队可预测。
_run_slots = asyncio.Semaphore(max(1, int(settings.MAX_CONCURRENT_TASKS)))


async def _run_and_cleanup(task_id: str, resume: bool = False):
    try:
        if resume:
            await TaskService.resume_task(task_id)
        else:
            await TaskService.execute_task(task_id)
    finally:
        _running_tasks.discard(task_id)


async def spawn_task(task_id: str, resume: bool = False) -> bool:
    """启动后台执行协程（去重）。返回是否真的启动了。

    协程一创建就登记进 _running_tasks，删任务时靠它挡住「正在跑的不能删」。
    """
    if task_id in _running_tasks:
        return False
    _running_tasks.add(task_id)
    asyncio.create_task(_run_and_cleanup(task_id, resume=resume))
    return True


class TaskService:

    @staticmethod
    async def create_task(
        db: AsyncSession,
        template_id: str,
        params: dict,
        batch_id: str = "",
        created_by: str = "",
        commit: bool = True,
    ) -> Task:
        task = Task(
            template_id=template_id,
            batch_id=batch_id or uuid.uuid4().hex,
            params=params,
            created_by=created_by,
        )
        db.add(task)
        if commit:
            await db.commit()
            await db.refresh(task)
        else:
            await db.flush()  # 批量场景由调用方统一 commit
        return task

    @staticmethod
    async def cancel_task(db: AsyncSession, task_id: str) -> Task:
        """用户主动取消任务。只有未终态（pending/running）的任务能取消。

        顺序要紧：先登记 _cancel_requested 让执行协程尽快看到，再落库，
        最后通知引擎撤掉/中断 prompt。
        """
        task = await db.get(Task, task_id)
        if not task:
            raise LookupError(f"Task not found: {task_id}")
        if task.status in ("done", "error", "cancelled"):
            raise ValueError(f"任务已处于终态（{task.status}），无法取消")

        _cancel_requested.add(task_id)
        task.status = "cancelled"
        task.finished_at = datetime.utcnow()
        task.error_msg = ""
        await db.commit()
        await db.refresh(task)

        # 通知引擎停手。还没有 provider_task_id 说明还卡在 submit 之前/之中，
        # 那一段由 execute_task 在 submit 之后补刀，这里不用管。
        if task.provider_task_id:
            template = await db.get(Template, task.template_id)
            engine = template.engine if template else "comfyui"
            try:
                provider = get_provider(engine)
                await provider.cancel(task.provider_task_id)
            except NotImplementedError:
                # 这个引擎不支持取消：本地状态照样记成 cancelled，只是引擎那边会跑完
                pass
            except Exception as e:  # noqa: BLE001 —— 引擎侧失败不能反过来否定用户的取消
                print(f"[TaskService] 通知引擎取消失败（本地已标记 cancelled）: {e}")
        return task

    @staticmethod
    async def _finish_task(db: AsyncSession, task: Task, template: Template, provider, prompt_id: str):
        """等引擎出结果并落库。execute_task（新任务）与 resume_task（重启接回）共用。

        抽出来是为了让「重启后接着跑」不必再走一遍提交 —— prompt 已经在引擎上，
        重新挂上去轮询就行，跑完照常按 output_config 取产出、存素材。
        """
        result = await TaskService._poll_result(db, task, provider, prompt_id)

        if result.status == TaskStatus.CANCELLED:
            return  # 终态已由 cancel_task 写好，别覆盖

        if result.status.value == "done":
            task.status = "done"
            task.progress = 100.0
            # 只留勾选出图节点的产出 —— 一个工作流常有多个出图节点
            # （预览图、裁剪参考脸…），用户往往只要其中一部分
            outputs = TaskService._select_outputs(template, result.outputs)
            # 合并写入而不是整体覆盖 —— result 里可能已有请求快照（_request）
            task.result = {**(task.result or {}), "outputs": outputs}
            task.finished_at = datetime.utcnow()

            # 下载并保存素材
            for output in outputs:
                await TaskService._save_asset(db, task, template, provider, output)
        else:
            task.status = "error"
            task.error_msg = result.error or "Generation failed"
            task.finished_at = datetime.utcnow()

        await db.commit()

    @staticmethod
    async def execute_task(task_id: str):
        """后台执行任务：加载模板 → 注入参数 → 提交引擎 → 轮询结果 → 存储素材"""
        async with _run_slots:  # 并发闸门：超出 MAX_CONCURRENT_TASKS 的在这里排队
            async with async_session() as db:
                task = await db.get(Task, task_id)
                if not task:
                    return

                # 排队等闸门期间可能已经被取消了（cancel_task 不经过这里）
                if _cancel_requested_for(task.id) or task.status == "cancelled":
                    return

                template = await db.get(Template, task.template_id)
                if not template:
                    task.status = "error"
                    task.error_msg = "Template not found"
                    await db.commit()
                    return

                # 更新状态
                task.status = "running"
                task.started_at = datetime.utcnow()
                await db.commit()

                try:
                    provider = get_provider(template.engine)

                    # 加载工作流 JSON —— 仅当模板配置了 workflow_file 时才需要
                    # （通用 API 引擎通常不依赖 ComfyUI 工作流文件）
                    workflow: dict = {}
                    if template.workflow_file:
                        workflow_path = settings.WORKFLOW_DIR / Path(template.workflow_file).name
                        if not workflow_path.exists():
                            raise FileNotFoundError(f"Workflow file not found: {template.workflow_file}")
                        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))

                    # 提交上下文统一组装，Provider 各取所需：
                    # ComfyUI 用 _workflow/_param_mapping（在 submit 内做节点注入），
                    # HTTP 类用 _capability 调 build_payload。isinstance 特判没了。
                    ctx = {
                        "_type": template.category,
                        "_capability": template.capability or template.category,
                        "_template_name": template.name,
                        "_workflow": workflow,
                        "_param_mapping": template.param_mapping,
                    }

                    # 请求体快照：审计 + 重跑依据，不受后续配置/插件代码变更影响。
                    # Provider 提交时把实际发出的 payload 记进 last_request（基类默认 None，
                    # ComfyUI 的 payload 是整个工作流太大不记，params+param_mapping 已可重放）
                    prompt_id = await provider.submit(ctx, task.params)
                    snapshot = getattr(provider, "last_request", None)
                    if snapshot is not None:
                        task.result = {**(task.result or {}), "_request": snapshot}
                    task.provider_task_id = prompt_id
                    await db.commit()

                    # 取消竞态：用户可能在 submit 还没返回时就点了取消，
                    # 那会儿 cancel_task 拿不到 prompt_id、通知不了引擎。
                    # 这里补一刀，否则那条 prompt 会照跑不误、白占显存。
                    if _cancel_requested_for(task.id):
                        try:
                            await provider.cancel(prompt_id)
                        except Exception as e:  # noqa: BLE001
                            print(f"[TaskService] 补刀取消引擎任务失败: {e}")
                        return

                    await TaskService._finish_task(db, task, template, provider, prompt_id)

                except Exception as e:
                    # 已经取消的任务别再改写成 error，否则「已取消」会变成「失败」
                    if not _cancel_requested_for(task.id):
                        task.status = "error"
                        task.error_msg = str(e)
                        task.finished_at = datetime.utcnow()

                await db.commit()

    @staticmethod
    async def resume_task(task_id: str):
        """重启后接回一个引擎侧还在跑 / 已经跑完的任务。

        与 execute_task 的唯一区别是**不再 submit** —— prompt 已经在 ComfyUI 上了，
        我们只是重新挂上去等结果。这样 5 分钟的视频不会因为重启后端就白跑。
        """
        async with _run_slots:
            async with async_session() as db:
                task = await db.get(Task, task_id)
                if not task or not task.provider_task_id:
                    return
                if task.status in ("done", "error", "cancelled"):
                    return

                template = await db.get(Template, task.template_id)
                if not template:
                    task.status = "error"
                    task.error_msg = "Template not found"
                    task.finished_at = datetime.utcnow()
                    await db.commit()
                    return

                provider = get_provider(template.engine)
                task.status = "running"
                task.started_at = task.started_at or datetime.utcnow()
                await db.commit()

                try:
                    await TaskService._finish_task(db, task, template, provider, task.provider_task_id)
                except Exception as e:  # noqa: BLE001
                    if not _cancel_requested_for(task.id):
                        task.status = "error"
                        task.error_msg = f"重启后接回任务失败: {e}"
                        task.finished_at = datetime.utcnow()
                        await db.commit()

    @staticmethod
    async def reap_stale_tasks() -> dict:
        """启动时收拾上一个进程死掉时留下的 pending / running 任务。

        后台执行是**进程内**的 asyncio 协程，进程一没它们就永远停在 running，
        前端只能一直转圈（前面「结果加载中」那类问题的来源之一）。

        处置分两种：
          - 已经拿到 provider_task_id → 引擎那边多半还在跑或已经跑完，
            **接回去继续等结果**，别浪费那次生成
          - 连提交都没完成（没有 provider_task_id）→ 接不回去，标成可重试的失败

        只在启动时调用 —— 那时不可能有正在执行的任务，扫到的一定是上个进程遗留的。
        """
        async with async_session() as db:
            result = await db.execute(
                select(Task).where(Task.status.in_(["pending", "running"]))
            )
            stale = list(result.scalars().all())
            resumed: list[str] = []
            failed: list[str] = []
            for t in stale:
                if t.provider_task_id:
                    resumed.append(t.id)
                else:
                    t.status = "error"
                    t.error_msg = "服务重启时任务还没提交到引擎，可点击重试"
                    t.finished_at = datetime.utcnow()
                    failed.append(t.id)
            await db.commit()

        # 协程在会话外启动，别让启动流程被长任务卡住
        for task_id in resumed:
            await spawn_task(task_id, resume=True)

        return {"resumed": resumed, "failed": failed}

    @staticmethod
    async def _poll_result(
        db: AsyncSession,
        task: Task,
        provider,
        prompt_id: str,
        max_wait: int | None = None,
    ):
        """轮询引擎状态，期间把进度回写到 task.progress。

        max_wait 不传就用 settings.TASK_TIMEOUT_SECONDS。超时**必须**顺手把
        引擎侧的 prompt 停掉 —— 否则它会继续占着显存跑完，产物还成了没人收的孤儿。
        """
        max_wait = settings.TASK_TIMEOUT_SECONDS if max_wait is None else max_wait
        started = time.monotonic()
        interval = settings.TASK_POLL_INTERVAL
        while time.monotonic() - started < max_wait:
            # 取消优先于一切：在轮询间隙这个安全点退出
            if _cancel_requested_for(task.id):
                return GenerationResult(status=TaskStatus.CANCELLED)

            result = await provider.get_status(prompt_id)
            if result.status in (TaskStatus.DONE, TaskStatus.ERROR):
                return result
            # 进度回写，前端进度条实时可见
            if result.progress != task.progress:
                task.progress = float(result.progress)
                await db.commit()
            await asyncio.sleep(interval)

        # 超时：先让引擎停手，再回错误
        try:
            await provider.cancel(prompt_id)
        except Exception as e:  # noqa: BLE001
            print(f"[TaskService] 超时后取消引擎任务失败: {e}")
        return GenerationResult(
            status=TaskStatus.ERROR,
            error=f"任务执行超时（超过 {max_wait} 秒），已向引擎发出中断",
        )

    @staticmethod
    def _select_outputs(template: Template, outputs: list[dict]) -> list[dict]:
        """按 output_config.nodes 过滤出图节点的结果。

        空配置 = 不过滤。正常流程走不到这里（保存模板时强制勾选出图节点），
        留的是防御性兜底：宁可多存一张，也别让任务跑完一张图都不剩。
        """
        nodes = (template.output_config or {}).get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return outputs
        keep = {str(n) for n in nodes}
        return [o for o in outputs if str(o.get("node_id", "")) in keep]

    @staticmethod
    async def _save_asset(db: AsyncSession, task: Task, template: Template, provider, output: dict):
        """下载引擎输出文件并保存到本地存储"""
        # 产物是远程 URL 时把 URL 传下去 —— download_output 认 URL 直接下载。
        # generic_api 提取产物时 filename 已截成纯文件名（URL 在 url 字段），
        # 传纯文件名会拼出 /files//xxx.png 的错误路径，素材根本落不了盘
        src = output.get("url") or output["filename"]
        file_bytes = await provider.download_output(
            src, output.get("subfolder", ""), output.get("type", "output")
        )
        # 保存文件（文件名只取纯名字，防止路径穿越/非法字符）
        safe_name = Path(output["filename"]).name or f"{uuid.uuid4().hex}.bin"
        # 插件产物常是无扩展名的 CDN URL → 扩展名猜不出时看文件头，否则视频会被当图片渲染
        mime = guess_mime(safe_name, file_bytes)

        # 存储键固定用 POSIX 分隔符：Windows 下 str(Path) 是反斜杠，直接当对象键会跟
        # S3 / mc mirror 的 / 对不上号。老记录里存的是反斜杠，filestore 读入端两种都吃。
        rel_path = f"{task.id}/{safe_name}"
        save_asset_file(rel_path, file_bytes, content_type=mime)

        asset = Asset(
            task_id=task.id,
            file_path=rel_path,
            file_name=safe_name,
            file_size=len(file_bytes),
            mime_type=mime,
            tags=[template.category],
            meta=task.params,
        )
        db.add(asset)
        await db.flush()

