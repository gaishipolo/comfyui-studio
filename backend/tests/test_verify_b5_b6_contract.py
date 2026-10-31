"""Deterministic safety and cleanup contracts for verify_b5_b6.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
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


class FakeBackend:
    def __init__(self, *, b5_count: int = 1, b6_count: int = 1, fixture=None):
        self.b5_count = b5_count
        self.b6_count = b6_count
        self.fixture = fixture or valid_template()
        self.b5_posts = 0
        self.b6_posts = 0
        self.cancelled: list[str] = []
        self.deleted: list[str] = []
        self.deleted_templates: list[str] = []
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
                if path == "/api/engines":
                    self.send_json(
                        200,
                        [
                            {"name": "comfyui", "status": "connected"},
                            {"name": "generic_api", "status": "disconnected"},
                        ],
                    )
                    return
                if path == "/api/templates":
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
                            "params": {"prompt": "fake"},
                            "provider_task_id": "fake-provider-id",
                            "error_msg": "fake generic failure" if status == "error" else "",
                        },
                    )
                    return
                self.send_json(404, {"detail": "not found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                body = self.read_json()
                if path == "/api/auth/login":
                    self.send_json(200, {"access_token": "fake-admin-token"})
                    return
                if path == "/api/templates":
                    self.send_json(200, {"id": "b5-template", **body})
                    return
                if path == "/api/tasks":
                    template_id = body.get("template_id")
                    if template_id == "b5-template":
                        outer.b5_posts += 1
                        ids = [f"b5-task-{index}" for index in range(outer.b5_count)]
                        status = "running" if outer.b5_count > 1 else "error"
                    else:
                        outer.b6_posts += 1
                        ids = [f"b6-task-{index}" for index in range(outer.b6_count)]
                        status = "running" if outer.b6_count > 1 else "error"
                    for task_id in ids:
                        outer.tasks[task_id] = status
                    self.send_json(
                        200,
                        [
                            {
                                "id": task_id,
                                "created_by": "fake-admin-id",
                            }
                            for task_id in ids
                        ],
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
                if path.startswith("/api/templates/"):
                    template_id = path.rsplit("/", 1)[-1]
                    outer.deleted_templates.append(template_id)
                    self.send_json(200, {"message": "Template deleted"})
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


def unused_loopback_origin() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"


def run_verifier(fake: FakeBackend, generic_origin: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="task7-b5-contract-") as raw_dir:
        root = Path(raw_dir)
        env = os.environ.copy()
        env.update(
            {
                "COMFYUI_STUDIO_TEST_BASE_URL": fake.origin,
                "COMFYUI_STUDIO_B5_B6_TEMPLATE_ID": TEMPLATE_ID,
                "COMFYUI_STUDIO_TEST_GENERIC_BASE_URL": generic_origin,
                "GENERIC_API_BASE_URL": generic_origin,
                "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'workbench.db'}",
                "STORAGE_BACKEND": "local",
                "STORAGE_DIR": str(root / "storage"),
                "WORKFLOW_DIR": str(root / "workflows"),
                "SECRET_KEY": "task7-b5-contract-secret",
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


class VerifyB5B6ContractTests(unittest.TestCase):
    def test_extra_b5_task_ids_are_all_cancelled_and_deleted(self):
        with FakeBackend(b5_count=2) as fake:
            result = run_verifier(fake, unused_loopback_origin())

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b6_posts, 0, result.stdout + result.stderr)
        self.assertEqual(set(fake.cancelled), {"b5-task-0", "b5-task-1"})
        self.assertEqual(set(fake.deleted), {"b5-task-0", "b5-task-1"})

    def test_extra_b6_task_ids_are_all_deleted(self):
        with FakeBackend(b6_count=2) as fake:
            result = run_verifier(fake, unused_loopback_origin())

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b5_posts, 1)
        self.assertEqual(fake.b6_posts, 1)
        self.assertTrue({"b6-task-0", "b6-task-1"}.issubset(set(fake.cancelled)))
        self.assertTrue({"b6-task-0", "b6-task-1"}.issubset(set(fake.deleted)))

    def test_fixture_mismatch_fails_before_any_task_submission(self):
        fixture = valid_template()
        fixture["param_mapping"] = {**fixture["param_mapping"], "width": "999.inputs.value"}
        with FakeBackend(fixture=fixture) as fake:
            result = run_verifier(fake, unused_loopback_origin())

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b5_posts, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b6_posts, 0, result.stdout + result.stderr)

    def test_name_match_cannot_replace_exact_template_id(self):
        fixture = valid_template()
        fixture["id"] = "different-template-id"
        with FakeBackend(fixture=fixture) as fake:
            result = run_verifier(fake, unused_loopback_origin())

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b5_posts, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b6_posts, 0, result.stdout + result.stderr)

    def test_non_loopback_generic_origin_refuses_before_b5(self):
        with FakeBackend() as fake:
            result = run_verifier(fake, "https://example.com:443")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b5_posts, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b6_posts, 0, result.stdout + result.stderr)

    def test_reachable_loopback_generic_origin_refuses_before_b5(self):
        with FakeBackend() as fake:
            result = run_verifier(fake, fake.origin)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b5_posts, 0, result.stdout + result.stderr)
        self.assertEqual(fake.b6_posts, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
