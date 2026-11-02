"""M11 回归：工作流上传必须在解析前做 10MB 有界读取。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import templates as templates_api  # noqa: E402


class OversizedUpload:
    def __init__(self):
        self.requested = None
        self.filename = "large.json"

    async def read(self, size=-1):
        self.requested = size
        return b"x" * (10 * 1024 * 1024 + 1)


async def test_workflow_upload_is_bounded_before_json_parse() -> None:
    upload = OversizedUpload()
    try:
        await templates_api.upload_workflow(
            file=upload,
            user=SimpleNamespace(id="admin", role="admin"),
        )
    except HTTPException as exc:
        assert exc.status_code == 413, exc.status_code
    else:
        raise AssertionError("oversized workflow upload was accepted")
    assert upload.requested == 10 * 1024 * 1024 + 1, upload.requested


if __name__ == "__main__":
    try:
        asyncio.run(test_workflow_upload_is_bounded_before_json_parse())
    except BaseException as exc:
        print(f"[FAIL] test_workflow_upload_is_bounded_before_json_parse: {type(exc).__name__}: {exc}")
        print("\nRESULT: 0 passed / 1 failed")
        raise SystemExit(1)
    print("[PASS] test_workflow_upload_is_bounded_before_json_parse")
    print("\nRESULT: 1 passed / 0 failed")
