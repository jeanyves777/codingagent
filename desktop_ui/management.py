"""Read-only provider/readiness probes and explicitly authorized lifecycle commands.

No password, cookie, subscription token or API secret ever crosses the desktop bridge.
Official CLIs own sign-in. Coding Brain's existing installer/updater own system changes.
The registry distinguishes real integrations from discovery-only placeholders.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    group: str
    description: str
    executable: str | None = None
    login: tuple[str, ...] = ()
    docs: str = ""
    adapter: str = "planned"  # local, supervisor, cli-available, external-key, planned


PROVIDERS: tuple[Provider, ...] = (
    Provider("ollama", "Ollama / Qwen", "Local", "Local implementation and chat, no subscription required.",
             "ollama", adapter="local", docs="https://ollama.com/"),
    Provider("claude", "Claude Code", "Subscription", "Official CLI sign-in; used for governed planning and diagnosis.",
             "claude", ("claude",), "https://code.claude.com/docs/en/setup", "supervisor"),
    Provider("codex", "OpenAI Codex", "Subscription", "Sign in with ChatGPT through the official Codex CLI.",
             "codex", ("codex", "login"), "https://developers.openai.com/codex/cli/", "supervisor"),
    Provider("gemini", "Google Gemini CLI", "Additional", "Official Google sign-in supported by Gemini CLI; core delegation adapter is pending.",
             "gemini", ("gemini",), "https://github.com/google-gemini/gemini-cli", "cli-available"),
    Provider("grok", "xAI Grok", "Additional", "API-key authentication only; never treated as a subscription connection.",
             docs="https://docs.x.ai/", adapter="external-key"),
    Provider("meta", "Meta Llama", "Additional", "Local Llama models via Ollama; hosted provider routing requires an approved adapter.",
             "ollama", docs="https://www.llama.com/", adapter="local-discovery"),
    Provider("muse", "Meta Muse / Mouse", "Future", "Provider identity and a supported external developer interface must be confirmed before connecting.",
             docs="https://ai.meta.com/llama/get-started/", adapter="planned"),
)
BY_ID = {p.id: p for p in PROVIDERS}


def safe_probe(command: list[str], *, timeout: float = 7) -> bool | None:
    """Do not expose vendor auth status stdout, which may include account identifiers."""
    try:
        r = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


IS_WINDOWS = os.name == "nt"


def installed_cli(name: str) -> str | None:
    return locate_cli(name)[0]


def _registry_path_dirs() -> list[str]:
    """The user and machine PATH saved in the registry: a CLI installed after this desktop started
    is on it even though this process's inherited PATH is stale (no reboot needed)."""
    try:
        import winreg
    except ImportError:  # not Windows (or a simulated Windows in tests)
        return []
    directories = []
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as handle:
                directories += [os.path.expandvars(part) for part in winreg.QueryValueEx(handle, "Path")[0].split(";")
                                if part.strip()]
        except OSError:
            pass
    return directories


def _npm_prefixes() -> list[str]:
    """npm's global bin folders from configuration files only; npm itself is never run."""
    prefixes = []
    if os.environ.get("NPM_CONFIG_PREFIX"):
        prefixes.append(os.environ["NPM_CONFIG_PREFIX"])
    rcs = [Path(os.environ.get("USERPROFILE") or Path.home()) / ".npmrc"]
    if os.environ.get("APPDATA"):
        rcs.append(Path(os.environ["APPDATA"]) / "npm" / "etc" / "npmrc")
    for rc in rcs:
        try:
            for line in rc.read_text(encoding="utf-8", errors="replace").splitlines()[:200]:
                key, _, value = line.partition("=")
                if key.strip().lower() == "prefix" and value.strip():
                    prefixes.append(os.path.expandvars(value.strip().strip('"')))
        except OSError:
            continue
    if os.environ.get("APPDATA"):
        prefixes.append(str(Path(os.environ["APPDATA"]) / "npm"))
    return list(dict.fromkeys(prefixes))


def locate_cli(name: str) -> tuple[str | None, list[str]]:
    """Fallback for engines without the typed API (with API 1.0, providers.list from the installed
    engine is authoritative). Same order as the engine: this PATH, the registry PATH, npm's
    configured prefix, official launcher folders. Nothing is executed to find a CLI, and finding
    one never means signed in or connected. Returns the path and every location checked."""
    found = shutil.which(name)
    if found or not IS_WINDOWS or name not in {"claude", "codex", "gemini"}:
        return found, ([] if found else ["PATH"])  # only vendor CLIs get the extra lookup
    checked = ["PATH"]
    folders = [*_registry_path_dirs(), *_npm_prefixes()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        if name == "codex":  # the standalone Codex for Windows
            folders.append(str(Path(local) / "Programs" / "OpenAI" / "Codex" / "bin"))
        folders.append(str(Path(local) / "Microsoft" / "WinGet" / "Links"))
    if name in {"claude", "codex"}:
        folders.append(str(Path(os.environ.get("USERPROFILE") or Path.home()) / ".local" / "bin"))
    for folder in dict.fromkeys(folders):
        for suffix in (".exe", ".cmd"):
            candidate = Path(folder) / (name + suffix)
            checked.append(str(candidate))
            if candidate.is_file():
                return str(candidate), checked
    return None, checked


def is_key_set(name: str) -> bool:
    # Presence only, NEVER expose value or copy it into settings/logs.
    return bool(os.environ.get(name))


def providers_snapshot(probe: Callable[[list[str]], bool | None] = safe_probe) -> list[dict]:
    entries = []
    # Avoid 2 * 7 second serial CLI delays on the first visible desktop screen.
    binaries = {p.id: installed_cli(p.executable) if p.executable else None for p in PROVIDERS}
    with ThreadPoolExecutor(max_workers=2) as pool:
        checks = {p.id:pool.submit(probe, [binaries[p.id], "auth", "status"] if p.id == "claude"
                    else [binaries[p.id], "login", "status"])
                  for p in PROVIDERS if p.id in {"claude","codex"} and binaries[p.id]}
        auth = {name:check.result() for name,check in checks.items()}
    for provider in PROVIDERS:
        binary = binaries[provider.id]
        status = "not-configured"
        detail = "Requires a verified provider integration"
        if provider.id == "ollama":
            status = "installed" if binary else "not-installed"
            detail = "Local coding model" if binary else "Install Ollama during full setup"
        elif provider.id == "claude":
            result = auth.get("claude")
            status = "authenticated" if result is True else "sign-in-needed" if result is False else "auth-unverified" if binary else "not-installed"
            detail = "Official Claude CLI confirmed sign-in" if result is True else "Sign in with the official Claude CLI" if result is False else "Auth check did not complete" if binary else "CLI not found. If Claude works in PowerShell, restart this desktop or refresh your PATH."
        elif provider.id == "codex":
            result = auth.get("codex")
            status = "authenticated" if result is True else "sign-in-needed" if result is False else "auth-unverified" if binary else "not-installed"
            detail = "Official Codex CLI confirmed ChatGPT sign-in" if result is True else "Sign in with ChatGPT through Codex" if result is False else "Auth check did not complete" if binary else "Codex CLI not found in desktop PATH or user launcher folders. Restart this desktop after installing it."
        elif provider.id == "gemini":
            status = "installed" if binary else "not-installed"
            detail = "Sign in within Gemini CLI; autonomous core routing not implemented" if binary else "Gemini CLI not installed"
        elif provider.id == "grok":
            status = "api-key-configured" if is_key_set("XAI_API_KEY") else "api-key-needed"
            detail = "Key detected; API use requires explicit billing approval and core adapter" if is_key_set("XAI_API_KEY") else "No xAI API key configured"
        elif provider.id == "meta":
            status = "local-capability" if binary else "not-installed"
            detail = "Install a compatible Llama model using Ollama" if binary else "Ollama not installed"
        elif provider.id == "muse":
            status = "planned"
            detail = "No verified developer-auth workflow wired; not available for execution"
        entries.append({"id":provider.id,"name":provider.label,"group":provider.group,"description":provider.description,
                        "status":status,"detail":detail,"adapter":provider.adapter,"docs":provider.docs,
                        "sign_in_available":bool(provider.login and binary),
                        "authenticated":bool(status == "authenticated"),
                        "core_enabled": provider.adapter in {"local", "supervisor"}})
    return entries



def engine_capabilities(cli_command: list[str] | None) -> dict:
    """Read-only checks of the *installed* Coding Brain, never of this UI process.

    A running loopback UI is NOT evidence of a working engine, chat capability,
    or a live Ollama model. Do not infer support merely from a version number.
    """
    result = {"available": False, "version": None, "conversation": False,
              "typed_api": False, "task_command": False, "detail": "Coding Brain is not installed"}
    if not cli_command:
        return result
    try:
        version = subprocess.run([*cli_command, "version"], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, errors="replace", timeout=6,
                                 check=False)
        if version.returncode:
            result["detail"] = "Installed Coding Brain could not start; repair its Python environment"
            return result
        import re
        match = re.search(r"\b(?:codingbrain\s+)?v?(\d+\.\d+(?:\.\d+){0,2}(?:[.a-z0-9+-]+)?)\b",
                          version.stdout, re.I)
        result["version"] = match.group(1) if match else None
        result["available"] = True
        result["task_command"] = True  # availability of run command != sandbox/model readiness
        result["detail"] = "Engine responds to version probe; execution readiness is not yet verified"
        for command, key in (("chat", "conversation"), ("api", "typed_api")):
            try:
                probe = subprocess.run([*cli_command, command, "--help"],
                                       stdin=subprocess.DEVNULL, capture_output=True,
                                       text=True, errors="replace", timeout=6, check=False)
                result[key] = probe.returncode == 0 and "usage:" in probe.stdout.lower()
            except (OSError, subprocess.TimeoutExpired):
                result[key] = False
        if result["typed_api"]:
            result["conversation"] = True
        return result
    except (OSError, subprocess.TimeoutExpired):
        result["detail"] = "Could not start installed Coding Brain; check its Python environment"
        return result


def local_ollama_models() -> dict:
    """Read-only local Ollama tags; no inference request, no proxy, no remote host."""
    from urllib.request import ProxyHandler, Request, build_opener
    from urllib.error import HTTPError, URLError
    result = {"running": False, "models": [], "detail": "Ollama service is not reachable on localhost:11434"}
    try:
        request = Request("http://127.0.0.1:11434/api/tags", headers={"Accept": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            payload = json.load(response)
        result["running"] = True
        result["models"] = [str(item["name"])[:160] for item in payload.get("models", [])[:100]
                            if isinstance(item, dict) and isinstance(item.get("name"), str)]
        result["detail"] = "Ollama is reachable; a model generation test is still required for full readiness"
    except (ValueError, TypeError, KeyError, HTTPError, URLError, TimeoutError, OSError):
        pass
    return result


def configured_model(config_path: Path) -> dict:
    """Return only non-sensitive model names and routing type, never configuration secrets."""
    try:
        configuration = json.loads(config_path.read_text(encoding="utf-8"))
        models = configuration.get("models") or {}
        return {"provider": str(models.get("provider") or "unknown")[:80],
                "model": str(models.get("model") or "")[:160]}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"provider": "unknown", "model": ""}

def installation_probes(cli_command: list[str] | None = None, engine_providers: list[dict] | None = None) -> dict:
    """Cheap preflight: installation presence, not fake deep readiness. For Claude and Codex the
    installed engine's providers.list (API 1.0) wins over this desktop's own lookup."""
    required = ("git", "python", "docker", "ollama", "claude", "codex")
    engine_view = {item.get("id"): item for item in engine_providers or []}
    components = []
    for name in required:
        described = engine_view.get(name) if name in {"claude", "codex"} else None
        if described is not None:
            path, checked = described.get("cli_path"), described.get("checked") or []
            installed, source = bool(described.get("installed")), "engine"
        else:
            path, checked = locate_cli(name)
            if name == "python" and not path:
                path = installed_cli("python3") or sys.executable
            installed, source = bool(path), "desktop"
        if installed:
            detail = f"Found at {path}" if path else "Found by the installed engine"
        else:
            places = [item for item in checked if item != "PATH"]
            detail = ("Not found on PATH" + (f" or in {len(places)} other locations checked" if places else "")
                      + ". Installed it recently? Use Recheck; restart the desktop only if it is still missing.")
        components.append({"id": name, "name": name.title(), "installed": installed, "detail": detail,
                           "path": path, "checked": checked[:40], "source": source})
    engine = bool(cli_command)
    return {"engine_installed":engine,"components":components,
            "full_installer_available":False,
            "full_installer_note":"Full installer is part of the pending Coding Brain backend release."}


def supports_cli_option(cli_command: list[str], command: str, option: str) -> bool:
    """Inspect help only; never invoke a potentially expensive flag as a capability probe.

    Stable v0.9.0 has `doctor` but no `doctor --full`. The UI must not claim
    deep verification on that release or run an unsupported command.
    """
    if command != "doctor" or option != "--full":
        raise ValueError("Unsupported capability probe")
    try:
        result = subprocess.run([*cli_command, command, "--help"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, errors="replace", timeout=8, check=False)
        return result.returncode == 0 and "--full" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


ALLOWED_ACTIONS = {
    "check_updates": ("update", "--check"),
    "update_engine": ("update",),
    "install_plan": ("install", "--profile", "full", "--plan", "--plain"),
    "install_full": ("install", "--profile", "full"),
    "repair": ("setup", "--repair"),
    "doctor": ("doctor", "--full"),
}


def maintenance_args(action: str, cli_command: list[str], *, deep_doctor: bool = True) -> list[str]:
    """Allowlist prevents arbitrary shell invocation through HTTP input."""
    if action not in ALLOWED_ACTIONS:
        raise ValueError("Unsupported system action")
    if action == "doctor" and not deep_doctor:
        return [*cli_command, "doctor"]  # honest basic check on v0.9.0
    return [*cli_command, *ALLOWED_ACTIONS[action]]


def launch_interactive_windows(command: list[str], *, platform: str | None = None):
    """Official account sign-ins and system setup require their own visible terminal.

    No --yes, no stdin pipe, and never capture login output/credentials in UI logs.
    """
    platform = platform or os.name
    if platform != "nt":
        raise ValueError("Interactive installation/sign-in is available in the Windows desktop app only")
    subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)


def provider_auth_command(provider_id: str) -> list[str]:
    provider = BY_ID.get(provider_id)
    if not provider or not provider.login or not provider.executable:
        raise ValueError("This provider does not have a supported interactive sign-in integration")
    binary = installed_cli(provider.executable)
    if not binary:
        raise ValueError(f"{provider.label} is not installed; run full setup first")
    return [binary, *provider.login[1:]]
