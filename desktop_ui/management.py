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


def installed_cli(name: str) -> str | None:
    return shutil.which(name)


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
            detail = "CLI authenticated" if result is True else "Use official Claude CLI to sign in" if binary else "Install from full setup"
        elif provider.id == "codex":
            result = auth.get("codex")
            status = "authenticated" if result is True else "sign-in-needed" if result is False else "auth-unverified" if binary else "not-installed"
            detail = "CLI authenticated" if result is True else "Sign in with ChatGPT" if binary else "Install from full setup"
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
                        "core_enabled": provider.adapter in {"local", "supervisor"}})
    return entries


def installation_probes(cli_command: list[str] | None = None) -> dict:
    """Cheap preflight: installation presence, not fake deep readiness."""
    required = ("git", "python", "docker", "ollama", "claude", "codex")
    components = []
    for name in required:
        binary = installed_cli(name)
        if name == "python" and not binary:
            binary = installed_cli("python3") or sys.executable
        components.append({"id":name, "name":name.title(), "installed":bool(binary),
                           "detail":"Found on this computer" if binary else "Missing from PATH"})
    engine = bool(cli_command)
    return {"engine_installed":engine,"components":components,
            "full_installer_available":False,
            "full_installer_note":"Full installer is part of the pending Coding Brain backend release."}


ALLOWED_ACTIONS = {
    "check_updates": ("update", "--check"),
    "update_engine": ("update",),
    "install_plan": ("install", "--profile", "full", "--plan", "--plain"),
    "install_full": ("install", "--profile", "full"),
    "repair": ("setup", "--repair"),
    "doctor": ("doctor", "--full"),
}


def maintenance_args(action: str, cli_command: list[str]) -> list[str]:
    """Allowlist prevents arbitrary shell invocation through HTTP input."""
    if action not in ALLOWED_ACTIONS:
        raise ValueError("Unsupported system action")
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
