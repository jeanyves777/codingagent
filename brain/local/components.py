"""What a complete Coding Brain environment needs, how to check each part and how to get it.

Each component reports a state from a real check (a command that ran, a server that answered, a
model that generated text, a container that started), never from the mere presence of a file:
  ready          works now (with `verified` when a deep check ran in this check)
  missing        not installed
  stopped        installed but not running (Docker engine, Ollama server)
  needs_sign_in  installed but not signed in, signed in with API-key billing, or expired
  needs_restart  installed, waiting for a Windows restart
  unavailable    temporarily unreachable (network, rate limits)
  failed         installed but its working check failed
  waiting        needs another component first
  unsupported    not automated on this system; instructions are given instead
Installs use winget or the vendor's official channel (npm registry, ollama pull, docker build from
the bundled Dockerfiles); nothing is bundled, no remote script is executed and no security
prompt is suppressed. Package agreements are accepted only after the user agreed to the plan
that showed them.
"""
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import config as settings

GB = 1024 ** 3


@dataclass
class Status:
    state: str
    detail: str = ""
    version: str | None = None
    verified: bool = False
    action: str | None = None  # what the installer would do: install, start, pull, build, sign_in, manual, restart
    data: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value not in (None, {}, "")} | {"state": self.state}


class Env:
    """What a component check or install can use."""

    def __init__(self, system, layout, config: dict, progress=None, evidence: dict | None = None):
        self.system, self.layout, self.config = system, layout, config
        self.progress = progress or (lambda text: None)
        self.evidence = evidence if evidence is not None else {}
        self.results: dict[str, Status] = {}

    def save_config(self):
        settings.save(self.layout, self.config)


class Component:
    id = ""
    title = ""
    profiles = ("full",)
    optional = False          # declining it is a choice, not a problem
    admin = False             # needs administrator rights (Windows asks through UAC)
    size = ""                 # approximate download
    source = ""               # where it comes from
    license = ""              # terms the user should know about before agreeing
    requires: tuple = ()
    winget_id = ""
    winget_scope = "user"
    program = ""              # the installed executable whose signature is recorded

    def check(self, env: Env, deep: bool = False) -> Status:
        raise NotImplementedError

    def install(self, env: Env, status: Status) -> Status:
        if status.action == "install" and self.winget_id:
            return self.winget(env)
        return status

    def plan_lines(self, env: Env, status: Status) -> list[str]:
        lines = [f"{self.title}: {ACTIONS.get(status.action, status.action)}"]
        if status.action in {"install", "pull", "build"}:
            if self.source:
                lines.append(f"  source: {self.source}")
            if self.size:
                lines.append(f"  download: {self.size}")
            if self.admin:
                lines.append("  needs administrator rights: Windows will ask you (UAC)")
            if self.license:
                lines.append(f"  terms: {self.license}")
        return lines

    # helpers -----------------------------------------------------------------------------------
    def winget(self, env: Env, package: str | None = None) -> Status:
        system = env.system
        package = package or self.winget_id
        if system.platform != "windows":
            return Status("unsupported", f"install {self.title} with your system's package manager", action="manual")
        if not system.which("winget"):
            return Status("unsupported", "winget is not available: install 'App Installer' from the Microsoft Store, "
                          f"or install {self.title} from {self.source}", action="manual")
        arguments = ["winget", "install", "--exact", "--id", package, "--source", "winget",
                     "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
        if self.winget_scope:
            arguments += ["--scope", self.winget_scope]
        code, output = system.run(arguments, timeout=3600, on_line=env.progress)
        if code and self.winget_scope == "user" and "scope" in output.lower():
            code, output = system.run(arguments[:-2], timeout=3600, on_line=env.progress)  # no per-user package
        system.refresh_path()
        after = self.check(env, deep=True)
        if not after.ready and after.state not in {"stopped", "needs_sign_in", "needs_restart"}:
            after.detail = f"winget exited {code}: {output.strip()[-400:]}" if code else after.detail
        if self.program and after.state != "missing":
            try:
                signed = self.integrity(env, self.program)
            except IntegrityError as error:
                return Status("failed", str(error), action="manual")
            after.data["signature"] = signed or "not signed or not checked"
        return after

    def integrity(self, env: Env, program: str) -> str:
        """Record the publisher signature of an installed program; a tampered binary is refused."""
        path = env.system.which(program)
        if not path or env.system.platform != "windows":
            return ""
        signature = env.system.signature(path)
        if signature.get("status") in {"HashMismatch", "NotTrusted"}:
            raise IntegrityError(f"{path}: signature {signature['status']}; it was not run. Reinstall {self.title}.")
        return f"signed: {signature.get('signer') or signature.get('status')}" if signature.get("status") == "Valid" else ""


class IntegrityError(RuntimeError):
    pass


ACTIONS = {"install": "install", "start": "start it", "pull": "download the model", "build": "build the images",
           "sign_in": "sign in with your subscription (official flow)", "manual": "needs you (instructions below)",
           "restart": "restart Windows, then resume", None: "nothing to do"}


def version_line(env: Env, arguments: list[str]) -> str | None:
    code, output = env.system.run(arguments, timeout=30)
    return output.strip().splitlines()[0][:120] if code == 0 and output.strip() else None


# Core -------------------------------------------------------------------------------------------

class Python(Component):
    id, title, profiles = "python", "Python 3.11+", ("local", "full")
    source = "winget Python.Python.3.12 (installed by install.ps1 before Coding Brain itself)"

    def check(self, env, deep=False):
        import sys
        ok = sys.version_info >= (3, 11)
        return Status("ready" if ok else "failed", sys.executable, sys.version.split()[0], verified=ok,
                      action=None if ok else "manual")


class Git(Component):
    id, title, profiles = "git", "Git", ("local", "full")
    program, winget_id, source, size = "git", "Git.Git", "winget Git.Git (Git for Windows)", "about 65 MB"

    def check(self, env, deep=False):
        if not env.system.which("git"):
            return Status("missing", "not found", action="install" if env.system.platform == "windows" else "manual")
        line = version_line(env, ["git", "--version"])
        return Status("ready" if line else "failed", "git runs" if line else "git is on PATH but does not run",
                      line, verified=bool(line), action=None if line else "install")


# Virtualization, WSL 2 and Docker ------------------------------------------------------------------

class WSL(Component):
    id, title = "wsl", "WSL 2 (Windows Subsystem for Linux)"
    admin, size = True, "about 200 MB"
    source = "Microsoft: wsl --install --no-distribution (Docker Desktop provides its own Linux environment)"

    def check(self, env, deep=False):
        system = env.system
        if system.platform != "windows":
            return Status("ready", "not needed on this system")
        from .system import assess_virtualization
        if env.evidence.get("pending_restart") == self.id:
            return Status("needs_restart", "WSL was installed; Windows must restart before it works", action="restart")
        environment = {**system.environ(), "WSL_UTF8": "1"}
        code, output = system.run(["wsl", "--version"], timeout=60, env=environment) if system.which("wsl") else (127, "")
        working = code == 0
        status_code, status_text = system.run(["wsl", "--status"], timeout=60, env=environment) if working else (code, output)
        docker_linux = (env.results.get("docker") or Status("missing")).ready
        virtualization = assess_virtualization(system.facts(), wsl_working=working and status_code == 0,
                                               docker_linux=docker_linux)
        data = {"virtualization": virtualization}
        if working and status_code == 0:
            return Status("ready", "WSL 2 is installed and working", output.strip().splitlines()[0][:80] if output else None,
                          verified=True, data=data)
        if working:
            return Status("failed", f"WSL is installed but not working: {status_text.strip()[-300:]}"
                          + (f" {virtualization.get('advice', '')}" if virtualization["state"] != "enabled" else ""),
                          action="install", data=data)
        build = system.facts().get("build") or 0
        if build and build < 19041:
            return Status("unsupported", f"Windows build {build} is too old for WSL 2 (needs 19041 or newer; "
                          "update Windows first)", action="manual", data=data)
        return Status("missing", "WSL is not installed" + (f". {virtualization['advice']}"
                                                            if virtualization["state"] == "possibly_disabled" else ""),
                      action="install", data=data)

    def install(self, env, status):
        code = env.system.run_elevated(["wsl.exe", "--install", "--no-distribution"])
        if code == 1223 or code == 1:  # the UAC prompt was declined or the command failed
            return Status("failed", f"wsl --install failed or was not approved (exit {code}); it needs administrator "
                          "approval in the Windows prompt", action="install")
        after = self.check(env, deep=True)
        if after.ready:
            return after
        env.evidence["pending_restart"] = self.id  # Virtual Machine Platform becomes active after a restart
        return Status("needs_restart", f"WSL installed (exit {code}); restart Windows to finish", action="restart")


DOCKER_DESKTOP = r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"


class Docker(Component):
    id, title = "docker", "Docker Desktop"
    requires = ("wsl",)
    program = "docker"
    winget_id, winget_scope, admin, size = "Docker.DockerDesktop", None, True, "about 600 MB"
    source = "winget Docker.DockerDesktop (Docker Inc.)"
    license = ("Docker Desktop is free for personal use, education, non-commercial open source and businesses "
               "with fewer than 250 employees and less than $10 million revenue; larger organizations need a paid "
               "Docker subscription. On first start Docker Desktop shows its own agreement; Coding Brain never "
               "accepts it for you.")

    def check(self, env, deep=False):
        system = env.system
        if not system.which("docker"):
            if system.platform == "windows" and system.exists(DOCKER_DESKTOP):
                return Status("stopped", "Docker Desktop is installed but has not started yet", action="start")
            return Status("missing", "Docker is not installed", action="install" if system.platform == "windows" else "manual")
        version = version_line(env, ["docker", "--version"])
        code, output = system.run(["docker", "version", "--format", "{{.Server.Os}} {{.Server.Version}}"], timeout=30)
        server = output.strip().split()
        if code == 0 and server and server[0] == "linux":
            return Status("ready", f"engine {server[-1]} running Linux containers", version, verified=True)
        if code == 0 and server and server[0] == "windows":
            return Status("failed", "Docker is using Windows containers; switch to Linux containers (Docker Desktop "
                          "tray icon > Switch to Linux containers)", version, action="manual")
        startable = system.platform == "windows" and system.exists(DOCKER_DESKTOP)
        return Status("stopped", "the Docker command is installed but its engine is not running"
                      + ("" if startable else ": start Docker (e.g. `sudo systemctl start docker`)"),
                      version, action="start" if startable else "manual")

    def install(self, env, status):
        if status.action == "install":
            status = self.winget(env)
            if status.ready or status.state != "stopped":
                return status
        return self.start(env)

    def start(self, env, timeout: float = 300) -> Status:
        system = env.system
        if not system.start_detached([os.path.expandvars(DOCKER_DESKTOP)]):
            return Status("failed", "Docker Desktop could not be started", action="manual")
        started = system.time()
        while system.time() - started < timeout:
            system.sleep(5)
            status = self.check(env)
            if status.ready:
                return status
            env.progress(f"waiting for the Docker engine: {int(system.time() - started)} s "
                         "(if Docker Desktop shows its agreement or a sign-in, answer it there)")
        return Status("stopped", f"Docker Desktop did not start its engine within {int(timeout)} s. Open Docker "
                      "Desktop, accept its terms if asked, then run `codingbrain install --resume`.", action="start")


class Sandbox(Component):
    id, title = "sandbox", "Coding Brain test sandbox images"
    requires = ("docker",)
    size = "about 250 MB (python:3.11-slim and node:22-slim from Docker Hub)"
    source = "built locally from Coding Brain's bundled Dockerfiles"

    def images(self, env):
        return [("python", env.config["sandbox"]["python_image"], ["python", "-c", "print('sandbox ok')"]),
                ("node", env.config["sandbox"]["node_image"], ["node", "-e", "console.log('sandbox ok')"])]

    def check(self, env, deep=False):
        missing, ids = [], {}
        for _, image, _ in self.images(env):
            code, output = env.system.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=30)
            if code:
                missing.append(image)
            else:
                ids[image] = output.strip()
        if missing:
            return Status("missing", "not built: " + ", ".join(missing), action="build")
        cached = env.evidence.get("sandbox") or {}
        if not deep:
            same = cached.get("images") == ids
            if same and not cached.get("ok"):
                return Status("failed", f"images present but the last smoke test failed ({cached.get('when')})",
                              action="build")
            return Status("ready", "images present; " + (f"smoke test passed {cached.get('when')}" if same else
                                                         "smoke test not run yet (codingbrain doctor --full)"),
                          verified=same, data={} if same else {"unverified": True})
        failures = []
        for _, image, command in self.images(env):
            code, output = env.system.run(["docker", "run", "--rm", "--network", "none", "--memory", "512m",
                                           "--pids-limit", "128", image, *command], timeout=180)
            if code or "sandbox ok" not in output:
                failures.append(f"{image}: exit {code} {output.strip()[-200:]}")
        env.evidence["sandbox"] = {"ok": not failures, "images": ids, "when": stamp(env)}
        if failures:
            return Status("failed", "smoke test failed: " + "; ".join(failures), action="build")
        return Status("ready", "both images start offline (--network none) and run a command", verified=True)

    def install(self, env, status):
        assets = Path(__file__).parent / "assets"
        for dockerfile, image in (("Dockerfile.sandbox", env.config["sandbox"]["python_image"]),
                                  ("Dockerfile.sandbox.node", env.config["sandbox"]["node_image"])):
            code, output = env.system.run(["docker", "build", "-t", image, "-f", str(assets / dockerfile), str(assets)],
                                          timeout=1800, on_line=env.progress)
            if code:
                return Status("failed", f"building {image} failed: {output.strip()[-400:]}", action="build")
        return self.check(env, deep=True)


# Local models -------------------------------------------------------------------------------------

class Ollama(Component):
    id, title, profiles = "ollama", "Ollama (local model server)", ("local", "full")
    program = "ollama"
    winget_id, size, source = "Ollama.Ollama", "about 700 MB", "winget Ollama.Ollama (ollama.com)"

    def url(self, env):
        return env.config["models"]["url"].rstrip("/")

    def check(self, env, deep=False):
        system = env.system
        code, body = system.http("GET", self.url(env) + "/api/version", timeout=5)
        if code == 200:
            return Status("ready", f"server answering at {self.url(env)}", (body or {}).get("version")
                          if isinstance(body, dict) else None, verified=True)
        if not system.which("ollama"):
            action = "install" if system.platform == "windows" else "manual"
            return Status("missing", "Ollama is not installed" + ("" if action == "install" else
                                                                   ": see https://ollama.com/download"), action=action)
        return Status("stopped", f"installed, but nothing answers at {self.url(env)}", version_line(env, ["ollama", "--version"]),
                      action="start")

    def install(self, env, status):
        if status.action == "install":
            status = self.winget(env)
            if status.state != "stopped":
                return status
        env.system.start_detached(["ollama", "serve"])
        for _ in range(30):
            env.system.sleep(1)
            status = self.check(env)
            if status.ready:
                return status
        return Status("stopped", "Ollama did not start; open the Ollama app, then run `codingbrain install --resume`",
                      action="start")


# name, download size, memory it needs (GB), what it is for
CODING_MODELS = [("qwen2.5-coder:1.5b", 1.0, 4), ("qwen2.5-coder:3b", 1.9, 8), ("qwen2.5-coder:7b", 4.7, 14),
                 ("qwen2.5-coder:14b", 9.0, 28)]
VISION_MODELS = [("qwen2.5vl:3b", 3.2, 8), ("qwen2.5vl:7b", 6.0, 16)]


def recommend(models: list, ram: float | None, vram: float | None = None) -> tuple[str, float, int]:
    """The largest model that fits comfortably in memory (GPU memory counts when larger)."""
    budget, gpu = (ram or 8 * GB) / GB, (vram or 0) / GB
    fitting = [item for item in models if item[2] <= budget + 0.5 or gpu >= item[1] * 1.3 + 1]
    return fitting[-1] if fitting else models[0]


def stamp(env) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(env.system.time()))


class Model(Component):
    id, title, profiles = "model", "Local coding model", ("local", "full")
    requires = ("ollama",)
    source = "Ollama library (ollama pull; Ollama verifies each layer's SHA-256 digest)"
    kind, models, config_key = "coding", CODING_MODELS, ("models", "model")

    def wanted(self, env) -> str:
        section, key = self.config_key
        chosen = env.config[section].get(key)
        if chosen:
            return chosen
        facts = env.system.facts()
        return recommend(self.models, facts.get("ram"), (env.system.gpu() or {}).get("vram"))[0]

    def plan_lines(self, env, status):
        lines = super().plan_lines(env, status)
        if status.action == "pull":
            facts = env.system.facts()
            lines.insert(1, f"  model: {self.wanted(env)} (chosen for {facts.get('ram', 0) / GB:.0f} GB of memory); "
                         "alternatives: " + ", ".join(f"{name} ~{size} GB" for name, size, _ in self.models))
        return lines

    @property
    def size_of(self):
        return {name: size for name, size, _ in self.models}

    def check(self, env, deep=False):
        name = self.wanted(env)
        self.size = f"about {self.size_of.get(name, '?')} GB"
        url = env.config["models"]["url"].rstrip("/") if self.kind == "coding" else settings.vision_settings(env.config)["url"].rstrip("/")
        code, body = env.system.http("GET", url + "/api/tags", timeout=10)
        if code != 200:
            return Status("waiting", "Ollama is not answering", action=None)
        names = [item.get("name", "") for item in (body or {}).get("models", [])] if isinstance(body, dict) else []
        present = name in names or f"{name}:latest" in names
        if not present:
            return Status("missing", f"{name} is not downloaded", action="pull", data={"model": name})
        evidence = (env.evidence.get("generation") or {}).get(name)
        if not deep:
            if evidence and evidence.get("ok"):
                return Status("ready", f"{name}: generated text {evidence['when']} ({evidence['detail']})",
                              verified=True, data={"model": name})
            return Status("ready", f"{name} downloaded; generation not tested yet (codingbrain doctor --full)",
                          data={"model": name, "unverified": True})
        return self.generate(env, url, name)

    def generate(self, env, url, name) -> Status:
        """Proof the model runs: a real generation request, not the presence of a download."""
        payload = {"model": name, "prompt": "Reply with the single word READY.", "stream": False,
                   "options": {"num_predict": 16, "temperature": 0}}
        started = env.system.time()
        env.progress(f"{name}: loading the model and generating a short reply (the first load can take a minute)")
        code, body = env.system.http("POST", url + "/api/generate", payload, timeout=600)
        seconds = env.system.time() - started
        text = (body or {}).get("response", "") if isinstance(body, dict) else ""
        tokens = (body or {}).get("eval_count", 0) if isinstance(body, dict) else 0
        ok = code == 200 and bool(text.strip()) and tokens > 0
        detail = (f"{tokens} tokens in {seconds:.1f} s, replied {text.strip()[:30]!r}" if ok else
                  f"generation failed (HTTP {code}): {str(body)[:200]}")
        env.evidence.setdefault("generation", {})[name] = {"ok": ok, "when": stamp(env), "detail": detail}
        return Status("ready" if ok else "failed", f"{name}: {detail}", verified=ok, data={"model": name},
                      action=None if ok else "manual")

    def install(self, env, status):
        name = status.data.get("model") or self.wanted(env)
        size = self.size_of.get(name)
        free = env.system.disk_free(Path.home())
        if size and free < (size * 1.25 + 2) * GB:
            return Status("failed", f"not enough disk space for {name}: {free / GB:.1f} GB free, about "
                          f"{size * 1.25 + 2:.0f} GB needed", action="manual")
        url = env.config["models"]["url"].rstrip("/") if self.kind == "coding" else settings.vision_settings(env.config)["url"].rstrip("/")
        last = {"at": 0}

        def report(event):
            now = env.system.time()
            if event.get("total") and event.get("completed") is not None and now - last["at"] >= 3:
                last["at"] = now
                env.progress(f"ollama: {event.get('status', 'downloading')} {event['completed'] / GB:.2f} of "
                             f"{event['total'] / GB:.2f} GB")
            elif event.get("status") and not event.get("total"):
                env.progress(f"ollama: {event['status']}")
        code, body = env.system.http("POST", url + "/api/pull", {"model": name, "stream": True}, timeout=3600,
                                     on_json_line=report)
        if code != 200 or (isinstance(body, dict) and body.get("error")):
            return Status("failed", f"download failed: {str(body)[:300]}", action="pull", data={"model": name})
        section, key = self.config_key
        if not env.config[section].get(key):
            env.config[section][key] = name
            env.save_config()
        return self.check(env, deep=True)


class VisionModel(Model):
    id, title, profiles, optional = "vision", "Local vision model (images, screenshots)", ("full",), True
    kind, models, config_key = "vision", VISION_MODELS, ("vision", "model")

    def check(self, env, deep=False):
        status = super().check(env, deep=False)
        if not deep or not status.ready:
            return status
        name = self.wanted(env)
        url = settings.vision_settings(env.config)["url"].rstrip("/")
        code, body = env.system.http("POST", url + "/api/show", {"model": name}, timeout=30)
        capabilities = (body or {}).get("capabilities") or [] if isinstance(body, dict) else []
        env.evidence.setdefault("generation", {})[name] = {"ok": code == 200 and "vision" in capabilities,
                                                           "when": stamp(env), "detail": "image capability reported"}
        if code == 200 and "vision" in capabilities:
            return Status("ready", f"{name} accepts images (Ollama reports the vision capability)", verified=True,
                          data=status.data)
        return Status("failed", f"{name} does not report image support (HTTP {code}); choose a vision model such as "
                      "qwen2.5vl:7b", data=status.data, action="manual")


# Premium subscriptions -------------------------------------------------------------------------

AUTH_STATES = ("not_installed", "not_authenticated", "authenticated", "api_key_billing", "expired",
               "temporarily_unavailable", "disabled")


def classify_auth(name: str, code: int, text: str) -> tuple[str, str]:
    """The sign-in state from the CLI's own status command. Only the status is read; tokens and
    credential files are never opened."""
    lowered = text.lower()
    if code == 124 or any(word in lowered for word in ("timed out", "enotfound", "econnrefused", "network error",
                                                        "rate limit", "overloaded", "503", "service unavailable")):
        return "temporarily_unavailable", "the sign-in check could not reach the service; try again later"
    if any(word in lowered for word in ("expired", "re-authenticate", "reauthenticate", "token is invalid",
                                        "invalid_grant", "401")):
        return "expired", f"the {name} sign-in has expired; sign in again"
    if name == "claude":
        from ..subscriptions import json_object
        try:
            status = json_object(text)
        except ValueError:
            status = {}
        if code == 0 and status.get("loggedIn"):
            if "api" in str(status.get("authMethod", "")).lower():
                return "api_key_billing", "signed in with an API key (billed separately); Coding Brain uses only " \
                                          "subscription sign-ins"
            return "authenticated", f"signed in ({status.get('authMethod') or 'subscription'})"
        return "not_authenticated", "not signed in"
    if code == 0 and "logged in" in lowered and "not logged in" not in lowered:
        if "api key" in lowered:
            return "api_key_billing", "signed in with an API key (billed separately); Coding Brain uses only " \
                                      "ChatGPT subscription sign-ins"
        return "authenticated", text.strip().splitlines()[0][:100] if text.strip() else "signed in"
    return "not_authenticated", "not signed in"


class Premium(Component):
    optional = True
    command, status_arguments, sign_in_arguments, key_variables = "", (), (), ()

    def check(self, env, deep=False):
        system = env.system
        enabled = env.config["supervisors"].get(self.id, {}).get("enabled")
        locate = getattr(system, "locate", None)
        path, checked = locate(self.command) if locate else (system.which(self.command), [])
        if not path:
            where = f"; looked in {len(checked)} places: " + ", ".join(checked[:8]) if checked else ""
            return Status("missing", f"{self.command} CLI not found{where}", action="install",
                          data={"auth": "not_installed", "enabled": enabled, "checked": checked})
        # Run the launcher that was found: it may be outside this process's PATH.
        command = path if locate else self.command
        version = version_line(env, [command, "--version"])
        environment = {key: value for key, value in system.environ().items() if key not in self.key_variables}
        code, output = system.run([command, *self.status_arguments], timeout=60, env=environment)
        auth, detail = classify_auth(self.id, code, output)
        data = {"auth": auth, "enabled": enabled, "path": path}
        if auth == "authenticated":
            if not enabled:
                data["auth"] = "disabled"
                return Status("ready", f"{detail}; turned off in your settings (codingbrain setup --enable-{self.id})",
                              version, verified=True, data=data)
            return Status("ready", detail, version, verified=True, data=data)
        if auth == "temporarily_unavailable":
            return Status("unavailable", detail, version, data=data)
        return Status("needs_sign_in", detail, version, action="sign_in", data=data)

    def install(self, env, status):
        if status.action == "install":
            status = self.install_cli(env)
            if status.state != "needs_sign_in":
                return status
        return self.sign_in(env)

    def install_cli(self, env) -> Status:
        return self.winget(env)

    def sign_in(self, env) -> Status:
        """The vendor's own sign-in in this terminal (it opens the browser). API-key variables are
        removed from its environment so a subscription sign-in is not replaced by key billing."""
        environment = {key: value for key, value in env.system.environ().items() if key not in self.key_variables}
        env.progress(f"starting the official {self.title} sign-in: complete it in the browser window")
        code = env.system.run_interactive([self.command, *self.sign_in_arguments], env=environment)
        after = self.check(env)
        if not after.ready and code:
            after.detail += f" (sign-in exited {code}; {self.manual_sign_in})"
        return after

    manual_sign_in = ""


class Claude(Premium):
    id, title = "claude", "Claude Code (optional, uses your Claude subscription)"
    command, status_arguments, sign_in_arguments = "claude", ("auth", "status"), ("auth", "login")
    key_variables = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
    program = "claude"
    winget_id, size, source = "Anthropic.ClaudeCode", "about 100 MB", "winget Anthropic.ClaudeCode (Anthropic)"
    license = "Claude Code is governed by Anthropic's terms; a Claude Pro or Max subscription is needed to sign in"
    manual_sign_in = "you can also run `claude` and type /login"


class NodeJS(Component):
    id, title, winget_id, winget_scope, program = "node", "Node.js LTS", "OpenJS.NodeJS.LTS", None, "node"
    source = "winget OpenJS.NodeJS.LTS"

    def check(self, env, deep=False):
        return Status("ready" if env.system.which("npm") else "missing", action=None if env.system.which("npm") else "install")


class Codex(Premium):
    id, title = "codex", "Codex CLI (optional, uses your ChatGPT subscription)"
    command, status_arguments, sign_in_arguments = "codex", ("login", "status"), ("login",)
    key_variables = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")
    size, source = "about 60 MB (plus Node.js LTS if missing)", "npm registry @openai/codex (OpenAI), Node.js via winget OpenJS.NodeJS.LTS"
    license = "Codex is governed by OpenAI's terms; a ChatGPT Plus, Pro, Business or Enterprise plan is needed to sign in"
    manual_sign_in = "run `codex login` and choose Sign in with ChatGPT"

    def install_cli(self, env):
        system = env.system
        if not system.which("npm"):
            node = NodeJS().winget(env)
            if not node.ready:
                return Status("failed", "Node.js (needed for the Codex CLI) could not be installed: " + node.detail,
                              action="manual")
        code, output = system.run(["npm", "install", "--global", "@openai/codex"], timeout=1800, on_line=env.progress)
        system.refresh_path()
        after = self.check(env)
        if after.state == "missing":
            after.detail = f"npm exited {code}: {output.strip()[-300:]}"
        return after


# Knowledge and multimodal extras -------------------------------------------------------------------

class Knowledge(Component):
    id, title, optional = "knowledge", "Engineering knowledge library", True
    size, source = "about 30 MB", "license-checked sources from GitHub (knowledge.example.json)"

    def check(self, env, deep=False):
        path = env.layout.data / "knowledge.sqlite3"
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        except sqlite3.Error:
            count = 0
        if count:
            return Status("ready", f"{count} documents indexed", verified=True)
        return Status("missing", "not imported", action="install")

    def install(self, env, status):
        import sys
        manifest = Path(__file__).parent / "assets" / "knowledge.example.json"
        environment = {**env.system.environ(), "BRAIN_KNOWLEDGE_DB": str(env.layout.data / "knowledge.sqlite3")}
        code, output = env.system.run([sys.executable, "-m", "brain.knowledge", "sync", str(manifest)], timeout=3600,
                                      env=environment, on_line=env.progress)
        after = self.check(env)
        if not after.ready:
            after.state, after.detail = "failed", f"import exited {code}: {output.strip()[-300:]}"
        return after


class OCR(Component):
    id, title, optional = "ocr", "Tesseract OCR (text in screenshots and scans)", True
    program = "tesseract"
    winget_id, winget_scope, admin = "UB-Mannheim.TesseractOCR", None, True
    size, source = "about 50 MB", "winget UB-Mannheim.TesseractOCR"

    def check(self, env, deep=False):
        command = env.config["ocr"].get("command") or env.system.which("tesseract")
        if not command:
            return Status("missing", "not installed", action="install" if env.system.platform == "windows" else "manual")
        line = version_line(env, [command, "--version"])
        return Status("ready" if line else "failed", "tesseract runs" if line else "tesseract does not run", line,
                      verified=bool(line))


EDGE = (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")


class Browser(Component):
    id, title, optional = "browser", "Browser for visual checks", True
    size, source = "about 150 MB", "Playwright's Chromium (playwright install chromium)"

    def check(self, env, deep=False):
        import importlib.util
        if importlib.util.find_spec("playwright") is None:
            return Status("failed", "Playwright is not installed in Coding Brain's environment", action="manual")
        if env.system.platform == "windows" and any(env.system.exists(path) for path in EDGE):
            return Status("ready", "uses Microsoft Edge (part of Windows)", verified=True)
        if env.config["visual"].get("browser_executable"):
            return Status("ready", f"uses {env.config['visual']['browser_executable']}")
        cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or
                     (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright" if env.system.platform == "windows"
                      else Path.home() / ".cache" / "ms-playwright"))
        if cache.is_dir() and any(cache.glob("chromium*")):
            return Status("ready", f"Playwright Chromium in {cache}")
        return Status("missing", "no browser for visual checks", action="install")

    def install(self, env, status):
        import sys
        code, output = env.system.run([sys.executable, "-m", "playwright", "install", "chromium"], timeout=1800,
                                      on_line=env.progress)
        after = self.check(env)
        if not after.ready:
            after.state, after.detail = "failed", f"playwright install exited {code}: {output.strip()[-300:]}"
        return after


# The installation order: core, virtualization, Docker, models, subscriptions, sandbox, extras.
COMPONENTS = [Python(), Git(), WSL(), Docker(), Ollama(), Model(), Claude(), Codex(), Sandbox(), Knowledge(),
              OCR(), VisionModel(), Browser()]
BY_ID = {component.id: component for component in COMPONENTS}
PROFILES = {"local": [c.id for c in COMPONENTS if "local" in c.profiles], "full": [c.id for c in COMPONENTS]}


def check_all(env: Env, ids=None, deep=False) -> dict[str, Status]:
    """Check components in order; one that needs another that is not ready is 'waiting'."""
    for component_id in ids or [component.id for component in COMPONENTS]:
        component = BY_ID[component_id]
        blocked = [need for need in component.requires if need in env.results and not env.results[need].ready]
        if blocked:
            env.results[component_id] = Status("waiting", f"needs {', '.join(BY_ID[need].title for need in blocked)} first")
            continue
        try:
            env.results[component_id] = component.check(env, deep=deep)
        except IntegrityError as error:
            env.results[component_id] = Status("failed", str(error), action="manual")
        except Exception as error:  # a probe that crashes is reported, not hidden
            env.results[component_id] = Status("failed", f"check failed: {type(error).__name__}: {str(error)[:200]}")
    return env.results


def dumps(results: dict[str, Status]) -> str:
    return json.dumps({key: value.to_dict() for key, value in results.items()}, indent=2)
