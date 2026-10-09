"""A simulated Windows computer for the installer's decision logic.

Everything here is SIMULATED: no virtualization, WSL, Docker, Ollama, winget or subscription is
real. Tests that use it prove how the installer decides, checkpoints and reports; they do not
prove that real hardware, real installers or real sign-ins work (tests marked real do that).
"""
import json
import sqlite3
from pathlib import Path

GB = 1024 ** 3
WINGET = {"Git.Git": ["git"], "Docker.DockerDesktop": ["docker", "docker_desktop"], "Ollama.Ollama": ["ollama"],
          "Anthropic.ClaudeCode": ["claude"], "OpenJS.NodeJS.LTS": ["node", "npm"],
          "UB-Mannheim.TesseractOCR": ["tesseract"]}
MODEL_SIZES = {"qwen2.5-coder:7b": 4.7, "qwen2.5-coder:3b": 1.9, "qwen2.5-coder:1.5b": 1.0, "qwen2.5-coder:14b": 9.0,
               "qwen2.5vl:7b": 6.0, "qwen2.5vl:3b": 3.2}


class FakeMachine:
    simulated = True

    def __init__(self, **overrides):
        self.platform = "windows"
        self.programs = {"winget", "powershell", "wsl"}  # wsl.exe ships with Windows, even before WSL is installed
        self.facts_ = {"caption": "Microsoft Windows 11 Pro (simulated)", "build": 26100, "arch": "AMD64",
                       "ram": 16 * GB, "cores": 8, "admin": False, "execution_policy": "RemoteSigned",
                       "firmware_virtualization": True, "hypervisor_present": False}
        self.wsl = "missing"            # missing | needs_restart | installed
        self.wsl_needs_restart = True
        self.uac_declined = False
        self.virtualization_ok = True
        self.docker_engine = None       # None (stopped) | "linux" | "windows"
        self.docker_starts = True
        self.images = {}
        self.ollama_running = False
        self.ollama_autostart = True
        self.models = set()
        self.generation_ok = True
        self.network = True
        self.disk = 200 * GB
        self.auth = {"claude": "none", "codex": "none"}   # none | subscription | api_key | expired | offline
        self.sign_in_works = True
        self.winget_fails = set()
        self.tampered = set()
        self.resume_command = None
        self.clock = 1_000_000.0
        self.calls = []
        self.environment = {"PATH": "C:\\Windows", "USERPROFILE": "C:\\Users\\Tester"}
        self.interactive_env = []
        self.interrupt_on = None        # raise KeyboardInterrupt when this call happens
        for key, value in overrides.items():
            setattr(self, key, value)
        if "programs" in overrides:
            self.programs = set(overrides["programs"]) | {"wsl", "powershell"}

    # probes
    def which(self, name):
        name = Path(name).name.lower().removesuffix(".exe")
        return f"C:\\fake\\{name}.exe" if name in self.programs else None

    def exists(self, path):
        if "msedge.exe" in path:
            return True  # Edge is part of Windows
        return "Docker Desktop.exe" in path and "docker_desktop" in self.programs

    def facts(self):
        return dict(self.facts_)

    def disk_free(self, path):
        return float(self.disk)

    def gpu(self):
        return getattr(self, "gpu_", None)

    def environ(self):
        return dict(self.environment)

    def time(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def refresh_path(self):
        pass

    def signature(self, path):
        name = path.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")
        return {"status": "HashMismatch"} if name in self.tampered else {"status": "Valid", "signer": f"CN={name} vendor"}

    # commands
    def run(self, arguments, timeout=60, env=None, on_line=None, stdin=None):
        program = arguments[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        rest = list(arguments[1:])
        self.calls.append([program, *rest])
        if self.interrupt_on and self.interrupt_on(program, rest):
            self.interrupt_on = None
            raise KeyboardInterrupt
        if program not in self.programs and not program.startswith("python"):
            return 127, f"{program}: not found"
        say = on_line or (lambda line: None)
        if program == "git":
            return 0, "git version 2.47.0.windows.1"
        if program == "winget":
            package = rest[rest.index("--id") + 1]
            if not self.network or package in self.winget_fails:
                return 1, "InternetOpenUrl() failed. 0x80072ee7 : unknown error"
            say(f"Found {package} [{package}] Version 1.0")
            say("Downloading https://example.invalid/installer.exe")
            say("  ██████████████████████████████  120 MB / 120 MB")
            say("Successfully installed")
            self.programs.update(WINGET[package])
            if package == "Ollama.Ollama" and self.ollama_autostart:
                self.ollama_running = True
            return 0, "Successfully installed"
        if program == "wsl":
            working = self.wsl == "installed"
            return (0, "WSL version: 2.3.26.0\nKernel version: 5.15") if working else (1, "WSL is not installed.")
        if program == "docker":
            return self.docker(rest, say)
        if program == "tesseract":
            return 0, "tesseract v5.4.0.20240606 (simulated)"
        if program == "ollama":
            return 0, "ollama version is 0.12.0"
        if program in ("claude", "codex"):
            if rest == ["--version"]:
                return 0, f"{program} 1.0.0"
            return self.auth_status(program, env)
        if program == "npm":
            if not self.network:
                return 1, "npm ERR! network"
            self.programs.add("codex")
            return 0, "added 1 package"
        if "-m" in rest and "brain.knowledge" in rest:
            path = Path((env or {})["BRAIN_KNOWLEDGE_DB"])
            if not self.network:
                return 1, "network unreachable"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, source TEXT)")
                db.execute("INSERT INTO documents (source) VALUES ('fake')")
            return 0, "imported 1 document"
        if "-m" in rest and "playwright" in rest:
            return 0, "chromium downloaded"
        return 0, ""

    def docker(self, rest, say):
        if rest == ["--version"]:
            return 0, "Docker version 28.4.0, build simulated"
        if rest[:1] == ["version"]:
            return (0, f"{self.docker_engine} 28.4.0") if self.docker_engine else \
                (1, "error during connect: the docker daemon is not running")
        if not self.docker_engine:
            return 1, "Cannot connect to the Docker daemon"
        if rest[:2] == ["image", "inspect"]:
            image = rest[-1]
            return (0, self.images[image]) if image in self.images else (1, "No such image")
        if rest[:1] == ["build"]:
            if not self.network:
                return 1, "failed to resolve source metadata for docker.io/library/python:3.11-slim"
            image = rest[rest.index("-t") + 1]
            say(f"#1 building {image}")
            self.images[image] = "sha256:" + str(abs(hash(image)))[:12]
            return 0, "naming to " + image
        if rest[:1] == ["run"]:
            image = next(argument for argument in rest if argument in self.images)
            return 0, "sandbox ok"
        if rest[:1] in (["rmi"], ["volume"], ["system"]):
            raise AssertionError(f"the installer must never remove Docker data: docker {' '.join(rest)}")
        return 0, ""

    def auth_status(self, program, env):
        state = self.auth[program]
        if state == "offline":
            return 124, "request timed out"
        if program == "claude":
            if state == "subscription":
                return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
            if state == "api_key":
                return 0, json.dumps({"loggedIn": True, "authMethod": "api_key"})
            if state == "expired":
                return 1, "OAuth token has expired. Please run /login"
            return 1, json.dumps({"loggedIn": False})
        if state == "subscription":
            return 0, "Logged in using ChatGPT"
        if state == "api_key":
            return 0, "Logged in using an API key - sk-proj-***"
        if state == "expired":
            return 1, "Your access token could not be refreshed: refresh token expired. Please log in again."
        return 1, "Not logged in"

    def run_interactive(self, arguments, env=None):
        program = arguments[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        self.calls.append(["interactive", program, *arguments[1:]])
        self.interactive_env.append(env or {})
        if self.sign_in_works:
            self.auth[program] = "subscription"
            return 0
        return 1

    def powershell(self, script, timeout=60):
        return 0, ""

    def run_elevated(self, arguments, timeout=3600):
        self.calls.append(["elevated", *arguments])
        if self.uac_declined:
            return 1223
        if "--install" in arguments:
            self.wsl = "needs_restart" if self.wsl_needs_restart else "installed"
        return 0

    def start_detached(self, arguments):
        self.calls.append(["start", *arguments])
        if "Docker Desktop.exe" in arguments[0]:
            if self.docker_starts and self.wsl == "installed" and self.virtualization_ok:
                self.docker_engine = "linux"
            return True
        if Path(arguments[0]).name.startswith("ollama"):
            self.ollama_running = "ollama" in self.programs
            return True
        return False

    def register_resume(self, command):
        self.resume_command = command
        return True

    def clear_resume(self):
        self.resume_command = None

    def restart(self):
        """Windows restarted: WSL finishes installing; background apps are not running yet."""
        if self.wsl == "needs_restart" and self.virtualization_ok:
            self.wsl = "installed"
        self.docker_engine = None
        self.ollama_running = self.ollama_running and self.ollama_autostart
        self.calls.append(["restart"])

    # HTTP
    def http(self, method, url, payload=None, timeout=10, on_json_line=None):
        if url.startswith("http://localhost:11434"):
            if not self.ollama_running:
                return 0, "ConnectError: connection refused"
            path = url.split("11434", 1)[1]
            if path == "/api/version":
                return 200, {"version": "0.12.0"}
            if path == "/api/tags":
                return 200, {"models": [{"name": name} for name in sorted(self.models)]}
            if path == "/api/generate":
                if payload["model"] not in self.models:
                    return 404, {"error": "model not found"}
                return (200, {"response": "READY", "eval_count": 3}) if self.generation_ok else \
                    (500, {"error": "llama runner process has terminated: exit status 0xc0000409"})
            if path == "/api/pull":
                if not self.network:
                    return 200, {"error": "pull model manifest: dial tcp: lookup registry.ollama.ai: no such host"}
                total = int(MODEL_SIZES.get(payload["model"], 1) * GB)
                for done in (0, total // 2, total):
                    self.clock += 5
                    on_json_line({"status": f"pulling {payload['model']}", "total": total, "completed": done})
                on_json_line({"status": "verifying sha256 digest"})
                on_json_line({"status": "success"})
                self.models.add(payload["model"])
                return 200, {"status": "success"}
            if path == "/api/show":
                return 200, {"capabilities": ["completion", "vision"] if "vl" in payload.get("model", "") else ["completion"]}
        return (200, {}) if self.network else (0, "ConnectError: no route to host")
