"""Live activity in the terminal, and the trace / activity / snapshot views.

Everything shown comes from the journal (brain.telemetry) as it is written: stages with real
timestamps and durations, the responsible agent and model, model-provided explanations and
observed decisions, tool calls, test results and approvals. While a model request is in flight a
status line gives its elapsed time and the age of its last heartbeat; a missing heartbeat is
reported as a possible stall rather than hidden. No percentages or invented progress.

Modes: live (default), plain (no in-place status line; also used without a terminal), quiet
(failures, approvals and outcomes), verbose (adds heartbeats, routes, snapshots), json (one JSON
event per line).
"""
import json
import sys
import threading
import time

from .. import accounting
from ..activity import PHASES

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}
FANCY = {"RUNNING": "→", "COMPLETED": "✓", "FAILED": "✗", "BLOCKED": "■", "RETRYING": "↻",
         "WAITING_APPROVAL": "⏸", "CANCELLED": "×", "PENDING": "·", "INFO": "·"}
PLAIN = {"RUNNING": ">", "COMPLETED": "+", "FAILED": "x", "BLOCKED": "!", "RETRYING": "~",
         "WAITING_APPROVAL": "?", "CANCELLED": "-", "PENDING": ".", "INFO": "."}


def glyphs(stream=None) -> dict:
    stream = stream or sys.stdout
    try:
        "".join(FANCY.values()).encode(stream.encoding or "ascii")
        return FANCY
    except (UnicodeEncodeError, LookupError):
        return PLAIN  # e.g. Windows PowerShell with a legacy code page


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60:02d}:{seconds % 60:02d}"


def who(event: dict) -> str:
    agent = (event.get("agent") or "coding_brain").replace("_", " ")
    model = event.get("model")
    label = {"coding brain": "Coding Brain", "implementer": "Implementer", "reviewer": "Reviewer",
             "coordinator": "Coordinator", "supervisor": "Supervisor", "sandbox": "Sandbox", "browser": "Browser",
             "vision": "Vision", "user": "You"}.get(agent, agent.title())
    return f"{label} ({model})" if model and model != "cli-default" else label


def describe(event: dict, mode: str = "live") -> str | None:
    """One line for an event, or None when this mode does not show it."""
    kind, status, phase = event["event_type"], event.get("status") or "INFO", event.get("phase")
    title = PHASES.get(phase, phase or "")
    if kind == "stage":
        if status == "RUNNING":
            return f"{who(event)} — {event.get('summary') or title}"
        if mode == "quiet" and status == "COMPLETED":
            return None
        duration = f" ({event['duration']:.1f} s)" if event.get("duration") is not None else ""
        return f"{title}{duration}" if status == "COMPLETED" else f"{event.get('summary')}{duration}"
    if kind == "decision":
        if mode == "quiet":
            return None
        source = (event.get("data") or {}).get("source", "observed")
        label = "explanation (model-provided)" if source == "model-provided" else "observed"
        files = (event.get("data") or {}).get("files")
        text = " ".join(str(event.get("summary") or "").split())
        return f"{who(event)} {label}: {text[:400]}" + (f"\n      files: {', '.join(files)}" if files else "")
    if kind == "inference":
        return None if mode == "quiet" else f"  {event.get('summary')}"
    if kind == "model_request":
        return None if mode in {"quiet", "live", "plain"} else f"{who(event)} request started: {event.get('summary')}"
    if kind == "model_response":
        if status == "FAILED":
            return f"{who(event)}: {event.get('summary')}"
        return None if mode != "verbose" else f"{who(event)} responded in {event.get('duration', 0):.1f} s"
    if kind == "heartbeat":
        return f"{who(event)} heartbeat: {event.get('summary')}" if mode == "verbose" else None
    if kind in {"test_result", "approval", "fallback", "escalation", "supervisor_selected"}:
        if mode == "quiet" and kind in {"escalation", "supervisor_selected"}:
            return None
        return f"{who(event)} {event.get('summary')}" if kind != "approval" else str(event.get("summary"))
    if kind == "tool":
        return None if mode == "quiet" else f"  {who(event)} used {event.get('summary')}"
    if kind in {"route", "snapshot"}:
        return f"  {event.get('summary')}" if mode == "verbose" else None
    if kind == "task_event":
        if status in {"FAILED", "BLOCKED", "RETRYING", "CANCELLED"}:
            return str(event.get("summary"))[:500]
        return str(event.get("summary"))[:300] if mode == "verbose" else None
    return None


class LiveView:
    """Follows the journal from a background thread while the task runs in this process (or in
    another terminal, for `codingbrain watch`)."""

    def __init__(self, telemetry, mode: str = "live", task_ids=None, after: int | None = None, stream=None,
                 heartbeat: float | None = None):
        self.telemetry, self.mode, self.task_ids = telemetry, mode, task_ids
        self.stream = stream or sys.stdout
        self.after = telemetry.last_seq() if after is None else after
        self.started = time.time()
        self.glyphs = glyphs(self.stream)
        self.interval = heartbeat or accounting.HEARTBEAT_SECONDS
        self.active = {}  # (agent, model, role) -> {"since", "beat", "phase"}
        self.phase = None
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.status_shown = False
        self.last_output = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.seen = 0

    # lifecycle
    def __enter__(self):
        if self.mode != "json":
            self._write("Coding Brain — live activity (Ctrl+C pauses; `codingbrain trace` shows the full history)")
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.thread.join(timeout=5)
        self.poll()

    def pause(self):
        with self.lock:
            self.poll(locked=True)
            self._clear_status()
            self.paused.set()

    def resume(self):
        self.paused.clear()

    # rendering
    def _write(self, line: str):
        self._clear_status()
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (UnicodeEncodeError, ValueError):
            self.stream.write(line.encode("ascii", "replace").decode() + "\n")
        self.last_output = time.time()

    def _clear_status(self):
        if self.status_shown:
            self.stream.write("\r" + " " * 110 + "\r")
            self.status_shown = False

    def emit(self, event: dict):
        self.seen += 1
        key = (event.get("agent"), event.get("model"), (event.get("data") or {}).get("role"))
        if event["event_type"] == "model_request":
            self.active[key] = {"since": event["at"], "beat": event["at"], "phase": event.get("phase")}
        elif event["event_type"] == "heartbeat" and key in self.active:
            self.active[key]["beat"] = event["at"]
        elif event["event_type"] == "model_response":
            self.active.pop(key, None)
        if event["event_type"] == "stage" and event.get("status") == "RUNNING":
            self.phase = event.get("phase")
        if self.mode == "json":
            self.stream.write(json.dumps(event, default=str) + "\n")
            self.stream.flush()
            return
        text = describe(event, self.mode)
        if text is None:
            return
        mark = self.glyphs.get(event.get("status") or "INFO", "·")
        offset = clock(event["at"] - self.started) if event["at"] >= self.started else time.strftime(
            "%H:%M:%S", time.localtime(event["at"]))
        self._write(f"[{offset}] {mark} {text}")

    def status(self) -> str | None:
        """The in-flight request: elapsed time and heartbeat age (or a stall warning)."""
        if not self.active:
            return None
        (agent, model, role), item = max(self.active.items(), key=lambda pair: pair[1]["since"])
        now = time.time()
        quiet_for = now - item["beat"]
        phase = PHASES.get(item.get("phase") or self.phase, item.get("phase") or "")
        line = (f"   … {who({'agent': agent, 'model': model})} working on {phase.lower() or role} — "
                f"{clock(now - item['since'])} elapsed, last heartbeat {int(quiet_for)} s ago")
        if quiet_for > 3 * self.interval + 5:
            line += " — no heartbeat: the request may be stalled (Ctrl+C pauses; `codingbrain resume` continues)"
        return line

    def poll(self, locked=False):
        events = self.telemetry.journal(self.task_ids, after=self.after, limit=500)
        for event in events:
            self.after = event["seq"]
            if self.task_ids is None or event.get("task_id") in self.task_ids:
                self.emit(event)
        return events

    def _run(self):
        while not self.stopped.is_set():
            if not self.paused.is_set():
                with self.lock:
                    try:
                        self.poll(locked=True)
                    except Exception:
                        pass
                    line = self.status()
                    if line and self.mode in {"live", "plain"}:
                        if self.mode == "live" and self.stream.isatty():
                            self._clear_status()
                            self.stream.write("\r" + line[:200])
                            self.stream.flush()
                            self.status_shown = True
                        elif time.time() - self.last_output >= self.interval:
                            self._write(line.strip())
            self.stopped.wait(0.4)


# History views ---------------------------------------------------------------------------------

def trace(telemetry, task: dict, children: list[dict] = (), snapshots: list[dict] = ()) -> dict:
    """The full, ordered history of a task (and its assignments) with per-stage timing."""
    ids = [task["id"], *[child["id"] for child in children]]
    events = telemetry.journal(ids, limit=100000)
    stages, open_stages = [], {}
    for event in events:
        if event["event_type"] != "stage":
            continue
        key = (event["task_id"], event["phase"])
        if event["status"] == "RUNNING":
            open_stages[key] = event
        else:
            start = open_stages.pop(key, None)
            stages.append({"task": event["task_id"][:8], "phase": event["phase"], "agent": event["agent"],
                           "status": event["status"], "started": (start or event)["at"], "duration": event.get("duration"),
                           "summary": event.get("summary")})
    stages += [{"task": key[0][:8], "phase": key[1], "agent": event["agent"], "status": "RUNNING",
                "started": event["at"], "duration": time.time() - event["at"], "summary": event.get("summary")}
               for key, event in open_stages.items()]
    stages.sort(key=lambda item: item["started"])
    usage = accounting.summarize(task.get("inference_log", []))
    requests = [event for event in events if event["event_type"] == "model_response"]
    return {
        "task": {key: task.get(key) for key in ("id", "goal", "status", "kind", "base_commit", "branch")},
        "started": events[0]["at"] if events else None, "ended": events[-1]["at"] if events else None,
        "stages": stages,
        "decisions": [{"at": event["at"], "agent": event["agent"], "phase": event["phase"],
                       "source": (event.get("data") or {}).get("source"), "summary": event["summary"]}
                      for event in events if event["event_type"] == "decision"],
        "tests": [{"at": event["at"], "status": event["status"], "summary": event["summary"]}
                  for event in events if event["event_type"] == "test_result"],
        "models": usage["models"], "fallbacks": usage["fallbacks"], "escalations": usage["escalations"],
        "model_requests": len(requests), "failed_requests": sum(1 for event in requests if event["status"] == "FAILED"),
        "retries": sum(1 for event in events if event.get("status") == "RETRYING"),
        "snapshots": list(snapshots), "events": len(events),
    }


def render_trace(report: dict) -> str:
    task = report["task"]
    lines = [f"Task {task['id'][:8]}  {task['status']}  {task.get('goal', '')[:100]}"]
    if report["started"]:
        lines.append(f"Started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report['started']))}; "
                     f"{clock(report['ended'] - report['started'])} recorded; {report['events']} events")
    lines.append("\nStages:")
    for item in report["stages"]:
        offset = clock(item["started"] - report["started"]) if report["started"] else "?"
        duration = f"{item['duration']:.1f} s" if item.get("duration") is not None else "unknown"
        lines.append(f"  [{offset}] {item['status']:<16} {PHASES.get(item['phase'], item['phase']):<34} "
                     f"{(item['agent'] or ''):<12} {duration}")
    if report["decisions"]:
        lines.append("\nExplanations and decisions:")
        for item in report["decisions"]:
            lines.append(f"  - {item['agent']} ({item['source']}, {item['phase']}): {' '.join(item['summary'].split())[:300]}")
    if report["tests"]:
        lines.append("\nTests:")
        lines += [f"  - {item['status']}: {item['summary']}" for item in report["tests"]]
    lines.append("\nModels:")
    if not report["models"]:
        lines.append("  none recorded")
    for name, item in report["models"].items():
        tokens = f"{item['prompt_tokens'] or 'unknown'} in / {item['output_tokens'] or 'unknown'} out"
        lines.append(f"  - {name} ({item['provider']}): {item['calls']} call(s), {tokens}; roles "
                     + ", ".join(f"{role} {count}" for role, count in item["roles"].items()))
    lines.append(f"  requests {report['model_requests']} ({report['failed_requests']} failed), fallbacks "
                 f"{report['fallbacks']}, premium consultations {report['escalations']}, retries {report['retries']}")
    if report["snapshots"]:
        lines.append("\nSnapshots:")
        lines += [f"  {item['id']}  {item['label']:<18} {item['status'] or ''}" for item in report["snapshots"]]
    return "\n".join(lines)
