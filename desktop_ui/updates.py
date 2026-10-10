"""Read-only stable and preview update discovery for the native app and installed engine.

Stable means a tagged, owner-approved GitHub Release. Preview means a successful GitHub Actions
run for the current `preview` branch head and an unexpired version-named artifact. A preview
artifact is NEVER called a GitHub Release, and the user must explicitly select the preview
channel. The updater rechecks the run and verifies the downloaded files before installing.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

REPO = "jeanyves777/codingagent"
API = "https://api.github.com/repos/" + REPO
RELEASE_URL = API + "/releases/latest"
VERSION_FILE = Path(__file__).with_name("release-version.json")
PREVIEW_WORKFLOW_NAME = "Preview build (unreleased, Windows)"


def _json(url: str) -> dict:
    request = Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "CodingBrainDesktop",
        "Cache-Control": "no-cache",
    })
    with urlopen(request, timeout=12) as result:
        return json.load(result)


def desktop_version() -> str | None:
    try:
        value = json.loads(VERSION_FILE.read_text(encoding="utf-8"))["version"]
        if not isinstance(value, str):
            return None
        Version(value)
        return value
    except (OSError, ValueError, TypeError, KeyError, InvalidVersion):
        return None


def _installed_comparison(version: str, engine_version: str | None, desktop: str | None) -> tuple[bool, bool]:
    published = Version(version)
    try:
        engine_behind = not engine_version or Version(engine_version) < published
    except InvalidVersion:
        engine_behind = True
    try:
        desktop_behind = not desktop or Version(desktop) < published
    except InvalidVersion:
        desktop_behind = True
    return engine_behind, desktop_behind


def _preview_candidate() -> dict:
    # Only the current preview branch head may be offered: stale successful runs on
    # superseded code must never be advertised to users.
    current_head = _json(API + "/git/ref/heads/preview")["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", str(current_head)):
        raise ValueError("GitHub preview branch ref is invalid")
    payload = _json(API + "/actions/workflows/preview-build.yml/runs?branch=preview&per_page=30")
    runs = [r for r in payload.get("workflow_runs", []) if
            r.get("name") == PREVIEW_WORKFLOW_NAME and r.get("head_branch") == "preview"
            and r.get("head_sha") == current_head and r.get("status") == "completed"
            and r.get("conclusion") == "success"]
    if not runs:
        raise ValueError("The current preview branch has no successful, completed Windows preview build")
    run = max(runs, key=lambda r: int(r.get("run_number") or 0))
    run_id = int(run["id"])
    artifact_list = _json(API + f"/actions/runs/{run_id}/artifacts")
    matches = []
    for artifact in artifact_list.get("artifacts", []):
        name = artifact.get("name", "")
        match = re.fullmatch(r"CodingBrain-Preview-(\d+\.\d+\.\d+\.dev\d+)", name)
        if match and not artifact.get("expired"):
            matches.append((match.group(1), name))
    if len(matches) != 1:
        raise ValueError("Successful preview run has no unique unexpired versioned engine + desktop artifact")
    version, artifact = matches[0]
    # The workflow derives the preview version from its run number; reject relabelled artifacts.
    if int(Version(version).dev) != int(run.get("run_number") or -1):
        raise ValueError("Preview artifact version does not match the successful build run")
    return {"version": version, "run_id": run_id, "source_commit": current_head,
            "artifact": artifact, "run_url": run["html_url"]}


def release_status(engine_version: str | None, channel: str = "stable") -> dict:
    """Fetch fresh GitHub metadata without checking out, downloading or executing remote code."""
    if channel not in {"stable", "preview"}:
        raise ValueError("Unsupported update channel")
    local = desktop_version()
    if channel == "preview":
        preview = _preview_candidate()
        version = preview["version"]
        engine_behind, desktop_behind = _installed_comparison(version, engine_version, local)
        return {
            "channel": "preview", "unreleased": True,
            "latest": version, "tag": "UNRELEASED preview " + version,
            "engine": engine_version or "unknown", "desktop": local or "unversioned/preview",
            "engine_update_available": engine_behind, "desktop_update_available": desktop_behind,
            "desktop_in_release": True, "any_update_available": engine_behind or desktop_behind,
            "engine_only": False, "release_url": preview["run_url"],
            "preview_run_id": preview["run_id"], "preview_source_commit": preview["source_commit"],
            "preview_artifact": preview["artifact"],
        }
    release = _json(RELEASE_URL)
    tag = release.get("tag_name")
    if (release.get("draft") or release.get("prerelease") or
            not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag)):
        raise ValueError("Latest GitHub Release is not an approved stable version")
    version = tag[1:]
    engine_behind, desktop_behind = _installed_comparison(version, engine_version, local)
    names = {item.get("name") for item in release.get("assets", []) if isinstance(item, dict)}
    desktop_published = {"CodingBrain-Setup.exe", "SHA256SUMS", "release.json"} <= names
    return {
        "channel": "stable", "unreleased": False,
        "latest": version, "tag": tag,
        "engine": engine_version or "unknown", "desktop": local or "unversioned/preview",
        "engine_update_available": engine_behind,
        "desktop_update_available": desktop_behind and desktop_published,
        "desktop_in_release": desktop_published,
        "any_update_available": engine_behind or (desktop_behind and desktop_published),
        "engine_only": not desktop_published,
        "release_url": "https://github.com/" + REPO + "/releases/tag/" + tag,
    }
