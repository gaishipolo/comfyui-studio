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

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.uploads as uploads_api
import app.services.task_service as ts
from app.config import settings
from app.engines.base import GenerationProvider, GenerationResult, TaskStatus
from app.models.models import Base, Task, Template
from app.safe_detail import safe_detail
from app.schemas.schemas import AssetUploadRequest
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
    def __init__(
        self,
        submit_error="",
        status_error="",
        upload_error="",
        output=b"data",
        result=None,
    ):
        super().__init__()
        self.submit_error = submit_error
        self.status_error = status_error
        self.upload_error = upload_error
        self.output = output
        self.result = result or GenerationResult(status=TaskStatus.DONE, outputs=[])

    async def submit(self, ctx, params):
        if self.submit_error:
            raise RuntimeError(self.submit_error)
        return "provider-task"

    async def get_status(self, task_id):
        if self.status_error:
            raise RuntimeError(self.status_error)
        return self.result

    async def download_output(self, filename, subfolder="", output_type="output"):
        return self.output

    async def upload_input(self, data, filename):
        if self.upload_error:
            raise NotImplementedError(self.upload_error)
        return filename

    async def health_check(self):
        return True


class FakeAssetDb:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


class FakeUploadDb:
    def __init__(self, asset):
        self.asset = asset

    async def get(self, model, asset_id):
        return self.asset


class OneShotCommitFailureSession(AsyncSession):
    failure_message = ""
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.injected_failure = False
        self.needs_rollback = False
        self.rollback_called = False
        type(self).instances.append(self)

    async def commit(self):
        if not self.injected_failure:
            self.injected_failure = True
            self.needs_rollback = True
            self.expire_all()
            raise RuntimeError(self.failure_message)
        if self.needs_rollback:
            raise RuntimeError("rollback required before error persistence")
        await super().commit()

    async def rollback(self):
        self.rollback_called = True
        await super().rollback()
        self.needs_rollback = False


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
    long_exception = type("X" * 250, (Exception,), {})
    long_type_detail = safe_detail(long_exception())
    report("empty-message exception types are capped at 200 characters",
           len(long_type_detail) <= 200, f"length={len(long_type_detail)}")

    print("\n=== upload error boundary ===")
    acquisition_raw = (
        "unknown engine http://10.0.0.5/private?api_key=UPLOADSECRET "
        "token=UPLOADSECRET"
    )
    with patch.object(
        uploads_api,
        "get_provider",
        side_effect=ValueError(acquisition_raw),
    ):
        acquisition_error = None
        try:
            await uploads_api._forward_to_engine("missing", b"data", "input.png")
        except HTTPException as exc:
            acquisition_error = exc
    acquisition_detail = str(acquisition_error.detail) if acquisition_error else ""
    report("provider acquisition keeps 400 and sanitizes detail",
           acquisition_error is not None
           and acquisition_error.status_code == 400
           and acquisition_detail.startswith("ValueError:")
           and "10.0.0.5" not in acquisition_detail
           and "UPLOADSECRET" not in acquisition_detail,
           acquisition_detail)

    unsupported_raw = "upload unsupported Bearer upload.secret token=UPLOADSECRET"
    with patch.object(
        uploads_api,
        "get_provider",
        return_value=FakeProvider(upload_error=unsupported_raw),
    ):
        unsupported_error = None
        try:
            await uploads_api._forward_to_engine("fake", b"data", "input.png")
        except HTTPException as exc:
            unsupported_error = exc
    unsupported_detail = str(unsupported_error.detail) if unsupported_error else ""
    report("unsupported upload keeps 400 and sanitizes detail",
           unsupported_error is not None
           and unsupported_error.status_code == 400
           and unsupported_detail.startswith("NotImplementedError:")
           and "upload.secret" not in unsupported_detail
           and "UPLOADSECRET" not in unsupported_detail,
           unsupported_detail)

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

        await reset_db(engine)
        acquisition_raw = "provider lookup failed token=RESUMELOOKUPSECRET"
        await seed_task(
            session_factory,
            "resume-provider-failure",
            provider_task_id="provider-existing",
        )

        def fail_provider_lookup(engine_name):
            raise ValueError(acquisition_raw)

        ts.get_provider = fail_provider_lookup
        handler.records.clear()
        escaped = None
        try:
            await TaskService.resume_task("resume-provider-failure")
        except Exception as exc:
            escaped = exc
        task = await load_task(session_factory, "resume-provider-failure")
        report("resume provider acquisition failure is persisted safely",
               escaped is None
               and task.status == "error"
               and task.error_msg.startswith("ValueError:")
               and "RESUMELOOKUPSECRET" not in task.error_msg,
               f"escaped={escaped!r} status={task.status} detail={task.error_msg}")
        check_failure_log(
            "resume provider acquisition log",
            matching_record(handler.records, "resume-provider-failure"),
            "resume-provider-failure",
            acquisition_raw,
        )

        await reset_db(engine)
        setup_raw = "running-state commit failed password=RESUMECOMMITSECRET"
        await seed_task(
            session_factory,
            "resume-setup-failure",
            provider_task_id="provider-existing",
        )
        OneShotCommitFailureSession.failure_message = setup_raw
        OneShotCommitFailureSession.instances.clear()
        failing_session_factory = async_sessionmaker(
            engine,
            class_=OneShotCommitFailureSession,
            expire_on_commit=False,
        )
        ts.get_provider = lambda engine_name: FakeProvider()
        ts.async_session = failing_session_factory
        handler.records.clear()
        escaped = None
        try:
            await TaskService.resume_task("resume-setup-failure")
        except Exception as exc:
            escaped = exc
        finally:
            ts.async_session = session_factory
        task = await load_task(session_factory, "resume-setup-failure")
        failed_session = OneShotCommitFailureSession.instances[-1]
        report("resume setup failure rolls back and persists a safe error",
               escaped is None
               and failed_session.rollback_called
               and task.status == "error"
               and task.error_msg.startswith("RuntimeError:")
               and "RESUMECOMMITSECRET" not in task.error_msg,
               f"escaped={escaped!r} rollback={failed_session.rollback_called} "
               f"status={task.status} detail={task.error_msg}")
        check_failure_log(
            "resume setup failure log",
            matching_record(handler.records, "resume-setup-failure"),
            "resume-setup-failure",
            setup_raw,
        )

        await reset_db(engine)
        terminal_raw = (
            "provider terminal error http://10.0.0.5/result?api_key=RESULTSECRET "
            "token=RESULTSECRET"
        )
        await seed_task(session_factory, "terminal-provider-error")
        ts.get_provider = lambda engine_name: FakeProvider(result=GenerationResult(
            status=TaskStatus.ERROR,
            error=terminal_raw,
        ))
        await TaskService.execute_task("terminal-provider-error")
        task = await load_task(session_factory, "terminal-provider-error")
        report("terminal Provider errors are sanitized before persistence",
               task.status == "error"
               and task.error_msg.startswith("RuntimeError:")
               and "10.0.0.5" not in task.error_msg
               and "RESULTSECRET" not in task.error_msg
               and len(task.error_msg) <= 200,
               task.error_msg)

        await reset_db(engine)
        await seed_task(session_factory, "terminal-provider-fallback")
        ts.get_provider = lambda engine_name: FakeProvider(result=GenerationResult(
            status=TaskStatus.ERROR,
            error="",
        ))
        await TaskService.execute_task("terminal-provider-fallback")
        task = await load_task(session_factory, "terminal-provider-fallback")
        report("empty terminal Provider errors keep the plain fallback",
               task.error_msg == "Generation failed", task.error_msg)

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

        asset = SimpleNamespace(
            task_id="upload-owner-task",
            is_shared=False,
            mime_type="image/png",
            file_path="upload-owner-task/input.png",
            file_name="input.png",
            file_size=16,
        )
        upload_db = FakeUploadDb(asset)
        upload_user = SimpleNamespace(id="owner", role="member")
        started = threading.Event()
        release = threading.Event()
        state = {}

        def blocking_asset_read(file_path):
            started.set()
            state["released"] = release.wait(timeout=1.0)
            return b"asset-bytes"

        async def fake_owner_map(db, task_ids):
            return {"upload-owner-task": "owner"}

        async def fake_forward(engine_name, data, filename):
            return "engine-input.png"

        with patch.object(uploads_api, "read_asset_bytes", blocking_asset_read), \
                patch.object(uploads_api, "task_owner_map", fake_owner_map), \
                patch.object(uploads_api, "_forward_to_engine", fake_forward):
            await run_blocking_gate(
                "from-asset reads run outside the event loop",
                lambda: uploads_api.upload_from_asset(
                    body=AssetUploadRequest(asset_id="asset-id", engine="fake"),
                    user=upload_user,
                    db=upload_db,
                ),
                started,
                release,
                state,
            )

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
                "source FileStore writes run outside the event loop",
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
                "video thumbnail generation runs outside the event loop",
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

        fake_db = FakeAssetDb()
        started = threading.Event()
        release = threading.Event()
        state = {}

        def blocking_thumbnail_save(key, data, content_type="application/octet-stream"):
            if content_type == "image/jpeg":
                started.set()
                state["released"] = release.wait(timeout=1.0)

        with patch.object(ts, "save_asset_file", blocking_thumbnail_save), \
                patch.object(ts, "make_video_thumb", lambda *args: b"thumbnail-bytes"):
            await run_blocking_gate(
                "video thumbnail FileStore writes run outside the event loop",
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
