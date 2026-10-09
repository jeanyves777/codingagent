"""Install, update and roll back Coding Brain from verified releases.

Versions are installed side by side (app/versions/<version>/venv); the launcher points at one of
them. An update never replaces the code a running session uses: it is refused while a session or
a task is active, the new version is installed next to the old one, state is backed up, the new
version migrates state and passes its health check, and only then does the launcher switch. Any
failure restores the backup and leaves the previous version active.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .paths import Layout
from .session import active_sessions, pid_alive, running_tasks

REPOSITORY = "jeanyves777/codingagent"
REQUIRED = ("release.json", "constraints.txt", "SHA256SUMS")
KEEP_VERSIONS = 3
SKIPPED_STATE = {"workspace", "baseline", "integration"}  # task worktrees: large, not state
SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")  # captured by SQLite's backup API, never copied raw


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    tag: str
    prerelease: bool = False
    notes: str = ""
    assets: dict = field(default_factory=dict)  # name -> URL or local path

    @property
    def wheel(self) -> str:
        return f"coding_brain-{self.version}-py3-none-any.whl"


class GitHubSource:
    """Published GitHub releases: the stable channel uses the latest full release; the dev channel
    also considers pre-releases. Both are tagged, pinned versions, never a branch head."""

    def __init__(self, repository: str = REPOSITORY, channel: str = "stable"):
        if channel not in {"stable", "dev"}:
            raise UpdateError("channel must be stable or dev")
        self.repository, self.channel = repository, channel

    def _get(self, url: str):
        import httpx
        response = httpx.get(url, follow_redirects=True, timeout=60,
                             headers={"Accept": "application/vnd.github+json", "User-Agent": "codingbrain-updater"})
        if response.status_code == 404:
            raise UpdateError(f"Not found: {url}")
        response.raise_for_status()
        return response

    @staticmethod
    def _release(payload: dict) -> Release:
        tag = payload["tag_name"]
        return Release(version=tag.removeprefix("v"), tag=tag, prerelease=bool(payload.get("prerelease")),
                       notes=payload.get("body") or "",
                       assets={asset["name"]: asset["browser_download_url"] for asset in payload.get("assets", [])})

    def release(self, version: str | None = None) -> Release:
        api = f"https://api.github.com/repos/{self.repository}/releases"
        if version:
            return self._release(self._get(f"{api}/tags/v{version.removeprefix('v')}").json())
        if self.channel == "stable":
            return self._release(self._get(f"{api}/latest").json())
        candidates = [item for item in self._get(f"{api}?per_page=30").json() if not item.get("draft")]
        if not candidates:
            raise UpdateError("No releases published")
        return max((self._release(item) for item in candidates), key=lambda item: Version(item.version))

    def download(self, release: Release, name: str, destination: Path) -> Path:
        import httpx
        if name not in release.assets:
            raise UpdateError(f"Release {release.tag} has no {name}")
        target = destination / name
        with httpx.stream("GET", release.assets[name], follow_redirects=True, timeout=300,
                          headers={"User-Agent": "codingbrain-updater"}) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return target


class DirectorySource:
    """Release assets in a local folder (offline installs, CI, tests)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def release(self, version: str | None = None) -> Release:
        info = json.loads((self.folder / "release.json").read_text(encoding="utf-8"))
        if version and Version(info["version"]) != Version(version):
            raise UpdateError(f"{self.folder} holds {info['version']}, not {version}")
        return Release(version=info["version"], tag=info.get("tag", "v" + info["version"]),
                       prerelease=info.get("channel") == "dev", notes=info.get("notes", ""),
                       assets={path.name: path for path in self.folder.iterdir() if path.is_file()})

    def download(self, release: Release, name: str, destination: Path) -> Path:
        if name not in release.assets:
            raise UpdateError(f"{self.folder} has no {name}")
        return Path(shutil.copy2(release.assets[name], destination / name))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_verified(source, release: Release, folder: Path) -> dict:
    """Download the wheel, constraints and release.json, and check each against SHA256SUMS."""
    sums_file = source.download(release, "SHA256SUMS", folder)
    sums = {}
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*").strip()] = digest.lower()
    files = {}
    for name in (release.wheel, "constraints.txt", "release.json"):
        if name not in sums:
            raise UpdateError(f"SHA256SUMS does not cover {name}")
        path = source.download(release, name, folder)
        if sha256(path) != sums[name]:
            raise UpdateError(f"Checksum mismatch for {name}: the download is corrupt or was altered")
        files[name] = path
    info = json.loads(files["release.json"].read_text(encoding="utf-8"))
    if Version(info["version"]) != Version(release.version):
        raise UpdateError(f"release.json says {info['version']} but the release is {release.version}")
    return {"wheel": files[release.wheel], "constraints": files["constraints.txt"], "info": info}


def read_current(layout: Layout) -> dict:
    if not layout.current_file.exists():
        return {}
    return json.loads(layout.current_file.read_text(encoding="utf-8"))


def write_launcher(layout: Layout, python: Path) -> Path:
    """The command on PATH. One line that ends the script, so replacing the file while it runs
    (during an update) cannot make cmd.exe execute a partial new file."""
    layout.bin.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # cmd.exe reads batch files in the console code page: refer to %LOCALAPPDATA% instead of
        # spelling out a user folder that may contain non-ASCII characters.
        text_path, local = str(python), os.environ.get("LOCALAPPDATA", "")
        if local and text_path.lower().startswith(local.lower() + "\\"):
            text_path = "%LOCALAPPDATA%" + text_path[len(local):]
        # -I: never import code from the current directory (a project may have its own `brain`
        # package) or from PYTHON* variables. The single line ends the script either way, and
        # passes failure on as exit code 1 (`& exit /b` alone loses it under cmd /c).
        target, text = (layout.bin / "codingbrain.cmd",
                        f'@"{text_path}" -I -m brain.local %* && exit /b 0 || exit /b 1\r\n')
    else:
        target, text = layout.bin / "codingbrain", f'#!/bin/sh\nexec "{python}" -I -m brain.local "$@"\n'
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(text, encoding="oem" if sys.platform == "win32" else "utf-8", newline="")
    except (UnicodeEncodeError, LookupError):
        temporary.write_text(text, encoding="utf-8", newline="")
    if sys.platform != "win32":
        temporary.chmod(0o755)
    os.replace(temporary, target)
    return target


def switch(layout: Layout, version: str, base_python: str | None = None) -> dict:
    current = read_current(layout)
    previous = [item for item in [current.get("version"), *current.get("previous", [])] if item and item != version]
    record = {"version": version, "previous": previous[:KEEP_VERSIONS - 1], "switched": time.time(),
              "base_python": base_python or current.get("base_python") or getattr(sys, "_base_executable", sys.executable)}
    layout.app.mkdir(parents=True, exist_ok=True)
    temporary = layout.current_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, layout.current_file)
    write_launcher(layout, layout.venv_python(version))
    return record


def prune(layout: Layout):
    current = read_current(layout)
    keep = {current.get("version"), *current.get("previous", [])}
    for folder in layout.versions.iterdir() if layout.versions.exists() else []:
        if folder.name not in keep:
            shutil.rmtree(folder, ignore_errors=True)


def _state_files(layout: Layout):
    for root in (layout.config, layout.data):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(layout.home)
            if path.is_file() and not SKIPPED_STATE & set(relative.parts) and not path.name.endswith(SQLITE_SIDE_FILES):
                yield path, relative


def backup_state(layout: Layout, version: str, reason: str) -> Path:
    """Copy configuration and state (databases through SQLite's online backup) to backups/."""
    target = layout.backups / f"{time.strftime('%Y%m%d-%H%M%S')}-from-{version}"
    files = {}
    for path, relative in _state_files(layout):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in {".sqlite3", ".db"}:
            source, copy = sqlite3.connect(path), sqlite3.connect(destination)
            try:
                source.backup(copy)
                # The copy inherits WAL mode; fold it into one self-contained file before closing,
                # so the checksum and a later restore see every page.
                copy.execute("PRAGMA journal_mode=DELETE")
                copy.commit()
            finally:
                copy.close()
                source.close()
        else:
            shutil.copy2(path, destination)
        files[relative.as_posix()] = sha256(destination)
    (target / "manifest.json").write_text(json.dumps(
        {"from_version": version, "reason": reason, "created": time.time(), "files": files}, indent=2),
        encoding="utf-8")
    return target


def restore_state(layout: Layout, backup: Path):
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for path, relative in list(_state_files(layout)):
        if relative.as_posix() not in manifest["files"]:
            path.unlink()  # created after the backup (e.g. by a failed migration)
    for relative, digest in manifest["files"].items():
        source = backup / relative
        if sha256(source) != digest:
            raise UpdateError(f"Backup file {relative} is damaged")
        destination = layout.home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        for suffix in SQLITE_SIDE_FILES:  # a stale write-ahead log must not replay onto the restored database
            Path(str(destination) + suffix).unlink(missing_ok=True)
        shutil.copy2(source, destination)


def _run(python: Path, layout: Layout, *arguments: str, timeout=900) -> subprocess.CompletedProcess:
    environment = {**os.environ, "CODINGBRAIN_HOME": str(layout.home), "PYTHONUTF8": "1"}
    if arguments[:2] == ("-m", "brain.local"):
        arguments = ("-I", *arguments)  # the installed version's code, never the current directory's
    return subprocess.run([str(python), *arguments], capture_output=True, text=True, timeout=timeout,
                          env=environment)


def install_version(layout: Layout, version: str, wheel: Path, constraints: Path, base_python: str) -> Path:
    folder = layout.versions / version
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    created = subprocess.run([base_python, "-m", "venv", str(folder / "venv")], capture_output=True, text=True)
    if created.returncode:
        raise UpdateError("Could not create a virtual environment: " + created.stderr[-500:])
    python = layout.venv_python(version)
    installed = _run(python, layout, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                     "--constraint", str(constraints), str(wheel), timeout=1800)
    if installed.returncode:
        raise UpdateError("Installing the release failed: " + (installed.stderr or installed.stdout)[-1500:])
    return python


def _guard(layout: Layout):
    sessions = [item for item in active_sessions(layout) if item["pid"] != os.getpid()]
    if sessions:
        raise UpdateError("A Coding Brain session is running (" + ", ".join(
            f"{item['project']} pid {item['pid']}" for item in sessions) + "); finish it, then update")
    tasks = running_tasks(layout)
    if tasks:
        raise UpdateError("Tasks are mid-execution: " + ", ".join(f"{t['project']}:{t['id'][:8]} ({t['status']})"
                                                                 for t in tasks) +
                          ". Let them finish, or interrupt them (they resume later), then update")


class UpdateLock:
    def __init__(self, layout: Layout):
        self.path = layout.locks / "update.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(json.loads(self.path.read_text())["pid"])
            except (ValueError, KeyError, OSError):
                pid = 0
            if pid_alive(pid):
                raise UpdateError(f"Another update is running (pid {pid})")
        self.path.write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def install_release(layout: Layout, source, installed: str | None, version: str | None = None,
                    force: bool = False, log=print) -> dict:
    """Install a release (fresh install when installed is None, otherwise an update)."""
    layout.ensure()
    with UpdateLock(layout):
        if installed:
            _guard(layout)
        release = source.release(version)
        if installed and not force and Version(release.version) <= Version(installed):
            return {"status": "up_to_date", "installed": installed, "available": release.version}
        log(f"Downloading {release.tag} and verifying checksums")
        # On Windows a virus scanner may still hold a fresh download open; a leftover temporary
        # folder must not fail an otherwise complete update.
        with tempfile.TemporaryDirectory(dir=layout.home, prefix="download-", ignore_cleanup_errors=True) as scratch:
            fetched = fetch_verified(source, release, Path(scratch))
            info = fetched["info"]
            base_python = read_current(layout).get("base_python") or getattr(sys, "_base_executable", sys.executable)
            python_version = subprocess.run([base_python, "-c", "import platform; print(platform.python_version())"],
                                            capture_output=True, text=True).stdout.strip()
            if info.get("requires_python") and python_version not in SpecifierSet(info["requires_python"]):
                raise UpdateError(f"{release.tag} needs Python {info['requires_python']}; found {python_version}")
            if installed and info.get("min_upgrade_from") and Version(installed) < Version(info["min_upgrade_from"]):
                raise UpdateError(f"{release.tag} cannot upgrade {installed} directly; install "
                                  f"{info['min_upgrade_from']} first (see its release notes)")
            backup = backup_state(layout, installed or "none", f"before installing {release.version}")
            log(f"Backed up configuration and state to {backup}")
            try:
                log(f"Installing {release.version} next to the current version")
                python = install_version(layout, release.version, fetched["wheel"], fetched["constraints"], base_python)
                migrated = _run(python, layout, "-m", "brain.local", "migrate", "--json")
                if migrated.returncode:
                    raise UpdateError("State migration failed: " + (migrated.stderr or migrated.stdout)[-1500:])
                health = _run(python, layout, "-m", "brain.local", "doctor", "--offline", "--json")
                report = json.loads(health.stdout or "{}") if health.stdout.strip().startswith("{") else {}
                if health.returncode or not report.get("ok"):
                    raise UpdateError("Health check failed: " + (health.stdout or health.stderr)[-1500:])
            except Exception:
                restore_state(layout, backup)
                shutil.rmtree(layout.versions / release.version, ignore_errors=True)
                log(f"Update failed; restored state from {backup}" +
                    (f" and kept {installed}" if installed else ""))
                raise
        switch(layout, release.version, base_python)
        prune(layout)
        return {"status": "installed", "installed": release.version, "previous": installed,
                "backup": str(backup), "migration": json.loads(migrated.stdout or "{}"), "notes": release.notes}


def rollback(layout: Layout, restore_backup: bool = False, log=print) -> dict:
    """Switch the launcher back to the previous version. With restore_backup, also restore the
    state backup taken when updating away from it (current state is backed up first)."""
    with UpdateLock(layout):
        _guard(layout)
        current = read_current(layout)
        previous = next((item for item in current.get("previous", [])
                         if layout.venv_python(item).exists()), None)
        if not previous:
            raise UpdateError("No previous version is installed to roll back to")
        python = layout.venv_python(previous)
        restored = None
        if restore_backup:
            backups = sorted(path for path in layout.backups.glob(f"*-from-{previous}") if (path / "manifest.json").exists())
            if not backups:
                raise UpdateError(f"No state backup from {previous} exists")
            safety = backup_state(layout, current["version"], f"before rolling back to {previous}")
            restore_state(layout, backups[-1])
            restored = {"restored": str(backups[-1]), "current_state_saved_to": str(safety)}
        health = _run(python, layout, "-m", "brain.local", "doctor", "--offline", "--json")
        if health.returncode:
            raise UpdateError(f"{previous} cannot use the current state (schema too new?). Run "
                              "`codingbrain rollback --restore-state` to also restore the state backup "
                              f"taken before the update. Details: {(health.stdout or health.stderr)[-800:]}")
        record = switch(layout, previous)
        log(f"Rolled back to {previous}")
        return {"status": "rolled_back", "version": previous, "from": current["version"], **(restored or {}),
                "record": record}
