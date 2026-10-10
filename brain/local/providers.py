"""Provider descriptors: what each model provider is, whether it is installed, signed in and
usable, what it may do and how it is billed. The desktop app and `codingbrain api` show these as
they are; an installed CLI is not reported as usable, and a provider without a reviewed adapter is
never reported as connected.

Fields: id, name, role, installed, authentication, capabilities, enabled, readiness
(ready | needs_setup | needs_sign_in | unavailable | not_supported), billing_type, cost_gate,
last_error, supported_actions, adapter.
"""
from .components import Env, check_all

AUTH = {"authenticated": "signed_in", "disabled": "signed_in", "not_authenticated": "signed_out",
        "api_key_billing": "api_key_refused", "expired": "expired", "temporarily_unavailable": "unknown",
        "not_installed": "not_applicable"}


def descriptors(env: Env, deep: bool = False) -> list[dict]:
    results = check_all(env, ["ollama", "model", "claude", "codex"], deep=deep)
    config = env.config
    ollama, model = results["ollama"], results["model"]
    local = {
        "id": "ollama", "name": f"Local model ({config['models'].get('model') or 'not chosen'})",
        "role": "primary worker: planning, coding, review",
        "installed": ollama.state != "missing", "authentication": "not_applicable",
        "capabilities": ["chat", "code", "review"], "enabled": True,
        "readiness": ("ready" if ollama.ready and model.ready and not (model.data or {}).get("unverified") else
                      "needs_setup" if not (ollama.ready and model.ready) else "unverified"),
        "billing_type": "local", "cost_gate": None,
        "last_error": None if ollama.ready and model.ready else (model.detail if ollama.ready else ollama.detail),
        "supported_actions": ["install", "pull_model", "self_test"], "adapter": "ollama",
    }
    found = [local]
    for name, title in (("claude", "Claude Code"), ("codex", "OpenAI Codex")):
        status = results[name]
        auth = (status.data or {}).get("auth", "not_installed")
        enabled = bool(config["supervisors"].get(name, {}).get("enabled"))
        ready = status.ready and auth == "authenticated" and enabled
        found.append({
            "id": name, "name": title, "role": "optional planner, reviewer and escalation supervisor",
            "installed": status.state != "missing", "authentication": AUTH.get(auth, "unknown"),
            "capabilities": ["plan", "diagnose", "review"], "enabled": enabled,
            "readiness": ("ready" if ready else "needs_setup" if status.state == "missing" else
                          "needs_sign_in" if status.state == "needs_sign_in" else
                          "unavailable" if status.state == "unavailable" else "disabled" if not enabled else "unknown"),
            "billing_type": "subscription", "cost_gate": {
                "per_task": {key: config["budgets"].get(key) for key in ("plan_budget", "diagnose_budget",
                                                                          "review_budget")},
                "daily_limit": config["budgets"].get("daily_limit"), "api_key_billing": "refused"},
            "last_error": None if ready else status.detail,
            "cli_path": (status.data or {}).get("path"), "checked": (status.data or {}).get("checked") or [],
            "supported_actions": ["install", "sign_in", "enable", "disable"], "adapter": f"{name}_cli",
        })
    unsupported = "No reviewed adapter yet: shown for information only, never selectable or reported as connected."
    found += [
        {"id": "gemini", "name": "Google Gemini", "role": "not integrated", "installed": bool(env.system.which("gemini")),
         "authentication": "unknown", "capabilities": [], "enabled": False, "readiness": "not_supported",
         "billing_type": "unknown (subscription, free quota or API billing must be verified)", "cost_gate": None,
         "last_error": unsupported, "supported_actions": [], "adapter": None},
        {"id": "grok", "name": "xAI Grok", "role": "not integrated", "installed": False, "authentication": "unknown",
         "capabilities": [], "enabled": False, "readiness": "not_supported", "billing_type": "api_key",
         "cost_gate": "explicit per-provider budget and consent required", "last_error": unsupported,
         "supported_actions": [], "adapter": None},
        {"id": "meta-llama", "name": "Meta Llama (local, through Ollama)", "role": "alternative local model",
         "installed": ollama.state != "missing", "authentication": "not_applicable", "capabilities": ["chat", "code"],
         "enabled": False, "readiness": "needs_setup", "billing_type": "local", "cost_gate": None,
         "last_error": "Choose a Llama model with `codingbrain setup --model llama3.1:8b` (no Meta account is involved)",
         "supported_actions": ["pull_model"], "adapter": "ollama"},
        {"id": "muse", "name": "Muse (unidentified)", "role": "not integrated", "installed": False,
         "authentication": "unknown", "capabilities": [], "enabled": False, "readiness": "not_supported",
         "billing_type": "unknown", "cost_gate": None,
         "last_error": "The vendor, product and documented SDK/sign-in have not been identified; no availability or "
                       "login is offered.", "supported_actions": [], "adapter": None},
    ]
    return found

