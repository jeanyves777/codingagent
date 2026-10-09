"""Local settings: models, premium supervisors and budgets, approvals, permitted directories.

Credentials are never stored here. Free hosted providers name an environment variable that holds
their key (api_key_env); Claude and Codex are used only through their own signed-in CLIs.
"""
import copy
import json
import os
from pathlib import Path

from .paths import Layout

SCHEMA_VERSION = 1
BUDGET_KEYS = ("escalate_after", "plan_budget", "diagnose_budget", "decompose_budget", "review_budget",
               "daily_limit")
DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "models": {"provider": "ollama", "url": "http://localhost:11434", "model": "", "fast_model": "",
               "extra_brains": {}},
    "supervisors": {"claude": {"enabled": False, "model": None}, "codex": {"enabled": False, "model": None}},
    "budgets": {"escalate_after": 2, "plan_budget": 1, "diagnose_budget": 1, "decompose_budget": 1,
                "review_budget": 0, "daily_limit": 20},
    # propose: show each plan and ask before running it; auto: run plans, still ask before accepting.
    "autonomy": {"execution": "propose", "requirement_checks": True},
    # Empty means any directory; otherwise Coding Brain only works on projects under these roots.
    "permissions": {"allowed_roots": []},
    "update": {"channel": "stable"},
    "sandbox": {"python_image": "coding-brain-sandbox:0.1", "node_image": "coding-brain-node-sandbox:0.1"},
    # Multimodal (0.11): a vision model is separate from the coding model. Empty url = models.url.
    # premium: "off", "claude" or "codex"; even when set, each task needs --allow-premium-vision.
    "vision": {"provider": "ollama", "url": "", "model": "", "supports_images": False, "api_key_env": "",
               "premium": "off"},
    "ocr": {"engine": "tesseract", "command": "", "languages": "eng"},
    # retention of attachment copies: "task" (until the task is accepted or purged), "keep", "none".
    "attachments": {"retention": "task", "allowed_roots": [], "limits": {}, "remember": True},
    "visual": {"enabled": True, "viewports": ["desktop", "tablet", "mobile"], "max_repairs": 2,
               "browser_channel": "", "browser_executable": "", "accessibility_blocking": ["critical"],
               "pixel_threshold": 0.35},
    "multimodal_budgets": {"max_ocr_images": 12, "max_vision_images": 6, "max_premium_vision_calls": 2},
}


def _merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)  # never share (and later mutate) the nested defaults
    for key, value in override.items():
        merged[key] = _merge(base[key], value) if isinstance(base.get(key), dict) and isinstance(value, dict) else value
    return merged


def load(layout: Layout) -> dict:
    path = layout.config / "config.json"
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _merge(DEFAULTS, stored)


def save(layout: Layout, config: dict) -> Path:
    layout.config.mkdir(parents=True, exist_ok=True)
    path = layout.config / "config.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def configured(config: dict) -> bool:
    return bool(config["models"].get("model"))


def brains_config(config: dict) -> dict:
    """The existing brains-file format (brain.brains.load_brains) for the configured models."""
    models = config["models"]
    brains = {"main": {"provider": models["provider"], "model": models["model"]}}
    if models["provider"] != "anthropic" and models.get("url"):
        brains["main"]["url"] = models["url"]
    if models.get("api_key_env"):
        brains["main"]["api_key_env"] = models["api_key_env"]
    roles = {"implementer": ["main"], "reviewer": ["main"], "coordinator": ["main"], "fast": ["main"]}
    if models.get("fast_model") and models["fast_model"] != models["model"]:
        brains["fast"] = {**brains["main"], "model": models["fast_model"]}
        roles["fast"] = ["fast", "main"]
    for name, spec in (models.get("extra_brains") or {}).items():
        brains[name] = spec  # extra free brains are failover after the main one
        for role in roles:
            roles[role].append(name)
    payload = {"brains": brains, "roles": roles}
    supervisors = {name: {"provider": f"{name}_cli", **({"model": spec["model"]} if spec.get("model") else {})}
                   for name, spec in config["supervisors"].items() if spec.get("enabled")}
    if supervisors:
        payload["supervisors"] = supervisors
        payload["supervision"] = {"order": list(supervisors),
                                  **{key: config["budgets"][key] for key in BUDGET_KEYS if key in config["budgets"]}}
    return payload


def project_environment(layout: Layout, config: dict, project_root: Path, project_data: Path) -> dict:
    """Environment for the existing runtime factory (brain.factory.build_brain_from_env)."""
    brains_file = layout.config / "brains.json"
    brains_file.write_text(json.dumps(brains_config(config), indent=2) + "\n", encoding="utf-8")
    sandbox = config["sandbox"]
    return {
        "BRAIN_REPOSITORIES": str(project_root.parent),
        "BRAIN_DATA": str(project_data),
        "BRAIN_BRAINS_CONFIG": str(brains_file),
        "BRAIN_KNOWLEDGE_DB": str(layout.data / "knowledge.sqlite3"),
        # One premium ledger for the whole computer, so daily limits hold across projects.
        "BRAIN_SUPERVISION_LEDGER": str(layout.data / "supervision.sqlite3"),
        "BRAIN_REQUIREMENT_CHECKS": "true" if config["autonomy"].get("requirement_checks", True) else "false",
        "BRAIN_PYTHON_SANDBOX_IMAGE": sandbox["python_image"],
        "BRAIN_NODE_SANDBOX_IMAGE": sandbox["node_image"],
    }


def permitted(config: dict, project_root: Path) -> bool:
    roots = [Path(root).expanduser().resolve() for root in config["permissions"].get("allowed_roots") or []]
    return not roots or any(project_root.resolve().is_relative_to(root) for root in roots)


def vision_settings(config: dict) -> dict:
    vision = dict(config["vision"])
    if not vision.get("url"):
        vision["url"] = config["models"]["url"] if vision.get("provider", "ollama") == config["models"]["provider"] \
            else "http://localhost:11434"
    return vision
