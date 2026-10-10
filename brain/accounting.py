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
# The live activity journal for the current task: callable(event_type, **fields), set by the service.
SINK: ContextVar = ContextVar("coding_brain_activity_sink", default=None)
HEARTBEAT_SECONDS = float(__import__("os").environ.get("BRAIN_HEARTBEAT_SECONDS", "10"))
AGENTS = {"implementer": "implementer", "reviewer": "reviewer", "coordinator": "coordinator", "fast": "implementer",
          "plan": "supervisor", "diagnose": "supervisor", "review": "supervisor", "decompose": "supervisor",
          "vision_describe": "vision", "vision_compare": "vision"}


def emit(event_type: str, /, **fields):
    """Report live activity to the bound journal; a no-op when nothing is listening. Never raises."""
    sink = SINK.get()
    if sink is None:
        return
    try:
        sink(event_type, **fields)
    except Exception:
        pass


@contextmanager
def bind(sink):
    token = SINK.set(sink)
    try:
        yield
    finally:
        SINK.reset(token)


class request:
    """`async with accounting.request(role, provider, model):` around one model call: a started
    event, a heartbeat every HEARTBEAT_SECONDS while it is in flight (so a waiting user can tell an
    active request from a stalled one), and a completed or failed event with its duration."""

    def __init__(self, role: str, provider: str, model: str, summary: str = ""):
        self.role, self.provider, self.model, self.summary = role, provider, model, summary
        self.agent = AGENTS.get(role, role)

    async def __aenter__(self):
        import asyncio
        self.started = time.monotonic()
        emit("model_request", status="RUNNING", agent=self.agent, provider=self.provider, model=self.model,
             summary=self.summary or f"{self.role} request", data={"role": self.role})
        self._beat = asyncio.ensure_future(self._heartbeat()) if SINK.get() is not None else None
        return self

    async def _heartbeat(self):
        import asyncio
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            emit("heartbeat", status="RUNNING", agent=self.agent, provider=self.provider, model=self.model,
                 duration=time.monotonic() - self.started,
                 summary=f"{self.role} request in flight for {int(time.monotonic() - self.started)} s",
                 data={"role": self.role})

    async def __aexit__(self, kind, error, traceback):
        if self._beat:
            self._beat.cancel()
        failed = kind is not None
        emit("model_response", status="FAILED" if failed else "COMPLETED", agent=self.agent, provider=self.provider,
             model=self.model, duration=time.monotonic() - self.started,
             summary=(f"{self.role} request failed: {kind.__name__}: {str(error)[:300]}" if failed
                      else f"{self.role} response received"), data={"role": self.role})
        return False


def record(entry_type: str, /, **fields):
    """Report one event: inference, route, fallback, or escalation. A no-op outside collection."""
    if entry_type in {"inference", "fallback", "escalation", "route"}:
        emit(entry_type, status="COMPLETED" if entry_type != "fallback" else "RETRYING",
             agent=AGENTS.get(fields.get("role", ""), fields.get("role")), provider=fields.get("provider"),
             model=fields.get("model") or fields.get("to"), summary=_describe(entry_type, fields), data=fields)
    log = INFERENCE_LOG.get()
    if log is not None:
        log.append({"type": entry_type, "at": round(time.time(), 3),
                    **{key: value for key, value in fields.items() if value is not None}})


def _describe(entry_type: str, fields: dict) -> str:
    if entry_type == "inference":
        tokens = [f"{fields[key]} {label}" for key, label in (("prompt_tokens", "in"), ("output_tokens", "out"))
                  if fields.get(key) is not None]
        return f"{fields.get('role')} served by {fields.get('model')} ({', '.join(tokens) or 'tokens unknown'})"
    if entry_type == "fallback":
        return f"{fields.get('method')}: fell back to {fields.get('to')} ({str(fields.get('reason', ''))[:200]})"
    if entry_type == "escalation":
        return f"{fields.get('kind')} consultation with {fields.get('supervisor')}: {'ok' if fields.get('ok') else 'failed'}"
    return f"{fields.get('role')} routed to {fields.get('model')} ({fields.get('strategy')})"


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
