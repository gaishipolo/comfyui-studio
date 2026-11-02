"""I14 回归：ComfyUI /prompt 失败必须保留 error / node_errors 诊断。"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.comfyui import ComfyUIProvider  # noqa: E402


class FakeProgress:
    async def ensure_running(self) -> None:
        return None

    def record_plan(self, _prompt_id: str, _nodes: int) -> None:
        return None


async def test_submit_error_preserves_error_and_node_errors() -> None:
    body = {
        "error": {
            "type": "prompt_outputs_failed_validation",
            "message": "Prompt outputs failed validation",
        },
        "node_errors": {
            "76": {
                "class_type": "PrimitiveString",
                "errors": [{
                    "type": "value_not_in_list",
                    "message": "Value not in list",
                    "details": "prompt: bad choice",
                }],
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/prompt"
        return httpx.Response(400, json=body, request=request)

    provider = ComfyUIProvider(base_url="http://comfy.test")
    provider._progress = FakeProgress()
    provider._client = httpx.AsyncClient(
        base_url=provider.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        try:
            await provider.submit(
                {
                    "_workflow": {"76": {"inputs": {"value": "x"}}},
                    "_param_mapping": {},
                    "_template_name": "fixture",
                },
                {},
            )
        except RuntimeError as exc:
            detail = str(exc)
            assert "HTTP 400" in detail, detail
            assert "Prompt outputs failed validation" in detail, detail
            assert "node_errors" in detail, detail
            assert "Value not in list" in detail, detail
            assert "prompt: bad choice" in detail, detail
        else:
            raise AssertionError("400 /prompt did not raise RuntimeError")
    finally:
        await provider._client.aclose()


async def main() -> int:
    tests = [test_submit_error_preserves_error_and_node_errors]
    failures: list[str] = []
    for test in tests:
        try:
            await test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
