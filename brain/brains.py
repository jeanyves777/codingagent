"""Multiple named model backends ("brains") with per-role ordered failover."""
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from . import accounting
from .approvals import ApprovalRequired

NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROVIDERS = {"ollama", "openai", "anthropic"}
ROLES = {"implementer", "fast", "reviewer", "coordinator"}
DEFAULT_URLS = {"ollama": "http://localhost:11434"}


def _host_kind(host: str | None) -> str | None:
    """Classify a host as "loopback", "lan", or None for anything else."""
    if host == "localhost":
        return "loopback"
    if not host:
        return None
    if host.endswith(".local"):
        return "lan"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return "loopback" if address.is_loopback else "lan" if address.is_private else None


def validate_url(url: str, has_key: bool) -> str:
    """Allow HTTPS anywhere and plain HTTP only on this machine or the local network.
    An API key is never sent over plain HTTP to another machine."""
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("Brain URLs cannot contain credentials, queries, or fragments")
    if parsed.scheme == "https" and parsed.hostname:
        return url
    kind = _host_kind(parsed.hostname) if parsed.scheme == "http" else None
    if kind is None:
        raise ValueError("Brain URL must use HTTPS, or HTTP on this machine or the local network")
    if has_key and kind != "loopback":
        raise ValueError("API keys are only sent over HTTPS or to this machine")
    return url


class ImplementerUnavailable(RuntimeError):
    """No free implementer could be reached. This pauses work; it never triggers escalation."""


def is_unavailable(error: BaseException) -> bool:
    """True when an error means the model endpoint could not be reached at all."""
    import httpx
    if isinstance(error, (ImplementerUnavailable, httpx.ConnectError, httpx.ConnectTimeout,
                          ConnectionError)):
        return True
    return type(error).__name__ == "APIConnectionError"


class FailoverModel:
    """Try each brain in order; move to the next one when a brain is unreachable or misbehaves.

    A pending tool approval is never treated as a failure, so a side-effecting request is not
    re-issued to another brain.
    """

    def __init__(self, models: list):
        if not models:
            raise ValueError("A role needs at least one brain")
        self.models = models
        self.name = "+".join(getattr(model, "name", "unknown") for model in models)
        self.served = []

    @property
    def usage(self):
        return [item for model in self.models for item in getattr(model, "usage", [])]

    @property
    def mcp_gateway(self):
        return getattr(self.models[0], "mcp_gateway", None)

    async def _call(self, method, *args, **kwargs):
        errors, unreachable, skipped = [], 0, []
        for model in self.models:
            try:
                result = await getattr(model, method)(*args, **kwargs)
            except ApprovalRequired:
                raise
            except Exception as error:
                unreachable += is_unavailable(error)
                errors.append(f"{getattr(model, 'name', 'unknown')}: {type(error).__name__}: {error}"[:300])
                skipped.append((getattr(model, "name", "unknown"), type(error).__name__))
                continue
            if skipped:
                accounting.record("fallback", method=method, to=getattr(model, "name", "unknown"),
                                  **{"from": [name for name, _ in skipped]},
                                  reason=", ".join(reason for _, reason in skipped))
            self.served.append({"method": method, "model": getattr(model, "name", "unknown"),
                                "failed_over": len(errors)})
            del self.served[:-200]
            return result
        accounting.record("fallback", method=method, **{"from": [name for name, _ in skipped]},
                          reason="every brain failed")
        if unreachable == len(self.models):
            raise ImplementerUnavailable("No brain is reachable: " + " | ".join(errors))
        raise ValueError("All brains failed: " + " | ".join(errors))

    async def propose(self, root, goal, memories, repository_context=None, task_id=None):
        return await self._call("propose", root, goal, memories, repository_context, task_id=task_id)

    async def review(self, goal, diff):
        return await self._call("review", goal, diff)

    async def decompose(self, goal):
        return await self._call("decompose", goal)


def build_brain_model(spec: dict, options: dict, gateway=None):
    provider = spec["provider"]
    if provider == "ollama":
        from .model import OllamaModel
        return OllamaModel(spec["url"], spec["model"], mcp_gateway=gateway, **options)
    if provider == "openai":
        from .openai_compatible import OpenAICompatibleModel
        key = os.environ.get(spec["api_key_env"]) if spec.get("api_key_env") else None
        if spec.get("api_key_env") and not key:
            raise RuntimeError(f"Set {spec['api_key_env']} for brain {spec['name']}")
        return OpenAICompatibleModel(spec["url"], spec["model"], api_key=key, mcp_gateway=gateway,
                                     **options)
    from .anthropic_model import AnthropicModel
    return AnthropicModel(spec["model"], mcp_gateway=gateway, effort=spec.get("effort", "high"),
                          fallbacks=spec.get("fallbacks", True), **options)


def load_brains(path: Path) -> dict:
    """Validate a brains file and return {"brains": {name: spec}, "roles": {role: [names]}}."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) - {"brains", "roles", "supervisors", "supervision"} or not isinstance(
            payload.get("brains"), dict):
        raise ValueError("Brains configuration must contain a brains object and optional roles, "
                         "supervisors, and supervision")
    brains = {}
    for name, spec in payload["brains"].items():
        if not NAME.fullmatch(name) or not isinstance(spec, dict):
            raise ValueError("Invalid brain name or configuration")
        if set(spec) - {"provider", "url", "model", "api_key_env", "effort", "fallbacks"}:
            raise ValueError(f"Unknown key in brain {name}")
        provider = spec.get("provider")
        if provider not in PROVIDERS or not isinstance(spec.get("model"), str) or not spec["model"]:
            raise ValueError(f"Brain {name} needs a provider ({', '.join(sorted(PROVIDERS))}) and model")
        if spec.get("api_key_env") is not None and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", spec["api_key_env"]):
            raise ValueError(f"Brain {name} api_key_env must name an environment variable")
        url = spec.get("url", DEFAULT_URLS.get(provider))
        if provider == "anthropic":
            url = None
        elif not url:
            raise ValueError(f"Brain {name} needs a url")
        else:
            validate_url(url, bool(spec.get("api_key_env")))
        brains[name] = {**spec, "name": name, "url": url}
    if not brains:
        raise ValueError("Configure at least one brain")
    roles = payload.get("roles", {})
    if not isinstance(roles, dict) or set(roles) - ROLES:
        raise ValueError(f"Roles must be among {', '.join(sorted(ROLES))}")
    resolved = {}
    for role in ROLES:
        names = roles.get(role) or roles.get("implementer") or list(brains)
        if not isinstance(names, list) or any(name not in brains for name in names):
            raise ValueError(f"Role {role} names an unknown brain")
        resolved[role] = list(dict.fromkeys(names))
    return {"brains": brains, "roles": resolved, **load_supervisors(payload)}


def load_supervisors(payload: dict) -> dict:
    """Validate the optional premium supervisors and their policy."""
    from .supervision import SupervisionPolicy
    supervisors = payload.get("supervisors", {})
    if not isinstance(supervisors, dict):
        raise ValueError("supervisors must be an object")
    checked = {}
    for name, spec in supervisors.items():
        if not NAME.fullmatch(name) or not isinstance(spec, dict):
            raise ValueError("Invalid supervisor name or configuration")
        if set(spec) - {"provider", "model", "command", "timeout", "allow_api_billing"}:
            raise ValueError(f"Unknown key in supervisor {name}")
        if spec.get("provider") not in {"claude_cli", "codex_cli"}:
            raise ValueError(f"Supervisor {name} provider must be claude_cli or codex_cli")
        checked[name] = spec
    policy = dict(payload.get("supervision", {}))
    order = policy.pop("order", list(checked))
    if not isinstance(order, list) or any(name not in checked for name in order):
        raise ValueError("supervision.order names an unknown supervisor")
    unknown = set(policy) - set(SupervisionPolicy.DEFAULTS)
    if unknown:
        raise ValueError("Unknown supervision setting: " + ", ".join(sorted(unknown)))
    return {"supervisors": {name: checked[name] for name in order}, "supervision": policy}


def build_supervision(config: dict, data: Path):
    """Return a SupervisionPolicy, or None when no supervisor is configured."""
    from .subscriptions import SUPERVISORS
    from .supervision import SupervisionPolicy, SupervisorLedger
    if not config.get("supervisors"):
        return None
    supervisors = [SUPERVISORS[spec["provider"]](
        name, model=spec.get("model"), command=spec.get("command"),
        timeout=int(spec.get("timeout", 900)), allow_api_billing=bool(spec.get("allow_api_billing")))
        for name, spec in config["supervisors"].items()]
    return SupervisionPolicy(supervisors, SupervisorLedger(data / "supervision.sqlite3"),
                             **config.get("supervision", {}))


def build_roles(config: dict, options: dict, gateway=None) -> dict:
    """Return one model (or failover chain) per role."""
    built = {}
    for role, names in config["roles"].items():
        models = [build_brain_model(config["brains"][name], options,
                                    gateway if role in {"implementer", "fast"} else None)
                  for name in names]
        built[role] = models[0] if len(models) == 1 else FailoverModel(models)
    return built
