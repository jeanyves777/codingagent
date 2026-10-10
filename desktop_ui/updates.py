"""Read-only update discovery for both the native app and the separately installed engine.

Never treats a draft PR or workflow artifact as a release. Download/install is performed
by Update-CodingBrain.ps1 after explicit consent and cryptographic checksum validation.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

RELEASE_URL = "https://api.github.com/repos/jeanyves777/codingagent/releases/latest"
VERSION_FILE = Path(__file__).with_name("release-version.json")


def desktop_version() -> str | None:
    try:
        result = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        version = result["version"]
        return version if isinstance(version, str) and Version(version) else None
    except (OSError, ValueError, TypeError, KeyError, InvalidVersion):
        return None


def release_status(engine_version: str | None) -> dict:
    """Fetch current stable release metadata directly from GitHub, without caching."""
    request = Request(RELEASE_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "CodingBrainDesktop",
        "Cache-Control": "no-cache",
    })
    with urlopen(request, timeout=12) as result:
        release = json.load(result)
    tag = release.get("tag_name")
    if release.get("draft") or release.get("prerelease") or not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("Latest GitHub release is not a stable tagged release")
    published = Version(tag[1:])
    local = desktop_version()
    try:
        engine_behind = not engine_version or Version(engine_version) < published
    except InvalidVersion:
        engine_behind = True
    try:
        desktop_behind = not local or Version(local) < published
    except InvalidVersion:
        desktop_behind = True
    names = {item.get("name") for item in release.get("assets", []) if isinstance(item, dict)}
    desktop_published = "CodingBrain-Setup.exe" in names and "SHA256SUMS" in names and "release.json" in names
    return {
        "latest": str(published), "tag": tag,
        "engine": engine_version or "unknown", "desktop": local or "unversioned/preview",
        "engine_update_available": engine_behind,
        "desktop_update_available": desktop_behind and desktop_published,
        "desktop_in_release": desktop_published,
        "any_update_available": engine_behind or (desktop_behind and desktop_published),
        "engine_only": not desktop_published,
        "release_url": "https://github.com/jeanyves777/codingagent/releases/tag/" + tag,
    }
