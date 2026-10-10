"""Loopback-only Coding Brain workspace UI.

A deliberately small presentation bridge.  Coding Brain remains the sole coding
executor; this module never loads its Brain service or modifies task state.
"""
from __future__ import annotations

import argparse
import collections
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

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import management

ASSETS = Path(__file__).with_name("static")
IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", "build", ".idea", ".vscode"}
SENSITIVE = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
MAX_PREVIEW_BYTES = 128_000
MAX_ENTRIES = 350


def sensitive_name(name: str) -> bool:
    lower = name.lower()
    return (lower in SENSITIVE or lower.startswith('.env') or lower.endswith(('.pem', '.p12', '.pfx', '.key'))
            or lower in {'known_hosts', 'authorized_keys', 'secret', 'secrets'})


class ProjectChoice(BaseModel):
    path: str = Field(min_length=1, max_length=8192)


class StartRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16000)
    mode: str = "chat"


class ConfirmAction(BaseModel):
    confirmed: bool = False


class Onboarding(BaseModel):
    completed: bool


class ProviderSetting(BaseModel):
    enabled: bool


class Decision(BaseModel):
    allow: bool
    approval_id: str | None = None


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

    def emit(self, kind: str, message: str):
        with self.lock:
            self.cursor += 1
            self.events.append({"seq": self.cursor, "kind": kind, "message": message, "at": time.time(), "job_id": self.id})

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


class Workspace:
    def __init__(self, home: Path | None = None, executable: str | None = None):
        self._home_override = home is not None
        self.home = (home or Path.home()).resolve()
        self.executable = executable
        self.secret = secrets.token_urlsafe(32)
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

    def engine_status(self, refresh: bool = False) -> dict:
        """Truthful installed-engine/model readiness; the local UI is a separate process.

        Cache cheap probes briefly so a slow provider CLI doesn't freeze chat or activity.
        """
        now = time.monotonic()
        cached = getattr(self, "_engine_status_cache", None)
        if not refresh and cached and now - cached[0] < 20:
            return cached[1]
        try:
            command = self.cli_command()
        except ValueError:
            command = None
        core = management.engine_capabilities(command)
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
        self._engine_status_cache = (now, report)
        return report

    def installation_status(self) -> dict:
        try:
            cli = self.cli_command()
        except ValueError:
            cli = None
        report = management.installation_probes(cli)
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
        local = self.engine_status()["model"]
        for provider in providers:
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

    def maintenance(self, action: str) -> dict:
        cli = self.cli_command()
        deep_doctor = management.supports_cli_option(cli, "doctor", "--full") if action == "doctor" else True
        command = management.maintenance_args(action, cli, deep_doctor=deep_doctor)
        if action in {"install_full", "update_engine", "repair"}:
            # New console owns prompts, elevation and restarts. No silent consent.
            management.launch_interactive_windows(command)
            return {"opened_terminal":True,"message":"The official Coding Brain workflow opened in a terminal. Follow its prompts; refresh this screen afterward."}
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
        return {"name": path.name, "path": str(path), "git": self.git_branch(path)}

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
        if not path.is_file() or path.stat().st_size > MAX_PREVIEW_BYTES:
            raise ValueError("Preview limited to text files smaller than 128 KB")
        raw = path.read_bytes()
        if b"\0" in raw:
            raise ValueError("Binary files cannot be previewed")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Preview supports UTF-8 text only") from None
        return {"path": relative, "content": content, "bytes": len(raw)}

    def start(self, body: StartRequest) -> Job:
        mode = body.mode
        if mode not in {"chat", "run", "new"}:
            raise ValueError("Unknown mode")
        if mode == "run" and (not self.project or not self.git_branch(self.project)):
            raise ValueError("Select an existing Git repository to run a coding task")
        # Never treat chat as a coding task. Newer CLIs provide chat; older ones
        # return a clear unsupported-command error instead of running code.
        command = [*self.cli_command(), "chat" if mode == "chat" else "new" if mode == "new" else "run", body.message]
        with self.lock:
            if self.active and self.jobs[self.active].status in {"starting", "running", "approval_required"}:
                raise ValueError("Another task is active; finish or stop it first")
            job = Job(mode, str(self.project) if self.project else None, command)
            self.jobs[job.id] = job
            self.active = job.id
        # Typed service calls preserve the existing engine's permission model.
        # Fall back to the legacy CLI only on machines without an importable brain;
        # it cannot approve protected actions non-interactively.
        if self.use_core:
            try:
                from .core import supported, run as core_run, chat as core_chat, create_new
                if supported():
                    job.core = True
                    job.on_project_created = lambda p: self.choose(p)
                    target = core_run if mode == "run" else create_new if mode == "new" else core_chat
                    threading.Thread(target=target, args=(job,), daemon=True).start()
                    return job
            except ImportError:
                pass
        if mode in {"new", "run"} and self.use_core:
            del self.jobs[job.id]
            if self.active == job.id:
                self.active = None
            raise ValueError("The installed Coding Brain engine is unavailable; protected execution did not start")
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
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.get("/app.css")
    def css():
        return FileResponse(ASSETS / "app.css", media_type="text/css")

    @app.get("/app.js")
    def js():
        return FileResponse(ASSETS / "app.js", media_type="text/javascript")

    @app.get("/api/state", dependencies=[Depends(auth)])
    def initial():
        active = state.jobs.get(state.active) if state.active else None
        return {"project": {"name": state.project.name, "path": str(state.project),
                            "git": state.git_branch(state.project)} if state.project else None,
                "projects": state.projects(), "active": active.view() if active else None}

    @app.get("/api/engine/status", dependencies=[Depends(auth)])
    def engine_status(refresh: bool = False):
        return state.engine_status(refresh=refresh)

    @app.get("/api/setup", dependencies=[Depends(auth)])
    def setup_status():
        return {"readiness":state.installation_status(), "providers":state.configured_providers()}

    @app.post("/api/setup/completed", dependencies=[Depends(auth)])
    def set_onboarding(body: Onboarding):
        state.save_first_run(body.completed)
        return {"ok":True,"onboarding_completed":state.first_run_completed()}

    @app.post("/api/system/action", dependencies=[Depends(auth)])
    def system_action(action: str, body: ConfirmAction):
        if not body.confirmed:
            raise HTTPException(409, "Confirm the operation before starting it")
        try:
            return state.maintenance(action)
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

    @app.get("/api/tree", dependencies=[Depends(auth)])
    def tree(path: str = ""):
        try:
            return {"entries": state.tree(path)}
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

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
