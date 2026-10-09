"""Model-level accounting: which model actually handled each inference, fallback and escalation.

Adapters, the failover wrapper, the router and the supervision policy report here; the service
collects the entries into the task they belong to (task["inference_log"]). Collection is scoped
with a context variable, so concurrent tasks never mix their entries. Entries hold model names,
roles and token counts only, never prompts or outputs.
"""
import time
from contextlib import contextmanager
from contextvars import ContextVar

INFERENCE_LOG: ContextVar[list | None] = ContextVar("coding_brain_inference_log", default=None)
LIMIT = 500


def record(entry_type: str, /, **fields):
    """Report one event: inference, route, fallback, or escalation. A no-op outside collection."""
    log = INFERENCE_LOG.get()
    if log is not None:
        log.append({"type": entry_type, "at": round(time.time(), 3),
                    **{key: value for key, value in fields.items() if value is not None}})


@contextmanager
def collect(target: list | dict):
    """Collect entries into a task (its inference_log) or a list, for the enclosed calls."""
    log = target.setdefault("inference_log", []) if isinstance(target, dict) else target
    token = INFERENCE_LOG.set(log)
    try:
        yield log
    finally:
        INFERENCE_LOG.reset(token)
        del log[:-LIMIT]


def summarize(entries: list[dict]) -> dict:
    """Per-model calls and tokens, fallbacks taken, and premium escalations."""
    models, fallbacks, escalations, routes = {}, [], [], {}
    for entry in entries:
        if entry.get("type") == "inference":
            key = entry.get("model", "unknown")
            item = models.setdefault(key, {"provider": entry.get("provider"), "calls": 0, "roles": {},
                                           "prompt_tokens": 0, "output_tokens": 0})
            item["calls"] += 1
            item["roles"][entry.get("role", "unknown")] = item["roles"].get(entry.get("role", "unknown"), 0) + 1
            item["prompt_tokens"] += entry.get("prompt_tokens") or 0
            item["output_tokens"] += entry.get("output_tokens") or 0
            if entry.get("requested") and entry["requested"] != key:
                item["served_instead_of"] = entry["requested"]
        elif entry.get("type") == "fallback":
            fallbacks.append({key: entry.get(key) for key in ("method", "from", "to", "reason")})
        elif entry.get("type") == "escalation":
            escalations.append({key: entry.get(key) for key in ("kind", "supervisor", "model", "ok")})
        elif entry.get("type") == "route":
            name = f"{entry.get('role')}->{entry.get('model')}"
            routes[name] = routes.get(name, 0) + 1
    return {"models": models, "routes": routes, "fallbacks": len(fallbacks), "fallback_detail": fallbacks[:20],
            "escalations": len(escalations), "escalation_detail": escalations[:20]}
