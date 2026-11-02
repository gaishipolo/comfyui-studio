"""Focused behavior checks for low-risk review cleanup."""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import assets as assets_api  # noqa: E402
from app.api import uploads as uploads_api  # noqa: E402
from app.engines.base import TaskStatus  # noqa: E402
from app.engines.generic_api import GenericAPIProvider  # noqa: E402
from app.models.models import Asset  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402
from app.schemas.auth_schemas import LoginRequest, RegisterRequest, UserUpdateRequest  # noqa: E402
from app.schemas.schemas import AssetResponse, TemplateCreate  # noqa: E402


class ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class TagsDB:
    async def execute(self, _query):
        return ScalarRows([[" Foo ", "foo", "BAR"], ["bar", "Baz"]])


class TagAssetDB:
    def __init__(self, tags):
        self.asset = Asset(
            id="asset",
            task_id="task",
            file_path="task/file.png",
            file_name="file.png",
            tags=list(tags),
        )
        self.commits = 0

    async def get(self, _model, _asset_id):
        return self.asset

    async def commit(self):
        self.commits += 1


def test_asset_response_keeps_relative_filestore_key() -> None:
    assert "file_path" in AssetResponse.model_fields


def test_tag_counts_trim_and_merge_case_variants() -> None:
    user = SimpleNamespace(id="admin", role="admin")
    rows = asyncio.run(assets_api.list_tags(user=user, db=TagsDB()))
    by_key = {row["name"].casefold(): row for row in rows}
    assert by_key["foo"]["count"] == 2, rows
    assert by_key["bar"]["count"] == 2, rows
    assert by_key["baz"]["count"] == 1, rows


def test_upload_fallback_filename_uses_allowed_extension() -> None:
    assert uploads_api._fallback_filename("image").endswith(".png")
    assert uploads_api._fallback_filename("audio").endswith(".mp3")
    assert uploads_api._fallback_filename("video").endswith(".mp4")


def test_generic_status_404_is_terminal_error() -> None:
    provider = GenericAPIProvider({})

    class Response:
        status_code = 404

    result = provider._status_http_error("missing", Response())
    assert result.status == TaskStatus.ERROR, result
    assert "not found" in result.error.lower(), result.error


def test_generic_progress_is_validated_and_clamped() -> None:
    normalize = GenericAPIProvider._normalize_progress
    assert normalize("75.5") == 75.5
    assert normalize(-4) == 0.0
    assert normalize(120) == 100.0
    assert normalize("not-a-number") == 50.0
    assert normalize(None) == 50.0
    assert normalize(float("nan")) == 50.0


def _max_length(field) -> int | None:
    for meta in getattr(field, "metadata", []):
        if type(meta).__name__ == "MaxLen":
            return meta.max_length
    return None


def test_tag_query_parameters_are_length_bounded() -> None:
    list_tag = inspect.signature(assets_api.list_assets).parameters["tag"].default
    add_tag = inspect.signature(assets_api.add_tag).parameters["tag"].default
    assert _max_length(list_tag) == 64
    assert _max_length(add_tag) == 64


def test_remove_tag_is_case_insensitive_and_rejects_long_values() -> None:
    user = SimpleNamespace(id="owner", role="member")

    async def owner_of(_db, _asset):
        return "owner"

    db = TagAssetDB(["Foo", "Bar"])
    with patch.object(assets_api, "_owner_of", owner_of):
        result = asyncio.run(assets_api.remove_tag("asset", "foo", user=user, db=db))
    assert result == {"tags": ["Bar"]}, result
    assert db.commits == 1

    with patch.object(assets_api, "_owner_of", owner_of):
        try:
            asyncio.run(assets_api.remove_tag("asset", "x" * 65, user=user, db=db))
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError("overlong tag was accepted")


def test_schema_strings_match_database_column_widths() -> None:
    cases = [
        (RegisterRequest, {"username": "user", "password": "secret1", "display_name": "x" * 65}),
        (LoginRequest, {"username": "x" * 65, "password": "secret1"}),
        (LoginRequest, {"username": "user", "password": "x" * 129}),
        (UserUpdateRequest, {"display_name": "x" * 65}),
        (TemplateCreate, {"name": "x" * 129}),
    ]
    for model, payload in cases:
        try:
            model.model_validate(payload)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"{model.__name__} accepted overlong value")


def test_task_service_uses_logger_not_print() -> None:
    assert "print(" not in inspect.getsource(TaskService)


def test_generic_clients_use_independent_timeouts_and_reuse_download_pool() -> None:
    async def scenario() -> None:
        provider = GenericAPIProvider({
            "api_key": "secret",
            "timeout": 17,
            "download_timeout": 23,
        })
        assert provider.timeout == 17
        assert provider.download_timeout == 23
        api_client = await provider._get_client()
        download_one = await provider._get_download_client()
        download_two = await provider._get_download_client()
        assert download_one is download_two
        assert api_client.timeout.read == 17
        assert download_one.timeout.read == 23
        assert provider.key_header not in download_one.headers
        await provider.close()
        assert api_client.is_closed and download_one.is_closed

    asyncio.run(scenario())


def main() -> int:
    tests = [
        test_asset_response_keeps_relative_filestore_key,
        test_tag_counts_trim_and_merge_case_variants,
        test_upload_fallback_filename_uses_allowed_extension,
        test_generic_status_404_is_terminal_error,
        test_generic_progress_is_validated_and_clamped,
        test_tag_query_parameters_are_length_bounded,
        test_remove_tag_is_case_insensitive_and_rejects_long_values,
        test_schema_strings_match_database_column_widths,
        test_task_service_uses_logger_not_print,
        test_generic_clients_use_independent_timeouts_and_reuse_download_pool,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {failures[-1]}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\nRESULT: {len(tests) - len(failures)} passed / {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
