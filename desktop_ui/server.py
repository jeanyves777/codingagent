"""Loopback-only Coding Brain workspace UI.

A deliberately small presentation bridge.  Coding Brain remains the sole coding
executor; this module never loads its Brain service or modifies task state.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import management

ASSETS = Path(__file__).with_name("static")
IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", "build", ".idea", ".vscode"}
SENSITIVE = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
MAX_PREVIEW_BYTES = 128_000
MAX_EDIT_BYTES = 1_000_000  # the editor opens and saves text files up to this size
MAX_ENTRIES = 350
# Attachments mirror the engine's own limits (brain.attachments.LIMITS); the engine re-checks every file.
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 25_000_000
ATTACHMENT_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".docx", ".xlsx", ".csv", ".tsv", ".txt",
                    ".md", ".json", ".yaml", ".yml", ".xml", ".log", ".html", ".css", ".js", ".ts", ".tsx",
                    ".jsx", ".py", ".toml", ".ini", ".svg"}


def sensitive_name(name: str) -> bool:
    lower = name.lower()
    return (lower in SENSITIVE or lower.startswith('.env') or lower.endswith(('.pem', '.p12', '.pfx', '.key'))
            or lower in {'known_hosts', 'authorized_keys', 'secret', 'secrets'})


class ProjectChoice(BaseModel):
    path: str = Field(min_length=1, max_length=8192)


class StartRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16000)
    mode: str = "chat"
    attachments: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class SaveFile(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=MAX_EDIT_BYTES)
    sha256: str = Field(min_length=64, max_length=64)  # the version the editor opened


class OpenFile(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class ConfirmAction(BaseModel):
    confirmed: bool = False


class Onboarding(BaseModel):
    completed: bool


class ProviderSetting(BaseModel):
    enabled: bool


class Decision(BaseModel):
    allow: bool
    approval_id: str | None = None


class FileChangedError(ValueError):
    pass


class Job:
    def __init__(self, kind: str, project: str | None, command: list[str]):
        self.id = uuid.uuid4().hex
        self.kind, self.project, self.command = kind, project, command
        self.started = time.time()
        self.status = "starting"
        self.returncode: int | None = None
        self.process: subprocess.Popen | None = None
        self.events: collections.deque[dict] = collections.deque(maxlen=3000)
        self.cursor = 0
        self.lock = threading.Lock()
        self.awaiting_approval = False
        self.error: str | None = None
        self.goal = command[-1] if command else ""
        self.task_id: str | None = None
        self.pending_id: str | None = None
        self.pending_kind: str | None = None
        self.pending_payload: dict | None = None
        self.decision_event = threading.Event()
        self.decision_allow: bool | None = None
        self.stop_requested = threading.Event()
        self.cancel_core = None
        self.core = False
        self.new_project = False
        self.on_project_created = None
        self.attachments: list[str] = []

    def emit(self, kind: str, message: str, data: dict | None = None):
        with self.lock:
            self.cursor += 1
            event = {"seq": self.cursor, "kind": kind, "message": message, "at": time.time(), "job_id": self.id}
            if data:
                event["data"] = data  # the engine's structured fields: agent, model, phase, status, duration
            self.events.append(event)

    def dump(self, after: int = 0) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > after]

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "project": self.project, "status": self.status,
                "started": self.started, "returncode": self.returncode,
                "awaiting_approval": self.awaiting_approval, "error": self.error,
                "approval": {"id": self.pending_id, **self.pending_payload}
                    if self.pending_id and self.pending_payload else None,
                "task_id": self.task_id, "engine": "core" if self.core else "cli"}


# What each window mode needs from the installed engine (from engine.info's operation list, the
# authoritative feature flags for API 1.0 engines; never inferred from a version or an import).
REQUIRED_OPERATIONS = {
    "chat": ("conversation.send",),
    "run": ("projects.register", "tasks.start", "tasks.approve", "tasks.accept", "tasks.stop"),
    "new": ("conversation.send", "projects.create", "tasks.start", "tasks.approve", "tasks.accept"),
}


class Workspace:
    def __init__(self, home: Path | None = None, executable: str | None = None):
        self._home_override = home is not None
        self.home = (home or Path.home()).resolve()
        self.executable = executable
        self.secret = secrets.token_urlsafe(32)
        # Identifies this bridge process: a changed boot id tells the window the bridge restarted
        # and every in-memory job it was following is gone.
        self.boot = uuid.uuid4().hex
        self.project: Path | None = None
        self.jobs: dict[str, Job] = {}
        self.active: str | None = None
        self.lock = threading.Lock()
        self.use_core = True
        self.state_path = self._desktop_state_path()

    def _desktop_state_path(self) -> Path:
        # Desktop preferences are separate from brain configuration and its updater.
        # Tests passing an explicit home must never write into the runner's real APPDATA.
        if self._home_override:
            return self.home / ".codingbrain-desktop" / "preferences.json"
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", str(self.home / "AppData" / "Local")))
        else:
            base = Path(os.getenv("XDG_STATE_HOME") or (self.home / ".local" / "state"))
        return base / "CodingBrain" / "desktop" / "preferences.json"

    def first_run_completed(self) -> bool:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("completed") is True
        except (OSError, ValueError, TypeError):
            return False

    def save_first_run(self, complete: bool) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.state_path.with_suffix(".tmp")
        staging.write_text(json.dumps({"completed": bool(complete)}), encoding="utf-8")
        os.replace(staging, self.state_path)

    def _brain_home(self) -> Path:
        if self._home_override:
            return self.home / ".local" / "CodingBrain"
        return Path(os.environ.get("CODINGBRAIN_HOME") or
                    (Path(os.environ.get("LOCALAPPDATA", str(self.home / "AppData" / "Local"))) / "CodingBrain"))

    def engine_client(self):
        """The one typed-API transport, started from the installed engine's own interpreter."""
        from .engine_client import EngineClient
        command = self.cli_command()
        with self._engine_lock():
            client = getattr(self, "_engine", None)
            if client is None or client.command != command:
                if client is not None:
                    client.close()
                client = self._engine = EngineClient(command=command, cwd=str(self.home))
            return client

    def _engine_lock(self):
        lock = getattr(self, "_engine_lock_object", None)
        if lock is None:
            lock = self._engine_lock_object = threading.Lock()
        return lock

    def _typed_status(self, core: dict, refresh: bool = False) -> dict | None:
        """With API 1.0 installed, versions and provider readiness come from the engine itself
        (engine.info, providers.list), not from CLI probes or modules bundled into this UI."""
        from .engine_client import EngineError
        try:
            client = self.engine_client()
            info = client.call("engine.info", {}, None, 60)
        except (EngineError, ValueError) as error:
            core.update(typed_api=False, conversation=False, new_project=False, typed_api_failed=True,
                        detail=f"The installed engine's typed API did not start: {str(error)[:300]}")
            return None
        # Provider readiness checks the Claude/Codex sign-ins and can take 20 s or more (longer on a
        # cold start). It describes the supervisors only: a slow or failed check must never switch
        # chat off, so it is cached separately and its last good answer is kept on failure.
        cached = getattr(self, "_providers_cache", None)
        if cached and not refresh and time.monotonic() - cached[0] < 300:
            providers = cached[1]
        else:
            try:
                providers = client.call("providers.list", {}, None, 120)
                self._providers_cache = (time.monotonic(), providers)
            except (EngineError, ValueError):
                providers = cached[1] if cached else []
        operations = set(info.get("operations") or [])
        # The installed engine's own operation list decides chat and new-project support; the CLI
        # help probes in management.engine_capabilities are only the fallback for older engines.
        core.update(operations=sorted(operations), features=sorted(info.get("features") or []),
                    attachments="tasks.start.attachments" in (info.get("features") or []),
                    version=info.get("engine_version") or core.get("version"), api_version=info.get("api_version"),
                    conversation=all(op in operations for op in REQUIRED_OPERATIONS["chat"]),
                    new_project=all(op in operations for op in REQUIRED_OPERATIONS["new"]),
                    detail=f"Typed engine API {info.get('api_version')} (engine {info.get('engine_version')})")
        return {"providers": providers}

    def installed_version(self) -> str | None:
        """The active engine version from app/current.json: one small file read, no process."""
        try:
            version = json.loads((self._brain_home() / "app" / "current.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return version if isinstance(version, str) else None

    def health(self) -> dict:
        """Cheap liveness for the window's heartbeat: never starts the engine or a probe."""
        active = self.jobs.get(self.active) if self.active else None
        return {"ok": True, "boot": self.boot, "installed_version": self.installed_version(),
                "active": {"id": active.id, "status": active.status} if active else None}

    def engine_status(self, refresh: bool = False) -> dict:
        """Truthful installed-engine/model readiness; the local UI is a separate process.

        Cache cheap probes briefly so a slow provider CLI doesn't freeze chat or activity. A
        switched installed version (an update or rollback) always invalidates the cache.
        """
        now = time.monotonic()
        installed = self.installed_version()
        cached = getattr(self, "_engine_status_cache", None)
        if not refresh and cached and now - cached[0] < 20 and cached[2] == installed:
            return cached[1]
        try:
            command = self.cli_command()
        except ValueError:
            command = None
        core = management.engine_capabilities(command)
        typed = self._typed_status(core, refresh) if core.get("typed_api") else None
        model = management.configured_model(self._brain_home() / "config" / "config.json")
        ollama = management.local_ollama_models()
        wanted = model["model"]
        names = ollama["models"]
        # Ollama usually uses ':latest' for the default tag. Do not treat a
        # configured model as ready just because the Ollama executable exists.
        available = (model["provider"] == "ollama" and bool(wanted) and
                     any(name == wanted or (":" not in wanted and name == wanted + ":latest") for name in names))
        chat = bool(core["available"] and core["conversation"])
        if not core["available"]:
            title, detail = "Coding Brain engine unavailable", core["detail"]
            next_action = "setup"
        elif not chat:
            title = f"Engine v{core['version'] or '?'} needs the conversational release"
            detail = "Desktop UI is connected, but this installed engine cannot handle general chat. The development PR is not an installed release."
            next_action = "updates"
        elif model["provider"] == "ollama" and not available:
            title = "Local coding model not ready"
            detail = (f"Configured model {wanted} is not installed or Ollama is stopped." if wanted else
                      "Choose and install a local model through Coding Brain setup.")
            next_action = "setup"
        else:
            title = "Engine and chat interface available"
            detail = "Provider readiness and actual model generation require a deep system test."
            next_action = None
        report = {"bridge_online": True, "engine": core, "model": {
            "provider": model["provider"], "name": wanted, "ready": available,
            "ollama_running": ollama["running"], "installed_models": names,
            "detail": ollama["detail"] if model["provider"] == "ollama" else "Provider readiness not verified"},
            "chat_available": chat, "title": title, "detail": detail, "next_action": next_action}
        if typed:
            report["providers"] = typed["providers"]
        self._engine_status_cache = (now, report, installed)
        return report

    def update_status(self, channel: str = "stable") -> dict:
        """An uncached GitHub release check; preview is explicit and untagged."""
        from .updates import release_status
        if channel not in {"stable", "preview"}:
            raise ValueError("Invalid update channel")
        engine = self.engine_status()["engine"]
        return release_status(engine.get("version"), channel=channel)

    def installation_status(self) -> dict:
        try:
            cli = self.cli_command()
        except ValueError:
            cli = None
        status = self.engine_status(refresh=True)  # recheck: a CLI or sign-in may have changed
        report = management.installation_probes(cli, status.get("providers"))
        if cli:
            # A side-effect-free feature check: old v0.9.0 has no install command.
            found = management.safe_probe([*cli, "install", "--help"], timeout=6)
            report["full_installer_available"] = found is True
            report["full_installer_note"] = ("Install and repair through Coding Brain's own guided installer" if found is True
                    else "First publish/update to a Coding Brain release containing the full installer")
        # Detect option support rather than inferring it from an installed CLI version.
        report["deep_doctor_available"] = bool(cli and management.supports_cli_option(cli, "doctor", "--full"))
        report["doctor_note"] = ("Deep engine verification available" if report["deep_doctor_available"]
                                 else "Basic check available; full model/sandbox tests require a backend update")
        report["onboarding_completed"] = self.first_run_completed()
        return report

    def configured_providers(self) -> list[dict]:
        providers = management.providers_snapshot()
        base = self._brain_home()
        try:
            conf = json.loads((base / "config" / "config.json").read_text(encoding="utf-8"))
            supervisors = conf.get("supervisors", {})
        except (OSError, ValueError, TypeError):
            supervisors = {}
        status = self.engine_status()
        local = status["model"]
        engine_view = {item["id"]: item for item in status.get("providers") or []}
        for provider in providers:
            described = engine_view.get(provider["id"])
            if described and provider["id"] in {"claude", "codex"}:
                # The installed engine's own view (providers.list) wins over this UI's CLI probes.
                signed_in = described.get("authentication") == "signed_in"
                provider["authenticated"] = signed_in
                provider["status"] = ("not-installed" if not described.get("installed") else "authenticated" if signed_in
                                      else "sign-in-needed" if described.get("authentication") == "signed_out"
                                      else "auth-unverified")
                provider["engine_readiness"] = described.get("readiness")
                provider["source"] = "engine"
                provider["detail"] = (f"Installed engine reports {described.get('readiness')}"
                                      + (f": {described['last_error']}" if described.get("last_error") else ""))[:400]
                supervisors = {**supervisors, provider["id"]: {"enabled": bool(described.get("enabled"))}}
            if provider["id"] == "ollama":
                provider["model_name"] = local["name"]
                provider["model_ready"] = local["ready"]
                provider["ollama_running"] = local["ollama_running"]
                provider["detail"] = (f"Model {local['name']} is available via Ollama (generation not verified)"
                                      if local["ready"] else
                                      f"Configured model {local['name'] or '(none)'} is unavailable or Ollama is stopped")
                provider["status"] = "model-detected" if local["ready"] else "model-not-ready"
            if provider["id"] in {"claude", "codex"}:
                enabled = bool(supervisors.get(provider["id"], {}).get("enabled"))
                provider["supervisor_enabled"] = enabled
                # Signing in and actually being enabled are independent facts.
                # Never tell the UI it is connected merely because a CLI exists.
                provider["connection_state"] = (
                    "connected" if provider["authenticated"] and enabled else
                    "signed-in" if provider["authenticated"] else
                    "needs-sign-in" if provider["status"] == "sign-in-needed" else
                    "not-installed" if provider["status"] == "not-installed" else
                    "unverified"
                )
        return providers

    def sign_in(self, provider_id: str) -> None:
        # Authentication is always handled by the vendor's own interactive CLI.
        management.launch_interactive_windows(management.provider_auth_command(provider_id))

    def configure_provider(self, provider_id: str, enabled: bool) -> None:
        if provider_id not in {"claude", "codex"}:
            raise ValueError("This provider is not yet supported by the core supervisor router")
        cmd = [*self.cli_command(), "setup", "--non-interactive",
               "--enable-" + provider_id if enabled else "--no-enable-" + provider_id]
        # Never return stdout: the installer may print config paths/account hints.
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("Coding Brain setup could not be completed") from error
        if result.returncode:
            raise ValueError("Coding Brain rejected this supervisor setting")

    def maintenance(self, action: str, channel: str = "stable") -> dict:
        if channel not in {"stable", "preview"}:
            raise ValueError("Unsupported update channel")
        if channel == "preview" and action != "update_engine":
            raise ValueError("Preview channel is only supported for an explicit update")
        cli = self.cli_command()
        deep_doctor = management.supports_cli_option(cli, "doctor", "--full") if action == "doctor" else True
        command = management.maintenance_args(action, cli, deep_doctor=deep_doctor)
        if action == "update_engine" and os.name == "nt" and cli[1:] == ["-m", "brain.local"]:
            # Run from the packaged desktop's resources, outside the engine being updated.
            # The helper pins a single official release, updates the backend through its
            # own verified updater, then verifies and installs the *matching* desktop EXE.
            # It prompts to close this GUI before changing running program files.
            helper = Path(__file__).with_name("Update-CodingBrain.ps1")
            if not helper.is_file():
                raise ValueError("Desktop update helper missing from this installation; reinstall a verified desktop build")
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper),
                       "-EnginePython", cli[0], "-BrainHome", str(self._brain_home()),
                       "-Channel", channel]
        if action == "update_engine" and channel == "preview" and not (os.name == "nt" and command[0].lower().endswith("powershell.exe")):
            raise ValueError("This desktop build cannot install preview engine + native packages together")
        if action in {"install_full", "update_engine", "repair"}:
            # New console owns prompts, elevation and restarts. No silent consent.
            management.launch_interactive_windows(command)
            message = ("Close the desktop and follow the verified engine + desktop update instructions in the opened "
                       "PowerShell window. The GUI must stay closed until the update finishes."
                       if action == "update_engine" and os.name == "nt" and command[0].lower().endswith("powershell.exe")
                       else "The official Coding Brain workflow opened in a terminal. Follow its prompts; refresh this screen afterward.")
            return {"opened_terminal":True,"message":message}
        with self.lock:
            if self.active and self.jobs[self.active].status in {"starting", "running", "approval_required"}:
                raise ValueError("Wait for the active operation to finish")
            job = Job("maintenance", None, command)
            self.jobs[job.id] = job
            self.active = job.id
        threading.Thread(target=self._execute, args=(job,), daemon=True).start()
        return {"opened_terminal":False, "job":job.view(),
                "diagnostic_level": ("deep" if deep_doctor else "basic") if action == "doctor" else None}


    def cli_command(self) -> list[str]:
        """Run Python directly on Windows to avoid .cmd shell interpretation.

        Windows automatically invokes .bat/.cmd via cmd.exe even when Popen is
        given shell=False. User goals must not cross that quoting boundary.
        """
        if self.executable:
            if os.name == "nt" and self.executable.lower().endswith((".cmd", ".bat")):
                raise ValueError("Batch-based CLI launchers are not supported by the UI bridge")
            return [self.executable]
        if os.name == "nt":
            local = self._brain_home()
            current = local / "app" / "current.json"
            try:
                version = json.loads(current.read_text(encoding="utf-8"))["version"]
                python = local / "app" / "versions" / version / "venv" / "Scripts" / "python.exe"
                if python.is_file():
                    return [str(python), "-m", "brain.local"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            raise ValueError("Coding Brain Python environment not found; update or repair installation")
        binary = shutil.which("codingbrain")
        if not binary:
            raise ValueError("Coding Brain executable not found on PATH")
        return [binary]

    def projects(self) -> list[dict]:
        base = Path(os.getenv("CODINGBRAIN_HOME") or (Path(os.getenv("LOCALAPPDATA", str(self.home / ".local"))) / "CodingBrain"))
        base = base / "data" / "projects"
        results: dict[str, dict] = {}
        if base.is_dir():
            for entry in base.glob("*/project.json"):
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    p = Path(data["root"]).resolve(strict=True)
                    if p.is_dir():
                        results[str(p)] = {"name": p.name, "path": str(p)}
                except (OSError, ValueError, KeyError):
                    continue
        if self.project:
            results[str(self.project)] = {"name": self.project.name, "path": str(self.project)}
        return sorted(results.values(), key=lambda x: x["name"].lower())

    def choose(self, raw: str) -> dict:
        path = Path(raw).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Choose a directory")
        self.project = path
        if self.git_branch(path):
            # Make the conversation know this project: registering is read-only to the folder.
            threading.Thread(target=self._register, args=(path,), daemon=True).start()
        return {"name": path.name, "path": str(path), "git": self.git_branch(path)}

    def _register(self, path: Path):
        try:
            if self.engine_status()["engine"].get("typed_api"):
                self.engine_client().call("projects.register", {"path": str(path)}, None, 120)
        except Exception:
            pass  # chat then answers without project context, as before

    def _project_id(self, client) -> str | None:
        if not self.project:
            return None
        for item in client.call("projects.list", {}, None, 60):
            if Path(item["root"]).resolve() == self.project:
                return item["id"]
        return None

    def conversation(self, limit: int = 60) -> dict:
        """The conversation of the selected project (or global chat), as the engine stored it."""
        if not self.engine_status()["engine"].get("typed_api"):
            return {"project": None, "turns": [], "available": False}
        client = self.engine_client()
        project_id = self._project_id(client)
        params = {"limit": limit, **({"project_id": project_id} if project_id else {})}
        turns = client.call("conversation.history", params, None, 60)
        return {"project": self.project.name if project_id else None, "turns": turns, "available": True}

    def documents_folder(self) -> Path:
        from brain.documents import output_folder
        return output_folder().resolve()

    def open_document(self, raw: str, reveal: bool = False) -> dict:
        """Open (or show in Explorer) a file Coding Brain wrote into its documents folder; nothing else."""
        path = Path(raw).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(self.documents_folder()):
            raise ValueError("Only documents Coding Brain created can be opened from here")
        if os.name != "nt":
            raise ValueError("Opening files is supported on Windows")
        if reveal:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606 - a file in the Coding Brain documents folder
        return {"opened": str(path)}

    def reset_conversation(self) -> dict:
        client = self.engine_client()
        if "conversation.reset" not in set(client.call("engine.info", {}, None, 60).get("features") or []):
            raise ValueError("Update Coding Brain to start a new conversation from the desktop")
        project_id = self._project_id(client)
        return client.call("conversation.reset", {"project_id": project_id} if project_id else {}, None, 60)

    @staticmethod
    def git_branch(root: Path) -> str | None:
        try:
            p = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                               capture_output=True, text=True, timeout=3)
            return p.stdout.strip() if p.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def resolve_read(self, relative: str) -> Path:
        if not self.project:
            raise ValueError("Select a project first")
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or any(part in IGNORED or sensitive_name(part) for part in rel.parts):
            raise ValueError("This path is not available for preview")
        path = self.project / rel
        # Never follow symlinks/junctions, including intermediate directories.
        root = self.project
        if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
            raise ValueError("Links and junctions are not available for preview")
        current = root
        for part in rel.parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise ValueError("Links and junctions are not available for preview")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.project):
            raise ValueError("Path escapes the selected project")
        return resolved

    def tree(self, relative: str) -> list[dict]:
        folder = self.resolve_read(relative) if relative else self.project
        if folder is None or not folder.is_dir():
            raise ValueError("Not a directory")
        result = []
        for item in folder.iterdir():
            if (item.name in IGNORED or sensitive_name(item.name) or item.is_symlink()
                    or (hasattr(item, "is_junction") and item.is_junction())):
                continue
            try:
                if not (item.is_file() or item.is_dir()):
                    continue
                result.append({"name": item.name, "path": item.relative_to(self.project).as_posix(),
                               "dir": item.is_dir()})
            except (OSError, ValueError):
                continue
            if len(result) >= MAX_ENTRIES:
                break
        return sorted(result, key=lambda x: (not x["dir"], x["name"].casefold()))

    def preview(self, relative: str) -> dict:
        path = self.resolve_read(relative)
        if not path.is_file() or path.stat().st_size > MAX_EDIT_BYTES:
            raise ValueError("The editor opens text files up to 1 MB")
        raw = path.read_bytes()
        if b"\0" in raw:
            raise ValueError("Binary files cannot be previewed")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Preview supports UTF-8 text only") from None
        return {"path": relative, "content": content, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def save(self, relative: str, content: str, expected_sha256: str) -> dict:
        """Write an existing text file of the selected project from the editor. Same path rules as
        reading (no links, secrets or ignored folders; inside the project), the file must still be the
        version the editor opened, and the write is atomic."""
        path = self.resolve_read(relative)
        if not path.is_file():
            raise ValueError("Only existing files can be saved from the editor")
        current = path.read_bytes()
        if b"\0" in current:
            raise ValueError("Binary files cannot be edited")
        if hashlib.sha256(current).hexdigest() != expected_sha256:
            raise FileChangedError("This file changed on disk after you opened it. Reload it, then reapply your edit.")
        data = content.encode("utf-8")
        if len(data) > MAX_EDIT_BYTES:
            raise ValueError("The editor saves text files up to 1 MB")
        staging = path.with_name(f".{path.name}.codingbrain-{uuid.uuid4().hex[:8]}.tmp")
        try:
            staging.write_bytes(data)
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
        return {"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def open_external(self, relative: str) -> dict:
        """Open a project file in VS Code when it is installed, else in the default application."""
        path = self.resolve_read(relative)
        code = shutil.which("code")
        if code and os.name == "nt":
            # code.cmd sits in <VS Code>\bin; start Code.exe itself so no shell parses the path.
            exe = Path(code).resolve().parent.parent / "Code.exe"
            if exe.is_file():
                subprocess.Popen([str(exe), "--goto", str(path)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {"opened": "vscode"}
        elif code:
            subprocess.Popen([code, "--goto", str(path)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"opened": "vscode"}
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 - a validated project file, default handler
            return {"opened": "default"}
        raise ValueError("VS Code was not found")

    def job_diff(self, job_id: str) -> list[dict]:
        """Side-by-side views of the proposal waiting for approval (display only)."""
        from .diffview import views
        job = self.jobs[job_id]
        diff = (job.pending_payload or {}).get("diff") or ""
        if not diff:
            raise ValueError("No proposed changes to show")
        root = Path(job.project) if job.project else None

        def allowed(relative: str) -> bool:
            if not root:
                return False
            parts = Path(relative).parts
            if Path(relative).is_absolute() or ".." in parts or any(part in IGNORED or sensitive_name(part) for part in parts):
                return False
            current = root
            for part in parts:
                current = current / part
                if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                    return False
            return True
        return views(diff, root, allowed)

    def _staging(self) -> Path:
        """Where pasted or dropped files wait for the task. Never inside the engine's home: the engine
        refuses to attach anything from its own state folder, which made every upload fail on
        Windows (%LOCALAPPDATA%\\CodingBrain is both the engine home and the desktop's parent)."""
        if self._home_override:
            staging = self.home / ".codingbrain-desktop" / "attachments"
        elif os.name == "nt":
            staging = Path(os.environ.get("LOCALAPPDATA", str(self.home / "AppData" / "Local"))) / "CodingBrainDesktop" / "attachments"
        else:
            staging = Path(os.getenv("XDG_STATE_HOME") or (self.home / ".local" / "state")) / "codingbrain-desktop" / "attachments"
        brain = self._brain_home().resolve()
        resolved = staging.resolve()
        if resolved == brain or brain in resolved.parents:  # e.g. CODINGBRAIN_HOME set above it
            import tempfile
            staging = Path(tempfile.gettempdir()) / "codingbrain-desktop-attachments"
        return staging

    @staticmethod
    def attachment_view(path: Path) -> dict:
        return {"path": str(path), "name": path.name, "bytes": path.stat().st_size}

    def check_attachment(self, raw: str) -> Path:
        """A file the user chose: a regular file (no link), a supported type, within the size limit,
        and never a secret. The engine applies its full checks again before parsing."""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("Attachments must be absolute file paths")
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"{path.name}: links are not accepted as attachments")
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise ValueError(f"{path.name}: file not found") from None
        if not path.is_file():
            raise ValueError(f"{path.name}: not a file")
        if sensitive_name(path.name):
            raise ValueError(f"{path.name}: secret and key files are never attached")
        if path.suffix.lower() not in ATTACHMENT_TYPES:
            raise ValueError(f"{path.name}: unsupported type (images, PDF, Word, Excel, CSV, text and source files)")
        if path.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{path.name}: larger than 25 MB")
        return path

    def stage_upload(self, name: str, data: bytes) -> dict:
        """A pasted screenshot or dropped file, saved privately so the engine can read it by path."""
        clean = "".join(ch for ch in Path(name or "").name if ch.isalnum() or ch in "._- ").strip(" .") or "pasted.png"
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{clean}: larger than 25 MB")
        if not data:
            raise ValueError(f"{clean}: empty file")
        folder = self._staging() / uuid.uuid4().hex
        folder.mkdir(parents=True)
        target = folder / clean[:120]
        target.write_bytes(data)
        try:
            return self.attachment_view(self.check_attachment(str(target)))
        except ValueError:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def start(self, body: StartRequest) -> Job:
        mode = body.mode
        attachments = [str(self.check_attachment(item)) for item in body.attachments]
        if attachments and mode == "chat":
            raise ValueError("Chat only reads your message; attachments are used by Run task and New project. "
                             "Switch mode to send these files. Nothing was started.")
        if mode not in {"chat", "run", "new"}:
            raise ValueError("Unknown mode")
        if mode == "run" and (not self.project or not self.git_branch(self.project)):
            raise ValueError("Select an existing Git repository to run a coding task")
        # API 1.0 engines are gated below by the operations engine.info lists (which names what is
        # missing); these checks cover engines without the typed API, from the CLI probes.
        engine = self.engine_status()["engine"] if mode in {"chat", "new"} else {}
        if self.use_core and engine.get("typed_api_failed"):
            raise ValueError(f"{engine.get('detail')}. Nothing was started: no chat session, task or project.")
        if mode == "chat" and self.use_core and not engine.get("typed_api"):
            # The desktop's own Python package may contain assistant.py even
            # when the installed engine is older. Do not allocate a fake chat
            # job or return a scripted greeting in place of a real model reply.
            if not engine.get("conversation", False):
                version = engine.get("version") or "unknown"
                raise ValueError(
                    f"Conversation is not available in installed Coding Brain v{version}. "
                    "No chat session or coding task was started. "
                    "Install a verified conversational release to use Chat. "
                    "You can still open registered projects in Projects."
                )
        if mode == "new" and not engine.get("typed_api"):
            # Feature checks must refer to the INSTALLED engine, not an importable
            # copy of the UI's Python package. Never create a job on older engines.
            if not engine.get("new_project", False):
                version = engine.get("version") or "unknown"
                raise ValueError(
                    f"New-project creation is unavailable in installed Coding Brain v{version}. "
                    "No task started and no files were created. "
                    "Use Browse folder to open an existing project, or install a verified "
                    "Coding Brain release that supports 'codingbrain new'."
                )
        # Never treat chat as a coding task. Newer CLIs provide chat; older ones
        # return a clear unsupported-command error instead of running code.
        command = [*self.cli_command(), "chat" if mode == "chat" else "new" if mode == "new" else "run", body.message]
        with self.lock:
            if self.active and self.jobs[self.active].status in {"starting", "running", "approval_required"}:
                raise ValueError("Another task is active; finish or stop it first")
            job = Job(mode, str(self.project) if self.project else None, command)
            job.attachments = attachments
            self.jobs[job.id] = job
            self.active = job.id
        # Chat, new projects and tasks go through the installed engine's typed API (one transport,
        # the engine's own approval rules). Without it, nothing is faked: no canned greeting, and
        # protected execution does not start.
        if self.use_core:
            status = self.engine_status()
            needed = REQUIRED_OPERATIONS[mode]
            missing = [op for op in needed if op not in (status["engine"].get("operations") or [])]
            if status["engine"].get("typed_api") and missing:
                del self.jobs[job.id]
                if self.active == job.id:
                    self.active = None
                raise ValueError(f"The installed engine (v{status['engine'].get('version') or '?'}) does not support "
                                 f"{'creating projects' if mode == 'new' else 'this action'} ({', '.join(missing)} missing). "
                                 "Update Coding Brain; nothing was started.")
            if attachments and not status["engine"].get("attachments"):
                del self.jobs[job.id]
                if self.active == job.id:
                    self.active = None
                raise ValueError(f"The installed engine (v{status['engine'].get('version') or '?'}) cannot receive "
                                 "attachments from the desktop. Update Coding Brain, or send without files. "
                                 "Nothing was started.")
            if status["engine"].get("typed_api"):
                from .core import run as core_run, chat as core_chat, create_new
                job.core = True
                job.engine = self.engine_client()
                job.on_project_created = lambda p: self.choose(p)
                target = core_run if mode == "run" else create_new if mode == "new" else core_chat
                threading.Thread(target=target, args=(job,), daemon=True).start()
                return job
            if mode in {"new", "run"} or attachments or not status["engine"].get("conversation"):
                del self.jobs[job.id]
                if self.active == job.id:
                    self.active = None
                raise ValueError(f"{status['title']}. {status['detail']} Nothing was started.")
        threading.Thread(target=self._execute, args=(job,), daemon=True).start()
        return job

    def _execute(self, job: Job):
        job.status = "running"
        job.emit("start", f"{job.kind.capitalize()} started")
        try:
            popen_opts = {"cwd": job.project or str(self.home), "stdin": subprocess.PIPE,
                          "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                          "text": True, "bufsize": 1, "errors": "replace",
                          "env": {**os.environ, "PYTHONUNBUFFERED": "1"}}
            process = subprocess.Popen(job.command, **popen_opts)
            job.process = process
            # Input prompts have no trailing newline. Detect them without pretending
            # they are approvals until the actual CLI requests approval.
            buffer = ""
            prompt_tail = ("[y/N] ", "[Y/n] ")
            while True:
                char = process.stdout.read(1)
                if not char:
                    break
                buffer += char
                if char == "\n" or any(buffer.endswith(tail) for tail in prompt_tail):
                    message = buffer.rstrip("\r\n")
                    job.emit("output", message)
                    if any(message.endswith(tail) for tail in prompt_tail):
                        job.awaiting_approval = True
                        job.status = "approval_required"
                        job.emit("approval", message)
                    buffer = ""
            if buffer:
                job.emit("output", buffer)
            job.returncode = process.wait()
            if job.status != "cancelled":
                job.status = "completed" if job.returncode == 0 else "failed"
                job.emit("finish", f"Process exited with code {job.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            job.status = "failed"
            job.error = str(exc)
            job.emit("error", "Coding Brain executable unavailable or could not start")
        finally:
            job.awaiting_approval = False

    def decide(self, job_id: str, allow: bool, approval_id: str | None = None):
        job = self.jobs[job_id]
        if job.core:
            if not job.awaiting_approval or not job.pending_id or job.decision_event.is_set():
                raise ValueError("No approval is pending")
            if approval_id != job.pending_id:
                raise ValueError("Approval is stale or does not match the current request")
            job.decision_allow = allow
            job.decision_event.set()
            return
        if not job.awaiting_approval or not job.process or job.process.poll() is not None:
            raise ValueError("No approval is pending")
        job.process.stdin.write("y\n" if allow else "n\n")
        job.process.stdin.flush()
        job.awaiting_approval = False
        job.status = "running"
        job.emit("decision", "Approved" if allow else "Declined")

    def stop(self, job_id: str):
        job = self.jobs[job_id]
        if job.core:
            if job.status not in {"starting", "running", "approval_required"}:
                raise ValueError("Task is not running")
            job.stop_requested.set()
            job.decision_event.set()
            if job.cancel_core is not None and not job.awaiting_approval:
                try:
                    job.cancel_core()  # The actual Brain.cancel stores an engine cancellation request.
                except ValueError:
                    pass
            job.emit("stage", "Cancellation requested through Coding Brain; waiting for a safe boundary")
            return
        if not job.process or job.process.poll() is not None:
            raise ValueError("Task is not running")
        job.status = "cancelled"
        job.process.terminate()
        job.emit("finish", "Stop requested")


def create_app(state: Workspace | None = None) -> FastAPI:
    state = state or Workspace()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="Coding Brain Desktop Bridge")
    app.state.workspace = state

    def auth(x_codingbrain_token: str | None = Header(default=None)):
        if not x_codingbrain_token or not secrets.compare_digest(x_codingbrain_token, state.secret):
            raise HTTPException(403, "Not authorized")

    @app.get("/")
    def index():
        response = FileResponse(ASSETS / "index.html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                                                       "worker-src 'self'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; "
                                                       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.get("/app.css")
    def css():
        return FileResponse(ASSETS / "app.css", media_type="text/css")

    @app.get("/vendor/{asset:path}")
    def vendor(asset: str):
        """Bundled third-party editor files (Monaco), fetched at build time with a pinned checksum."""
        base = (ASSETS / "vendor").resolve()
        target = (base / asset).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(404, "Not found")
        response = FileResponse(target)
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.get("/app.js")
    def js():
        return FileResponse(ASSETS / "app.js", media_type="text/javascript")

    @app.get("/api/state", dependencies=[Depends(auth)])
    def initial():
        active = state.jobs.get(state.active) if state.active else None
        return {"project": {"name": state.project.name, "path": str(state.project),
                            "git": state.git_branch(state.project)} if state.project else None,
                "projects": state.projects(), "active": active.view() if active else None}

    @app.get("/api/health", dependencies=[Depends(auth)])
    def health():
        return state.health()

    @app.get("/api/engine/status", dependencies=[Depends(auth)])
    def engine_status(refresh: bool = False):
        return state.engine_status(refresh=refresh)

    @app.get("/api/updates/status", dependencies=[Depends(auth)])
    def update_status(channel: str = "stable"):
        try:
            return state.update_status(channel)
        except (OSError, ValueError) as error:
            raise HTTPException(503, f"Could not check GitHub's stable releases: {type(error).__name__}") from error

    @app.get("/api/setup", dependencies=[Depends(auth)])
    def setup_status():
        return {"readiness":state.installation_status(), "providers":state.configured_providers()}

    @app.post("/api/setup/completed", dependencies=[Depends(auth)])
    def set_onboarding(body: Onboarding):
        state.save_first_run(body.completed)
        return {"ok":True,"onboarding_completed":state.first_run_completed()}

    @app.post("/api/system/action", dependencies=[Depends(auth)])
    def system_action(action: str, body: ConfirmAction, channel: str = "stable"):
        if not body.confirmed:
            raise HTTPException(409, "Confirm the operation before starting it")
        try:
            return state.maintenance(action) if channel == "stable" else state.maintenance(action, channel=channel)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/providers/{provider_id}/signin", dependencies=[Depends(auth)])
    def provider_signin(provider_id: str, body: ConfirmAction):
        if not body.confirmed:
            raise HTTPException(409, "Sign-in requires explicit confirmation")
        try:
            state.sign_in(provider_id)
            return {"opened_terminal":True,"message":"Complete sign-in in the official provider CLI and refresh provider status"}
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/providers/{provider_id}/configure", dependencies=[Depends(auth)])
    def provider_configure(provider_id: str, body: ProviderSetting):
        try:
            state.configure_provider(provider_id, body.enabled)
            return {"ok":True,"message":"Supervisor routing setting saved by Coding Brain"}
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/project", dependencies=[Depends(auth)])
    def select(body: ProjectChoice):
        try:
            return state.choose(body.path)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/project/pick", dependencies=[Depends(auth)])
    def pick():
        try:
            from tkinter import Tk, filedialog
            root = Tk(); root.withdraw(); root.attributes("-topmost", True)
            try:
                choice = filedialog.askdirectory(title="Choose Coding Brain project")
            finally:
                root.destroy()
            return state.choose(choice) if choice else {"cancelled": True}
        except Exception as e:
            raise HTTPException(503, "Native folder picker unavailable; enter a path instead") from e

    @app.post("/api/attachments/pick", dependencies=[Depends(auth)])
    def pick_attachments():
        try:
            from tkinter import Tk, filedialog
            root = Tk(); root.withdraw(); root.attributes("-topmost", True)
            try:
                chosen = filedialog.askopenfilenames(title="Attach files for Coding Brain")
            finally:
                root.destroy()
        except Exception as e:
            raise HTTPException(503, "Native file picker unavailable; paste or drop files instead") from e
        files, refused = [], []
        for item in list(chosen or [])[:MAX_ATTACHMENTS]:
            try:
                files.append(state.attachment_view(state.check_attachment(item)))
            except ValueError as error:
                refused.append(str(error))
        return {"files": files, "refused": refused}

    @app.post("/api/attachments/upload", dependencies=[Depends(auth)])
    async def upload_attachment(request: Request, x_filename: str = Header(default="pasted.png")):
        if int(request.headers.get("content-length") or 0) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, "Attachment larger than 25 MB")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise HTTPException(413, "Attachment larger than 25 MB")
        try:
            from urllib.parse import unquote
            return state.stage_upload(unquote(x_filename), bytes(data))
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/git/snapshot", dependencies=[Depends(auth)])
    def git_snapshot():
        """Read-only fresh Git state for the selected project; no path input accepted."""
        from .git_inspection import git_snapshot as inspect_git
        try:
            return inspect_git(state.project)
        except (ValueError, OSError) as error:
            raise HTTPException(503, "Local Git state unavailable; retry the live refresh") from error

    @app.get("/api/tree", dependencies=[Depends(auth)])
    def tree(path: str = ""):
        try:
            return {"entries": state.tree(path)}
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/conversation", dependencies=[Depends(auth)])
    def conversation():
        from .engine_client import EngineError
        try:
            return state.conversation()
        except (EngineError, ValueError, OSError) as error:
            raise HTTPException(503, f"Conversation history unavailable: {str(error)[:200]}") from error

    @app.post("/api/documents/open", dependencies=[Depends(auth)])
    def open_document(body: OpenFile, reveal: bool = False):
        try:
            return state.open_document(body.path, reveal)
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/api/conversation/reset", dependencies=[Depends(auth)])
    def conversation_reset():
        from .engine_client import EngineError
        try:
            return state.reset_conversation()
        except (EngineError, ValueError, OSError) as error:
            raise HTTPException(409, str(error)[:300]) from error

    @app.post("/api/file", dependencies=[Depends(auth)])
    def save_file(body: SaveFile):
        try:
            return state.save(body.path, body.content, body.sha256)
        except FileChangedError as e:
            raise HTTPException(409, str(e)) from e
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/file/open-external", dependencies=[Depends(auth)])
    def open_external(body: OpenFile):
        try:
            return state.open_external(body.path)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/jobs/{job_id}/diff", dependencies=[Depends(auth)])
    def job_diff(job_id: str):
        try:
            return {"files": state.job_diff(job_id)}
        except KeyError:
            raise HTTPException(404, "Unknown task")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/file", dependencies=[Depends(auth)])
    def file(path: str):
        try:
            return state.preview(path)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/start", dependencies=[Depends(auth)])
    def start(body: StartRequest):
        try:
            return state.start(body).view()
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def job_status(job_id: str, after: int = 0):
        if job_id not in state.jobs:
            raise HTTPException(404, "Unknown task")
        job = state.jobs[job_id]
        return {"job": job.view(), "events": job.dump(after)}

    @app.post("/api/jobs/{job_id}/decision", dependencies=[Depends(auth)])
    def decision(job_id: str, body: Decision):
        try:
            state.decide(job_id, body.allow, body.approval_id)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "Unknown task")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/jobs/{job_id}/stop", dependencies=[Depends(auth)])
    def stop(job_id: str):
        try:
            state.stop(job_id)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "Unknown task")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Coding Brain workspace UI")
    parser.add_argument("--port", type=int, default=0, help="Loopback TCP port (0 = random)")
    parser.add_argument("--no-open", action="store_true", help="Do not open browser")
    parser.add_argument("--handshake-stdout", action="store_true", help="Emit machine-readable one-use desktop startup handshake")
    options = parser.parse_args(argv)
    import socket
    import uvicorn
    state = Workspace()
    app = create_app(state)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", options.port)); sock.listen(128)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/#token={state.secret}"
    if options.handshake_stdout:
        # Only the parent desktop process receives this pipe. Never log its token.
        print("CBUI_READY " + json.dumps({"port": port, "token": state.secret, "pid": os.getpid()}), flush=True)
    else:
        print("Coding Brain workspace UI - listening on 127.0.0.1 only", flush=True)
        print("Open this URL in your own browser:", url, flush=True)
    if not options.no_open:
        webbrowser.open(url)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
