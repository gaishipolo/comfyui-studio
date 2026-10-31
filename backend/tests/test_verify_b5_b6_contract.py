"""Deterministic in-process B5 and fake-service B6 contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


BACKEND = Path(__file__).resolve().parents[1]
VERIFIER = BACKEND / "tests" / "verify_b5_b6.py"
TEMPLATE_ID = "flux-template"


def valid_template() -> dict:
    return {
        "id": TEMPLATE_ID,
        "name": "Flux2 fake fixture",
        "engine": "comfyui",
        "workflow_file": "image_flux2_klein_text_to_image.json",
        "param_mapping": {
            "prompt": "76.inputs.value",
            "width": "103.inputs.value",
            "height": "106.inputs.value",
        },
        "output_config": {"nodes": ["108"]},
    }


class FakeB6Service:
    def __init__(self, *, b6_count: int = 1, fixture=None):
        self.b6_count = b6_count
        self.fixture = fixture or valid_template()
        self.login_posts = 0
        self.template_gets = 0
        self.b5_template_posts = 0
        self.generic_task_posts = 0
        self.b6_posts = 0
        self.cancelled: list[str] = []
        self.deleted: list[str] = []
        self.tasks: dict[str, str] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def send_json(self, status: int, value) -> None:
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def read_json(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/api/templates":
                    outer.template_gets += 1
                    self.send_json(200, [outer.fixture])
                    return
                if path.startswith("/api/tasks/") and path.endswith("/assets"):
                    self.send_json(200, [])
                    return
                if path.startswith("/api/tasks/"):
                    task_id = path.rsplit("/", 1)[-1]
                    if task_id in outer.deleted:
                        self.send_json(404, {"detail": "Task not found"})
                        return
                    status = outer.tasks.get(task_id)
                    if status is None:
                        self.send_json(404, {"detail": "Task not found"})
                        return
                    self.send_json(
                        200,
                        {
                            "id": task_id,
                            "status": status,
                            "progress": 0.0,
                            "params": {"prompt": "fake B6"},
                            "provider_task_id": "fake-provider-id",
                            "error_msg": "fake B6 terminal failure" if status == "error" else "",
                        },
                    )
                    return
                self.send_json(404, {"detail": "not found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                body = self.read_json()
                if path == "/api/auth/login":
                    outer.login_posts += 1
                    self.send_json(200, {"access_token": "fake-admin-token"})
                    return
                if path == "/api/templates":
                    outer.b5_template_posts += 1
                    self.send_json(500, {"detail": "B5 must not use service templates"})
                    return
                if path == "/api/tasks":
                    if body.get("template_id") != TEMPLATE_ID:
                        outer.generic_task_posts += 1
                        self.send_json(500, {"detail": "B5 must not use service tasks"})
                        return
                    outer.b6_posts += 1
                    ids = [f"b6-task-{index}" for index in range(outer.b6_count)]
                    status = "running" if outer.b6_count > 1 else "error"
                    for task_id in ids:
                        outer.tasks[task_id] = status
                    self.send_json(
                        200,
                        [{"id": task_id, "created_by": "fake-admin-id"} for task_id in ids],
                    )
                    return
                if path.endswith("/cancel"):
                    task_id = path.split("/")[-2]
                    outer.cancelled.append(task_id)
                    outer.tasks[task_id] = "cancelled"
                    self.send_json(200, {"id": task_id, "status": "cancelled"})
                    return
                self.send_json(404, {"detail": "not found"})

            def do_DELETE(self):
                path = urlsplit(self.path).path
                if path.startswith("/api/tasks/"):
                    task_id = path.rsplit("/", 1)[-1]
                    outer.deleted.append(task_id)
                    self.send_json(200, {"id": task_id, "message": "Task deleted"})
                    return
                self.send_json(404, {"detail": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def run_verifier(
    fake: FakeB6Service,
    *,
    template_id: str | None = TEMPLATE_ID,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="task7-b6-contract-") as raw_dir:
        root = Path(raw_dir)
        env = os.environ.copy()
        env.pop("COMFYUI_STUDIO_TEST_GENERIC_BASE_URL", None)
        env.pop("GENERIC_API_BASE_URL", None)
        if template_id is None:
            env.pop("COMFYUI_STUDIO_B5_B6_TEMPLATE_ID", None)
        else:
            env["COMFYUI_STUDIO_B5_B6_TEMPLATE_ID"] = template_id
        env.update(
            {
                "COMFYUI_STUDIO_TEST_BASE_URL": fake.origin,
                "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'unused-service.db'}",
                "STORAGE_BACKEND": "local",
                "STORAGE_DIR": str(root / "unused-storage"),
                "WORKFLOW_DIR": str(root / "unused-workflows"),
                "SECRET_KEY": "task7-b6-contract-secret",
            }
        )
        return subprocess.run(
            [sys.executable, "-u", "-X", "utf8", str(VERIFIER)],
            cwd=BACKEND,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )


def load_verifier_module():
    name = f"verify_b5_b6_contract_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifyB5B6ContractTests(unittest.TestCase):
    def test_b5_runs_in_process_without_service(self):
        with tempfile.TemporaryDirectory(prefix="task7-b5-import-") as raw_dir:
            previous = {
                name: os.environ.get(name)
                for name in ("DATABASE_URL", "STORAGE_DIR", "WORKFLOW_DIR")
            }
            os.environ.update(
                {
                    "DATABASE_URL": f"sqlite+aiosqlite:///{Path(raw_dir) / 'unused.db'}",
                    "STORAGE_DIR": str(Path(raw_dir) / "unused-storage"),
                    "WORKFLOW_DIR": str(Path(raw_dir) / "unused-workflows"),
                }
            )
            try:
                module = load_verifier_module()
                self.assertTrue(
                    hasattr(module, "verify_b5_in_process"),
                    "verify_b5_in_process contract is missing",
                )
                reports = []

                def report(label, condition, detail=""):
                    reports.append((label, bool(condition), detail))

                self.assertTrue(asyncio.run(module.verify_b5_in_process(report)))
                self.assertTrue(reports)
                self.assertTrue(
                    all(condition for _label, condition, _detail in reports),
                    reports,
                )
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_main_uses_no_b5_service_routes(self):
        with FakeB6Service() as fake:
            result = run_verifier(fake)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("B5 in-process", result.stdout)
        self.assertEqual(fake.b5_template_posts, 0)
        self.assertEqual(fake.generic_task_posts, 0)
        self.assertEqual(fake.b6_posts, 1)

    def test_extra_b6_task_ids_are_all_cancelled_and_deleted(self):
        with FakeB6Service(b6_count=2) as fake:
            result = run_verifier(fake)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b5_template_posts, 0)
        self.assertEqual(fake.generic_task_posts, 0)
        self.assertEqual(fake.b6_posts, 1)
        self.assertEqual(set(fake.cancelled), {"b6-task-0", "b6-task-1"})
        self.assertEqual(set(fake.deleted), {"b6-task-0", "b6-task-1"})

    def test_fixture_mapping_mismatch_fails_before_b6_submission(self):
        fixture = valid_template()
        fixture["param_mapping"] = {**fixture["param_mapping"], "width": "999.inputs.value"}
        with FakeB6Service(fixture=fixture) as fake:
            result = run_verifier(fake)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.template_gets, 1)
        self.assertEqual(fake.b6_posts, 0)
        self.assertEqual(fake.b5_template_posts, 0)
        self.assertEqual(fake.generic_task_posts, 0)

    def test_name_match_cannot_replace_exact_template_id(self):
        fixture = valid_template()
        fixture["id"] = "different-template-id"
        with FakeB6Service(fixture=fixture) as fake:
            result = run_verifier(fake)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.template_gets, 1)
        self.assertEqual(fake.b6_posts, 0)
        self.assertEqual(fake.b5_template_posts, 0)
        self.assertEqual(fake.generic_task_posts, 0)

    def test_missing_explicit_template_id_refuses_before_service(self):
        with FakeB6Service() as fake:
            result = run_verifier(fake, template_id=None)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.login_posts, 0)
        self.assertEqual(fake.template_gets, 0)
        self.assertEqual(fake.b6_posts, 0)
        self.assertEqual(fake.b5_template_posts, 0)
        self.assertEqual(fake.generic_task_posts, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
