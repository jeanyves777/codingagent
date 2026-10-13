"""Fixed container runner with user-selected, model-inaccessible profiles."""
import json
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


DEFAULTS = {
    "python": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    "node": ["npm", "test"],
    "rust": ["cargo", "test"], "go": ["go", "test", "./..."],
    "java": ["mvn", "test"], "dotnet": ["dotnet", "test"], "custom": ["make", "test"],
}
ALLOWED_EXECUTABLES = {"python", "pytest", "npm", "node", "npx", "pnpm", "yarn", "cargo", "go",
                       "dotnet", "mvn", "gradle", "make", "cmake", "ctest", "ruby", "php"}


def profile(workspace: Path) -> dict:
    config_path = workspace / "coding-brain.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if set(config) - {"test_profile", "test_command", "preview"}:
            raise ValueError("Unknown sandbox configuration key")
        name = config.get("test_profile", "python")
        command = config.get("test_command", DEFAULTS.get(name))
    else:
        from .local.project import commands
        import shlex
        declared = commands(workspace).get('test')
        fallback = 'node' if (workspace / 'package.json').is_file() else 'python'
        command = shlex.split(declared) if declared else DEFAULTS[fallback]
        name = {'npm':'node', 'pnpm':'node', 'yarn':'node', 'cargo':'rust', 'go':'go',
                'mvn':'java', 'gradle':'java', 'dotnet':'dotnet', 'make':'custom'}.get(command[0], 'python')
    if name not in DEFAULTS or not isinstance(command, list) or not 1 <= len(command) <= 20:
        raise ValueError("Invalid sandbox profile")
    if command[0] not in ALLOWED_EXECUTABLES or any(not isinstance(arg, str) or len(arg) > 300 for arg in command):
        raise ValueError("Test command is not allowed")
    return {"name": name, "command": command}


import os

# Seconds a test run may take; slow machines (no GPU, little free memory, Docker starting containers
# slowly) can raise it with CODINGBRAIN_SANDBOX_TIMEOUT.
TIMEOUT = max(30, int(os.environ.get("CODINGBRAIN_SANDBOX_TIMEOUT", "120") or 120))


def _remove(name: str):
    """Remove a container, best effort. A slow Docker must never turn cleanup into a task failure:
    if removal does not finish quickly it continues in the background (docker run --rm also removes
    the container once it exits)."""
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        try:
            subprocess.Popen(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    except OSError:
        pass


DOCKER_HELP = ("Docker is not responding ({detail}). Restart Docker Desktop (tray icon > Restart, or quit and "
               "reopen it), wait for \"Engine running\", then retry. Your code was not tested, so this is not a test failure.")


def docker_ready() -> tuple[bool, str]:
    """Whether the Docker engine answers at all, within 20 s. Used to explain a timeout or a docker
    error: a hung Docker Desktop is an infrastructure problem, not something to repair in the code."""
    try:
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                               timeout=20)
    except subprocess.TimeoutExpired:
        return False, "it did not answer within 20 s"
    except OSError as error:
        return False, f"docker could not be started: {error}"
    except Exception:  # a test double or an unexpected runner: do not guess
        return True, ""
    if getattr(probe, "returncode", 0) != 0 or not str(getattr(probe, "stdout", "x")).strip():
        return False, (str(getattr(probe, "stderr", "") or "").strip()[:200] or "the engine is not running")
    return True, ""


def _docker_problem(selected: dict) -> dict | None:
    ready, detail = docker_ready()
    if ready:
        return None
    return {"passed": False, "exit_code": None, "profile": selected["name"], "infrastructure": True,
            "output": DOCKER_HELP.format(detail=detail)}


def _stop(process):
    process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass  # the docker client is gone or stuck; the container was already asked to go


def _supervise(command: list[str], name: str, output, should_cancel) -> int | str:
    """Run the container, polling for cancellation; return its exit code, "timeout", or "cancelled"."""
    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + TIMEOUT
    try:
        while True:
            try:
                return process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            outcome = ("cancelled" if should_cancel() else
                       "timeout" if time.monotonic() >= deadline else None)
            if outcome:
                _remove(name)
                _stop(process)
                return outcome
    except BaseException:
        _remove(name)
        process.kill()
        raise


NODE_ID = re.compile(r"[\w./-]+\.py::[\w.\[\]-]+")


def run_tests(workspace: Path, images: str | dict, should_cancel=None, only=None) -> dict:
    """Run the project's tests offline. `only` (pytest node ids such as test_x.py::test_y) runs
    just those tests with the same command, for diagnosis."""
    selected = profile(workspace)
    if only:
        if selected["name"] != "python" or "pytest" not in selected["command"] or \
                not all(isinstance(node, str) and NODE_ID.fullmatch(node) for node in only):
            return {"passed": False, "exit_code": None, "profile": selected["name"],
                    "output": "Running single tests is supported for pytest node ids only"}
        selected = {**selected, "command": [*selected["command"], *only]}
    image = images if isinstance(images, str) else images.get(selected["name"])
    if not image:
        return {"passed": False, "exit_code": None, "profile": selected["name"], "infrastructure": True,
                "output": "No sandbox image configured for detected profile " + selected['name'] +
                          ". Prepare an image with the project's toolchain and dependencies before retrying."}
    name = "coding-brain-" + uuid.uuid4().hex
    command = ["docker", "run", "--rm", "--pull=never", "--name", name,
               "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=128",
               "--memory=512m", "--memory-swap=512m", "--cpus=1",
               "--user=65534:65534", "--tmpfs=/tmp:rw,nosuid,size=64m",
               "--mount", f"type=bind,source={workspace.resolve()},target=/code,readonly",
               "--workdir=/code", "--env=PYTHONDONTWRITEBYTECODE=1",
               "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env=NPM_CONFIG_CACHE=/tmp/npm",
               "--env=NODE_PATH=/opt/node_modules",
               "--env=PATH=/opt/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               image, *selected["command"]]
    with tempfile.TemporaryFile() as output:
        if should_cancel:
            code = _supervise(command, name, output, should_cancel)
        else:
            try:
                code = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                      timeout=TIMEOUT).returncode
            except subprocess.TimeoutExpired:
                _remove(name)
                code = "timeout"
        if code == "timeout":
            return _docker_problem(selected) or {
                "passed": False, "exit_code": None, "profile": selected["name"],
                "output": f"Sandbox timed out after {TIMEOUT} s (on a slow machine, set CODINGBRAIN_SANDBOX_TIMEOUT)"}
        if code in (125, 126, 127):  # docker itself failed (daemon, image or runtime), not the tests
            problem = _docker_problem(selected)
            if problem:
                return problem
        if code == "cancelled":
            return {"passed": False, "exit_code": None, "profile": selected["name"],
                    "cancelled": True, "output": "Sandbox stopped by cancellation"}
        output.seek(0)
        text = output.read(16_000).decode("utf-8", errors="replace")
    return {"passed": code == 0, "exit_code": code, "profile": selected["name"], "output": text}


BUILD_TIMEOUT = 300


def run_build(workspace: Path, images: str | dict, command: list, output: Path, timeout: int = BUILD_TIMEOUT) -> dict:
    """A frontend preview build in the same offline sandbox as the tests. The project stays
    read-only; the build may write only to /out (and a private /tmp)."""
    selected = profile(workspace)
    image = images if isinstance(images, str) else (images or {}).get(selected["name"])
    if not image:
        return {"passed": False, "output": "No sandbox image configured for profile"}
    if not isinstance(command, list) or not 1 <= len(command) <= 20 or command[0] not in ALLOWED_EXECUTABLES \
            or any(not isinstance(arg, str) or len(arg) > 300 for arg in command):
        return {"passed": False, "output": "Preview build command is not allowed"}
    name = "coding-brain-build-" + uuid.uuid4().hex
    docker = ["docker", "run", "--rm", "--pull=never", "--name", name,
              "--network=none", "--read-only", "--cap-drop=ALL",
              "--security-opt=no-new-privileges", "--pids-limit=256",
              "--memory=1g", "--memory-swap=1g", "--cpus=2",
              "--user=65534:65534", "--tmpfs=/tmp:rw,nosuid,size=256m",
              "--mount", f"type=bind,source={workspace.resolve()},target=/code,readonly",
              "--mount", f"type=bind,source={output.resolve()},target=/out",
              "--workdir=/code", "--env=HOME=/tmp", "--env=NPM_CONFIG_CACHE=/tmp/npm",
              "--env=NODE_PATH=/opt/node_modules", "--env=BUILD_OUT=/out",
              "--env=PATH=/opt/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
              image, *command]
    with tempfile.TemporaryFile() as log:
        try:
            code = subprocess.run(docker, stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            _remove(name)
            return {"passed": False, "output": "Preview build timed out"}
        log.seek(0)
        text = log.read(16_000).decode("utf-8", errors="replace")
    return {"passed": code == 0, "exit_code": code, "output": text}
