"""Safe error boundary and event-loop responsiveness verification.

This script uses a temporary SQLite database and fake providers. It does not
contact external engines or write to the configured FileStore.
"""
import asyncio
import logging
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.task_service as ts
from app.config import settings
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus
from app.models.models import Base, Task, Template
from app.safe_detail import safe_detail
from app.services.task_service import TaskService


ok_all = True


def report(label, condition, detail=""):
    global ok_all
    ok_all &= bool(condition)
    suffix = f"  {detail}" if detail else ""
    print(f"  [{'OK' if condition else 'FAIL'}] {label}{suffix}")


class RecordHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class FakeProvider(GenerationProvider):
    def __init__(self, submit_error="", status_error="", output=b"data"):
        super().__init__()
        self.submit_error = submit_error
        self.status_error = status_error
        self.output = output

    async def submit(self, ctx, params):
        if self.submit_error:
            raise RuntimeError(self.submit_error)
        return "provider-task"

    async def get_status(self, task_id):
        if self.status_error:
            raise RuntimeError(self.status_error)
        return GenerationResult(status=TaskStatus.DONE, outputs=[])

    async def download_output(self, filename, subfolder="", output_type="output"):
        return self.output

    async def health_check(self):
        return True


class FakeAssetDb:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


async def reset_db(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


async def seed_task(session_factory, task_id, *, workflow_file="", provider_task_id=""):
    template_id = f"tpl-{task_id}"
    async with session_factory() as db:
        db.add(Template(
            id=template_id,
            name=f"Template {task_id}",
            engine="fake",
            workflow_file=workflow_file,
            category="image",
        ))
        db.add(Task(
            id=task_id,
            template_id=template_id,
            status="running" if provider_task_id else "pending",
            provider_task_id=provider_task_id,
            params={},
        ))
        await db.commit()


async def load_task(session_factory, task_id):
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        db.expunge(task)
        return task


def matching_record(records, task_id):
    return next((record for record in records if task_id in record.getMessage()), None)


def check_failure_log(label, record, task_id, raw_error):
    message = record.getMessage() if record else ""
    rendered = logging.Formatter("%(levelname)s %(message)s").format(record) if record else ""
    report(f"{label}: stable task/provider context", bool(record)
           and task_id in message and "fake" in message and raw_error not in message,
           message)
    report(f"{label}: raw exception retained in traceback", bool(record)
           and record.exc_info is not None and raw_error in rendered)


async def run_blocking_gate(label, operation, started, release, state):
    ticks = 0

    async def ticker():
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 1.5
        while not started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        if not started.is_set():
            return
        for _ in range(4):
            ticks += 1
            await asyncio.sleep(0.01)
        release.set()

    results = await asyncio.wait_for(
        asyncio.gather(operation(), ticker(), return_exceptions=True),
        timeout=2.5,
    )
    operation_error = results[0] if isinstance(results[0], BaseException) else None
    report(label, operation_error is None and state.get("released") is True and ticks >= 4,
           f"ticks={ticks} released={state.get('released')} error={operation_error!r}")


async def main():
    print("=== safe_detail sanitization ===")
    combined = (
        "request http://10.0.0.5/private?api_key=TOPSECRET failed\n"
        "Authorization: Bearer abc.def.ghi\n"
        "token=TOPSECRET password=hunter2"
    )
    detail = safe_detail(RuntimeError(combined))
    forbidden = ["10.0.0.5", "TOPSECRET", "abc.def.ghi", "hunter2"]
    report("URL, Authorization, Bearer, token, and password values are absent",
           all(value not in detail for value in forbidden), detail)
    report("exception type is retained", detail.startswith("RuntimeError:"), detail)
    report("browser-visible detail is capped at 200 characters", len(detail) <= 200,
           f"length={len(detail)}")

    assignment_cases = {
        "API_KEY": "UPPER_KEY_VALUE",
        "apikey": "PLAIN_KEY_VALUE",
        "ToKeN": "MIXED_TOKEN_VALUE",
        "SECRET": "SECRET_VALUE",
        "Password": "PASSWORD_VALUE",
    }
    for key, value in assignment_cases.items():
        rendered = safe_detail(ValueError(f"provider rejected {key}={value}"))
        report(f"case-insensitive {key} assignment is sanitized", value not in rendered, rendered)
    bearer = safe_detail(RuntimeError("provider rejected Bearer standalone.token.value"))
    report("standalone Bearer token is sanitized", "standalone.token.value" not in bearer, bearer)

    temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    original_session = ts.async_session
    original_get_provider = ts.get_provider
    ts.async_session = session_factory

    handler = RecordHandler()
    ts.logger.addHandler(handler)
    try:
        print("\n=== task exception boundary ===")
        await reset_db(engine)
        execute_raw = (
            "submit failed at http://10.0.0.5/private?api_key=TOPSECRET "
            "Authorization: Bearer abc.def.ghi token=TOPSECRET password=hunter2"
        )
        await seed_task(session_factory, "execute-failure")
        ts.get_provider = lambda engine_name: FakeProvider(submit_error=execute_raw)
        handler.records.clear()
        await TaskService.execute_task("execute-failure")
        task = await load_task(session_factory, "execute-failure")
        report("execute_task stores only safe_detail", task.status == "error"
               and task.error_msg.startswith("RuntimeError:")
               and all(value not in task.error_msg for value in forbidden)
               and len(task.error_msg) <= 200,
               task.error_msg)
        check_failure_log(
            "execute_task log",
            matching_record(handler.records, "execute-failure"),
            "execute-failure",
            execute_raw,
        )

        await reset_db(engine)
        resume_raw = "resume failed secret=RESUMESECRET Bearer resume.token.value"
        await seed_task(
            session_factory,
            "resume-failure",
            provider_task_id="provider-existing",
        )
        ts.get_provider = lambda engine_name: FakeProvider(status_error=resume_raw)
        handler.records.clear()
        await TaskService.resume_task("resume-failure")
        task = await load_task(session_factory, "resume-failure")
        report("resume_task stores only safe_detail", task.status == "error"
               and task.error_msg.startswith("RuntimeError:")
               and "RESUMESECRET" not in task.error_msg
               and "resume.token.value" not in task.error_msg
               and len(task.error_msg) <= 200,
               task.error_msg)
        check_failure_log(
            "resume_task log",
            matching_record(handler.records, "resume-failure"),
            "resume-failure",
            resume_raw,
        )

        print("\n=== fire-and-forget exception consumption ===")
        original_execute = TaskService.execute_task
        unexpected_raw = "unexpected background failure token=BACKGROUNDSECRET"

        async def explode(task_id):
            raise RuntimeError(unexpected_raw)

        TaskService.execute_task = staticmethod(explode)
        ts._running_tasks.add("cleanup-failure")
        handler.records.clear()
        escaped = None
        try:
            await ts._run_and_cleanup("cleanup-failure")
        except Exception as exc:  # The boundary must consume this.
            escaped = exc
        finally:
            TaskService.execute_task = staticmethod(original_execute)
        report("_run_and_cleanup consumes unexpected exceptions", escaped is None,
               repr(escaped))
        report("_run_and_cleanup always clears the running registry",
               "cleanup-failure" not in ts._running_tasks)
        cleanup_record = matching_record(handler.records, "cleanup-failure")
        cleanup_rendered = (logging.Formatter("%(message)s").format(cleanup_record)
                            if cleanup_record else "")
        report("_run_and_cleanup logs stable context plus the raw traceback",
               bool(cleanup_record)
               and unexpected_raw not in cleanup_record.getMessage()
               and cleanup_record.exc_info is not None
               and unexpected_raw in cleanup_rendered)

        print("\n=== event-loop responsiveness ===")
        await reset_db(engine)
        with tempfile.TemporaryDirectory(prefix="workflow-read-") as workflow_dir:
            workflow_path = Path(workflow_dir) / "workflow.json"
            workflow_path.write_text("{}", encoding="utf-8")
            await seed_task(session_factory, "workflow-read", workflow_file=workflow_path.name)
            original_workflow_dir = settings.WORKFLOW_DIR
            settings.WORKFLOW_DIR = Path(workflow_dir)
            ts.get_provider = lambda engine_name: FakeProvider()
            started = threading.Event()
            release = threading.Event()
            state = {}
            original_read_text = Path.read_text

            def blocking_read_text(path, *args, **kwargs):
                if path == workflow_path:
                    started.set()
                    state["released"] = release.wait(timeout=1.0)
                return original_read_text(path, *args, **kwargs)

            try:
                with patch.object(Path, "read_text", blocking_read_text):
                    await run_blocking_gate(
                        "workflow reads run outside the event loop",
                        lambda: TaskService.execute_task("workflow-read"),
                        started,
                        release,
                        state,
                    )
            finally:
                settings.WORKFLOW_DIR = original_workflow_dir

        fake_db = FakeAssetDb()
        task = SimpleNamespace(id="asset-task", params={})
        template = SimpleNamespace(category="image")
        started = threading.Event()
        release = threading.Event()
        state = {}

        def blocking_save(*args, **kwargs):
            started.set()
            state["released"] = release.wait(timeout=1.0)

        with patch.object(ts, "save_asset_file", blocking_save):
            await run_blocking_gate(
                "FileStore writes run outside the event loop",
                lambda: TaskService._save_asset(
                    fake_db,
                    task,
                    template,
                    FakeProvider(output=b"image-bytes"),
                    {"filename": "image.png"},
                ),
                started,
                release,
                state,
            )

        fake_db = FakeAssetDb()
        started = threading.Event()
        release = threading.Event()
        state = {}

        def blocking_thumbnail(*args, **kwargs):
            started.set()
            state["released"] = release.wait(timeout=1.0)
            return None

        with patch.object(ts, "save_asset_file", lambda *args, **kwargs: None), \
                patch.object(ts, "make_video_thumb", blocking_thumbnail):
            await run_blocking_gate(
                "video thumbnail work runs outside the event loop",
                lambda: TaskService._save_asset(
                    fake_db,
                    task,
                    template,
                    FakeProvider(output=b"video-bytes"),
                    {"filename": "clip.mp4"},
                ),
                started,
                release,
                state,
            )
    finally:
        ts.logger.removeHandler(handler)
        ts.async_session = original_session
        ts.get_provider = original_get_provider
        await engine.dispose()
        Path(temp_db.name).unlink(missing_ok=True)

    print("\n" + ("ALL PASS" if ok_all else "SOME FAILED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
