# -*- coding: utf-8 -*-
"""B5/B6/B11 verification with real ComfyUI and deterministic cleanup."""

import asyncio
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base
from app.engines.base import GenerationResult, TaskStatus
from app.filestore import asset_exists
from app.models.models import Task, Template
from app.services import task_service as task_service_module
from app.services.task_service import TaskService
from app.thumbs import thumb_key
from provider_test_utils import lease_acquirer
from test_env import SERVICE_BASE_URL


BASE = SERVICE_BASE_URL
TERMINAL_STATUSES = {"done", "error", "cancelled"}
TEMPLATE_ID_ENV = "COMFYUI_STUDIO_B5_B6_TEMPLATE_ID"
EXPECTED_WORKFLOW_FILE = "image_flux2_klein_text_to_image.json"
EXPECTED_PARAM_MAPPING = {
    "prompt": "76.inputs.value",
    "width": "103.inputs.value",
    "height": "106.inputs.value",
}
EXPECTED_OUTPUT_NODES = ["108"]


def req(method, path, token=None, json_body=None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def req_bytes(method, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return (
                response.status,
                response.read(),
                {key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.read(),
            {key.lower(): value for key, value in exc.headers.items()},
        )


def required_template_id() -> str:
    template_id = os.environ.get(TEMPLATE_ID_ENV, "").strip()
    if not template_id:
        raise ValueError(f"必须显式设置 {TEMPLATE_ID_ENV}")
    return template_id


def returned_task_ids(payload) -> list[str]:
    if not isinstance(payload, list):
        return []
    return [
        item["id"]
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]


class _B5FakeGenericProvider:
    def __init__(self):
        self.submit_calls: list[tuple[dict, dict]] = []
        self.status_calls: list[str] = []
        self.last_request = None

    async def submit(self, ctx: dict, params: dict) -> str:
        self.submit_calls.append((dict(ctx), dict(params)))
        self.last_request = {"kind": "B5 in-process fake", "params": dict(params)}
        return "b5-in-process-prompt"

    async def get_status(self, task_id: str) -> GenerationResult:
        self.status_calls.append(task_id)
        return GenerationResult(
            status=TaskStatus.ERROR,
            error="B5 synthetic generic terminal result",
        )


async def verify_b5_in_process(check) -> bool:
    provider = _B5FakeGenericProvider()
    original_session_factory = task_service_module.async_session
    original_acquire_provider = task_service_module.acquire_provider
    conditions: list[bool] = []
    database_removed = False

    def verify(label, condition, detail=""):
        conditions.append(bool(condition))
        check(label, condition, detail)

    with tempfile.TemporaryDirectory(prefix="task7-b5-in-process-") as raw_dir:
        db_path = Path(raw_dir) / "b5.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        template_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex

        def fake_get_provider(engine_name: str):
            verify(
                "B5 in-process 只请求 generic_api Provider",
                engine_name == "generic_api",
                repr(engine_name),
            )
            return provider

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as db:
                db.add(
                    Template(
                        id=template_id,
                        name="B5 in-process generic fixture",
                        category="image",
                        engine="generic_api",
                        workflow_file="",
                        input_schema={"fields": []},
                        param_mapping={},
                        output_config={},
                        is_active=False,
                    )
                )
                db.add(
                    Task(
                        id=task_id,
                        template_id=template_id,
                        created_by="b5-in-process",
                        batch_id=uuid.uuid4().hex,
                        status="pending",
                        progress=0.0,
                        params={"prompt": "B5 in-process"},
                        result={},
                        error_msg="",
                    )
                )
                await db.commit()

            task_service_module.async_session = session_factory
            task_service_module.acquire_provider = lease_acquirer(fake_get_provider)
            await TaskService.execute_task(task_id)

            async with session_factory() as db:
                task = await db.get(Task, task_id)
                template = await db.get(Template, template_id)
                ctx = provider.submit_calls[0][0] if provider.submit_calls else {}
                params = provider.submit_calls[0][1] if provider.submit_calls else {}
                error = task.error_msg if task else ""
                verify("B5 in-process 模板 workflow_file 为空", template.workflow_file == "")
                verify("B5 in-process submit 被调用一次", len(provider.submit_calls) == 1)
                verify(
                    "B5 in-process submit context 正确且 workflow/mapping 为空",
                    ctx
                    == {
                        "_type": "image",
                        "_capability": "image",
                        "_template_name": "B5 in-process generic fixture",
                        "_workflow": {},
                        "_param_mapping": {},
                    },
                    repr(ctx),
                )
                verify("B5 in-process 参数到达 Provider", params == {"prompt": "B5 in-process"})
                verify("B5 in-process 状态查询使用 submit ID", provider.status_calls == ["b5-in-process-prompt"])
                verify(
                    "B5 in-process synthetic prompt ID 已持久化",
                    task is not None and task.provider_task_id == "b5-in-process-prompt",
                    task.provider_task_id if task else "missing task",
                )
                verify(
                    "B5 in-process 终态失败来自 fake result",
                    task is not None
                    and task.status == "error"
                    and "B5 synthetic generic terminal result" in error,
                    error,
                )
                verify(
                    "B5 in-process 失败与 workflow/IsADirectory 无关",
                    "workflow" not in error.lower() and "isadirectory" not in error.lower(),
                    error,
                )
                verify(
                    "B5 in-process 请求快照持久化",
                    (task.result or {}).get("_request", {}).get("kind")
                    == "B5 in-process fake",
                    repr(task.result if task else None),
                )
        except Exception as exc:  # noqa: BLE001 - aggregate verifier reports the failure
            verify("B5 in-process 执行不抛异常", False, f"{type(exc).__name__}: {exc}")
        finally:
            task_service_module.async_session = original_session_factory
            task_service_module.acquire_provider = original_acquire_provider
            await engine.dispose()
            for candidate in (
                db_path,
                Path(f"{db_path}-wal"),
                Path(f"{db_path}-shm"),
            ):
                candidate.unlink(missing_ok=True)
            database_removed = not any(
                candidate.exists()
                for candidate in (
                    db_path,
                    Path(f"{db_path}-wal"),
                    Path(f"{db_path}-shm"),
                )
            )

    verify("B5 in-process 临时 SQLite 已删除", database_removed)
    return bool(conditions) and all(conditions)


def main() -> int:
    ok = True

    def check(label, condition, detail=""):
        nonlocal ok
        ok &= bool(condition)
        print(f"{'OK  ' if condition else 'FAIL'} {label}" + (f" {detail}" if detail else ""))

    if not asyncio.run(verify_b5_in_process(check)):
        return 1

    try:
        expected_template_id = required_template_id()
    except ValueError as exc:
        print(f"安全检查失败: {exc}")
        return 1

    code, login = req(
        "POST",
        "/api/auth/login",
        json_body={"username": "admin", "password": "admin123"},
    )
    if code != 200 or not isinstance(login, dict) or not login.get("access_token"):
        print(f"登录失败: HTTP {code} {login}")
        return 1
    token = login["access_token"]

    created_task_ids: list[str] = []
    generated_assets: dict[str, list[dict]] = {}
    try:
        code, templates = req(
            "GET", "/api/templates?include_inactive=true", token=token
        )
        exact_matches = [
            item
            for item in templates
            if isinstance(item, dict) and item.get("id") == expected_template_id
        ] if code == 200 and isinstance(templates, list) else []
        check(
            "B6 按显式 ID 找到唯一模板",
            len(exact_matches) == 1,
            f"HTTP {code} id={expected_template_id!r} matches={len(exact_matches)}",
        )
        flux = exact_matches[0] if len(exact_matches) == 1 else None
        fixture_checks = {
            "engine=comfyui": bool(flux) and flux.get("engine") == "comfyui",
            f"workflow={EXPECTED_WORKFLOW_FILE}": bool(flux)
            and flux.get("workflow_file") == EXPECTED_WORKFLOW_FILE,
            "exact parameter mappings": bool(flux)
            and flux.get("param_mapping") == EXPECTED_PARAM_MAPPING,
            "output node 108": bool(flux)
            and (flux.get("output_config") or {}).get("nodes") == EXPECTED_OUTPUT_NODES,
        }
        for label, condition in fixture_checks.items():
            check(f"B6 fixture {label}", condition)
        if flux is None or not all(fixture_checks.values()):
            return 1

        prompt_uuid = str(uuid.uuid4())
        code, tasks = req(
            "POST",
            "/api/tasks",
            token=token,
            json_body={
                "template_id": flux["id"],
                "params": {
                    "prompt": f"Task 7 isolated regression image {prompt_uuid}",
                    "width": 768,
                    "height": 768,
                },
                "batch_count": 1,
            },
        )
        b6_task_ids = returned_task_ids(tasks)
        created_task_ids.extend(b6_task_ids)
        b6_response_ok = (
            code in {200, 201}
            and isinstance(tasks, list)
            and len(tasks) == 1
            and len(b6_task_ids) == 1
        )
        check(
            "B6 提交真实 ComfyUI 任务且只返回 1 个任务",
            b6_response_ok,
            f"HTTP {code} returned={len(tasks) if isinstance(tasks, list) else 'non-list'} "
            f"owned_ids={b6_task_ids}",
        )
        if not b6_response_ok:
            return 1
        b6_task_id = b6_task_ids[0]
        created_by = tasks[0].get("created_by", "")
        check("B11 提交任务写入 created_by", bool(created_by), repr(created_by))

        progress_seen = set()
        status_seen = set()
        task = {}
        for _ in range(90):
            code, task = req("GET", f"/api/tasks/{b6_task_id}", token=token)
            if code != 200 or not isinstance(task, dict):
                break
            progress_seen.add(task.get("progress"))
            status_seen.add(task.get("status"))
            if task.get("status") in TERMINAL_STATUSES:
                break
            time.sleep(2)

        final_status = task.get("status")
        reached_terminal = final_status in TERMINAL_STATUSES
        middle_progress = {value for value in progress_seen if value is not None and 0 < value < 100}
        progress_ok = final_status == "done" and (bool(middle_progress) or 100 in progress_seen)
        printable_statuses = sorted(value for value in status_seen if value is not None)
        printable_progress = sorted(value for value in progress_seen if value is not None)
        print(f"      状态轨迹: {printable_statuses}  进度轨迹: {printable_progress}")
        check("B6 轮询在时限内到达终态", reached_terminal, f"status={final_status}")
        check("B6 真实任务必须完成为 done", final_status == "done", f"status={final_status}")
        check(
            "B6 UUID 提示词持久化",
            prompt_uuid in str((task.get("params") or {}).get("prompt", "")),
            prompt_uuid,
        )
        check("B6 进度回写", progress_ok, f"中途进度={sorted(middle_progress) or '无'}")

        code, assets = (
            req("GET", f"/api/tasks/{b6_task_id}/assets", token=token)
            if final_status == "done"
            else (200, [])
        )
        print(
            f"      任务最终状态: {final_status} "
            f"素材数: {len(assets) if code == 200 and isinstance(assets, list) else 0}"
        )
        check(
            "B6 done 任务至少产生 1 个素材",
            code == 200 and isinstance(assets, list) and len(assets) >= 1,
            f"HTTP {code}",
        )
        if code == 200 and isinstance(assets, list) and assets:
            generated_assets[b6_task_id] = assets
            asset = assets[0]
            encoded_name = urllib.parse.quote(asset["file_name"])
            media_path = f"/api/tasks/{b6_task_id}/assets/{encoded_name}"
            source_code, source_bytes, source_headers = req_bytes(
                "GET", media_path, token=token
            )
            source_size = (0, 0)
            source_format = ""
            try:
                with Image.open(io.BytesIO(source_bytes)) as source_image:
                    source_size = source_image.size
                    source_format = source_image.format or ""
            except Exception:
                pass
            check(
                "B6 原图鉴权下载成功",
                source_code == 200
                and bool(source_bytes)
                and source_headers.get("content-type", "").startswith("image/")
                and bool(source_format),
                f"HTTP {source_code} format={source_format} size={source_size} bytes={len(source_bytes)}",
            )

            thumb_code, thumb_bytes, thumb_headers = req_bytes(
                "GET", media_path + "/thumb", token=token
            )
            thumb_size = (0, 0)
            thumb_format = ""
            try:
                with Image.open(io.BytesIO(thumb_bytes)) as thumb_image:
                    thumb_size = thumb_image.size
                    thumb_format = thumb_image.format or ""
            except Exception:
                pass
            persistent_key = thumb_key(b6_task_id, asset["file_name"])
            smaller_when_meaningful = (
                max(source_size or (0, 0)) <= 400 or len(thumb_bytes) < len(source_bytes)
            )
            check(
                "B6 持久 JPEG 缩略图不超过 400 边长且合理变小",
                thumb_code == 200
                and thumb_format == "JPEG"
                and thumb_headers.get("content-type", "").startswith("image/jpeg")
                and max(thumb_size or (0, 0)) <= 400
                and smaller_when_meaningful
                and asset_exists(persistent_key),
                (
                    f"HTTP {thumb_code} size={thumb_size} bytes={len(thumb_bytes)} "
                    f"source_bytes={len(source_bytes)} key={persistent_key}"
                ),
            )
            print(
                f"      真实生成 ID: task={b6_task_id} "
                f"provider={task.get('provider_task_id', '')} prompt_uuid={prompt_uuid}"
            )
    finally:
        for task_id in reversed(created_task_ids):
            code, task = req("GET", f"/api/tasks/{task_id}", token=token)
            status = task.get("status") if code == 200 and isinstance(task, dict) else None
            if status not in TERMINAL_STATUSES:
                cancel_code, _ = req("POST", f"/api/tasks/{task_id}/cancel", token=token)
                check(f"取消未终态任务 {task_id[:8]}", cancel_code == 200, f"HTTP {cancel_code}")
            delete_code, body = 409, {}
            for _ in range(20):
                delete_code, body = req("DELETE", f"/api/tasks/{task_id}", token=token)
                if delete_code != 409:
                    break
                time.sleep(0.25)
            check(
                f"清理任务 {task_id[:8]}",
                delete_code in {200, 404},
                f"HTTP {delete_code} {body}",
            )
            after_code, _ = req("GET", f"/api/tasks/{task_id}", token=token)
            check(f"任务 API 已清理 {task_id[:8]}", after_code == 404, f"HTTP {after_code}")
            for asset in generated_assets.get(task_id, []):
                encoded_name = urllib.parse.quote(asset["file_name"])
                media_path = f"/api/tasks/{task_id}/assets/{encoded_name}"
                source_code, _source_body, _ = req_bytes("GET", media_path, token=token)
                thumb_code, _thumb_body, _ = req_bytes("GET", media_path + "/thumb", token=token)
                check(
                    f"素材 API 已清理 {asset['file_name']}",
                    source_code == 404 and thumb_code == 404,
                    f"source={source_code} thumb={thumb_code}",
                )
                check(
                    f"素材文件已清理 {asset['file_name']}",
                    not asset_exists(asset["file_path"])
                    and not asset_exists(thumb_key(task_id, asset["file_name"])),
                    asset["file_path"],
                )

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
