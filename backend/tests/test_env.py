"""Shared endpoint configuration for service-backed verification scripts."""

import os
from urllib.parse import urlsplit


_ENV_NAME = "COMFYUI_STUDIO_TEST_BASE_URL"
SERVICE_BASE_URL = os.environ.get(
    _ENV_NAME,
    "http://127.0.0.1:8000",
).rstrip("/")
_SERVICE_URL = urlsplit(SERVICE_BASE_URL)

if (
    _SERVICE_URL.scheme not in {"http", "https"}
    or not _SERVICE_URL.hostname
    or _SERVICE_URL.path not in {"", "/"}
    or _SERVICE_URL.query
    or _SERVICE_URL.fragment
):
    raise RuntimeError(
        f"{_ENV_NAME} must be an HTTP(S) service origin without /api or another path"
    )

SERVICE_HOST = _SERVICE_URL.hostname
SERVICE_PORT = _SERVICE_URL.port or (443 if _SERVICE_URL.scheme == "https" else 80)
API_BASE_URL = f"{SERVICE_BASE_URL}/api"
