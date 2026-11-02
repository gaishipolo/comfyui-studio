"""Asset filename normalization shared by runtime writes and DB migration."""
from pathlib import Path


MAX_ASSET_FILENAME = 256


def unique_asset_name(filename: str, used: set[str]) -> str:
    raw = Path(filename).name or "asset.bin"
    suffix = Path(raw).suffix
    if len(suffix) >= MAX_ASSET_FILENAME:
        suffix = ""
    stem = raw[:-len(suffix)] if suffix else raw
    stem = stem or "asset"

    def candidate(marker: str = "") -> str:
        room = MAX_ASSET_FILENAME - len(suffix) - len(marker)
        return f"{stem[:max(1, room)]}{marker}{suffix}"

    name = candidate()
    index = 2
    while name in used:
        name = candidate(f"_{index}")
        index += 1
    return name
