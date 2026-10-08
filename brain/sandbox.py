"""Fixed container runner with user-selected, model-inaccessible profiles."""
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


DEFAULTS = {
    "python": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    "node": ["npm", "test", "--", "--runInBand"],
}
ALLOWED_EXECUTABLES = {"python", "pytest", "npm", "node", "npx", "pnpm", "yarn"}


def profile(workspace: Path) -> dict:
    config_path = workspace / "coding-brain.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if set(config) - {"test_profile", "test_command"}:
            raise ValueError("Unknown sandbox configuration key")
        name = config.get("test_profile", "python")
        command = config.get("test_command", DEFAULTS.get(name))
    else:
        name = "node" if (workspace / "package.json").exists() and not (workspace / "pyproject.toml").exists() else "python"
        command = DEFAULTS[name]
    if name not in DEFAULTS or not isinstance(command, list) or not 1 <= len(command) <= 20:
        raise ValueError("Invalid sandbox profile")
    if command[0] not in ALLOWED_EXECUTABLES or any(not isinstance(arg, str) or len(arg) > 300 for arg in command):
        raise ValueError("Test command is not allowed")
    return {"name": name, "command": command}


TIMEOUT = 120


def _remove(name: str):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


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
                process.kill()
                process.wait(timeout=15)
                return outcome
    except BaseException:
        _remove(name)
        process.kill()
        raise


def run_tests(workspace: Path, images: str | dict, should_cancel=None) -> dict:
    selected = profile(workspace)
    image = images if isinstance(images, str) else images.get(selected["name"])
    if not image:
        return {"passed": False, "exit_code": None, "profile": selected["name"],
                "output": "No sandbox image configured for profile"}
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
            return {"passed": False, "exit_code": None, "profile": selected["name"],
                    "output": "Sandbox timed out"}
        if code == "cancelled":
            return {"passed": False, "exit_code": None, "profile": selected["name"],
                    "cancelled": True, "output": "Sandbox stopped by cancellation"}
        output.seek(0)
        text = output.read(16_000).decode("utf-8", errors="replace")
    return {"passed": code == 0, "exit_code": code, "profile": selected["name"], "output": text}
