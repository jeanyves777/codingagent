"""The desktop's bridge to the Coding Brain engine API (brain.local.engine, API 1.0).

This file owns no planning, model routing, repositories, sandbox policy or task state. Every
conversation, project and task goes through the typed engine API (desktop_ui.engine_client), the
same contract as any other program, so the desktop never runs its own copy of the orchestration.
Approval boundaries stay explicit: each decision is single-use, made by the user in the window,
and bound to the task and to the digest of the proposal that was shown. There is no --yes.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path

from .engine_client import EngineError, shared


class CoreUnavailable(RuntimeError):
    pass


def supported() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("brain.local.engine") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def client_for(job):
    return getattr(job, "engine", None) or shared()


def _summary(task: dict, kind: str) -> dict:
    """What the user approves, from the engine's task summary (or the goal, for a new project)."""
    if kind == "new":
        return {"kind": kind, "task_id": task["id"],
                "summary": "Create a NEW local project from this goal and begin work? "
                           "Coding Brain will initialize Git and propose the implementation. "
                           "It will not reuse or overwrite an existing directory.",
                "goal": task.get("goal", "")[:2000]}
    if kind == "proposal":
        return {"kind": kind, "task_id": task["id"], "summary": str(task.get("plan", ""))[:4000],
                "files": list(task.get("files") or [])[:100], "diff": str(task.get("diff") or "")[:100000],
                "digest": task.get("digest")}
    if kind == "accept":
        tests = task.get("tests") or {}
        return {"kind": kind, "task_id": task["id"],
                "summary": "Tests passed in the sandbox. Accept the result on a NEW Git branch?",
                "tests_passed": bool(tests.get("passed")), "requirement": task.get("requirements") or "unchecked",
                "diff": str(task.get("diff") or "")[:100000]}
    return {"kind": kind, "task_id": task["id"], "summary": "The agent requests permission for a protected tool.",
            "request_id": task.get("pending_tool_approval")}


async def wait_approval(job, kind: str, task: dict) -> bool:
    """No implicit approval: every decision is single-use and bound to task/digest."""
    if job.stop_requested.is_set():
        return False
    job.decision_event.clear()
    job.pending_id = uuid.uuid4().hex
    job.pending_kind = kind
    job.pending_payload = _summary(task, kind)
    job.awaiting_approval = True
    job.status = "approval_required"
    job.emit("approval", f"{kind}: {job.pending_payload['summary'][:1000]}")
    # Timed wait makes cancellation responsive without a CLI prompt or --yes.
    while not job.decision_event.is_set():
        if job.stop_requested.is_set():
            job.awaiting_approval = False
            job.pending_payload = None
            return False
        await asyncio.sleep(.15)
    approved = job.decision_allow is True
    job.decision_allow = None
    job.awaiting_approval = False
    job.pending_id = None
    job.pending_payload = None
    job.status = "running"
    job.emit("decision", f"{kind} {'approved' if approved else 'declined'}")
    return approved


class Activity:
    """The engine's live journal events for this job's task, shown as they happen. When nothing
    arrives for a while, it says so instead of inventing progress."""

    def __init__(self, job, quiet: float = 10):
        self.job, self.quiet = job, quiet
        self.last = time.monotonic()
        self.busy = threading.Event()
        self.closed = threading.Event()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def __call__(self, event: dict):
        self.last = time.monotonic()
        if event.get("task_id") and not self.job.task_id:
            self.job.task_id = event["task_id"]
        text = str(event.get("summary") or event.get("event_type") or "event")
        label = " ".join(str(part) for part in (event.get("phase"), event.get("status")) if part)
        self.job.emit("stage", (f"{label}: " if label else "") + text[:3000])

    def _heartbeat(self):
        while not self.closed.wait(1):
            if self.busy.is_set() and time.monotonic() - self.last >= self.quiet:
                self.job.emit("heartbeat", f"Still working ({self.job.status}); no new engine event for "
                                           f"{int(time.monotonic() - self.last)}s")
                self.last = time.monotonic()

    async def call(self, client, op, params, timeout=None):
        """An engine call off the event loop, with its live events streamed into the job."""
        self.busy.set()
        self.last = time.monotonic()
        try:
            return await asyncio.to_thread(client.call, op, params, self, timeout)
        finally:
            self.busy.clear()

    def close(self):
        self.closed.set()


def _stopper(job, client, project_id):
    """Workspace.stop calls this while an engine operation is in flight; the engine answers at
    once and the running operation returns the task as cancelled at a safe boundary."""
    def stop():
        if job.task_id:
            try:
                client.call("tasks.stop", {"project_id": project_id, "task_id": job.task_id}, None, 60)
            except EngineError as error:
                raise ValueError(str(error)) from error
    return stop


async def execute_goal(job):
    """Plan, show the proposal, apply and test only after approval, accept only after approval."""
    client = client_for(job)
    activity = Activity(job)
    try:
        project = await asyncio.to_thread(client.call, "projects.register", {"path": job.project}, None, 120)
        project_id = project["id"]
        job.project_id = project_id
        job.cancel_core = _stopper(job, client, project_id)
        job.emit("stage", "Planning in the Coding Brain engine (nothing is applied before your approval)")
        task = await activity.call(client, "tasks.start", {"project_id": project_id, "goal": job.goal,
                                                           "new_project": bool(getattr(job, "new_project", False))})
        job.task_id = task["id"]
        while True:
            status = task["status"]
            if job.stop_requested.is_set() and status not in {"cancelled", "accepted", "completed"}:
                try:
                    await asyncio.to_thread(client.call, "tasks.stop",
                                            {"project_id": project_id, "task_id": task["id"]}, None, 60)
                except EngineError:
                    pass
                return "cancelled"
            if task.get("interrupted"):
                job.emit("error", "The engine that ran this task stopped; the task was interrupted and needs a retry.")
                return "failed"
            if status == "proposed":
                if not task.get("files"):
                    job.emit("output", str(task.get("plan") or "Analysis completed."))
                    return "completed"
                allowed = await wait_approval(job, "proposal", task)
                if not allowed and job.stop_requested.is_set():
                    continue  # stopped while the proposal was shown: cancelled above
                task = await activity.call(client, "tasks.approve", {
                    "project_id": project_id, "task_id": task["id"], "digest": task["digest"],
                    "decision": "approve" if allowed else "decline"})
                if not allowed:
                    job.emit("output", "Execution not authorized; your files were not changed.")
                    return "cancelled"
            elif status == "passed":
                if not await wait_approval(job, "accept", task):
                    job.emit("output", "Result remains in the isolated task workspace; not accepted.")
                    return "cancelled" if job.stop_requested.is_set() else "completed"
                task = await activity.call(client, "tasks.accept", {"project_id": project_id, "task_id": task["id"]})
                if task.get("branch"):
                    job.emit("output", "Accepted on new branch " + task["branch"] + ". Current branch unchanged.")
                return "completed"
            elif status == "awaiting_tool_approval":
                request_id = task.get("pending_tool_approval")
                if not request_id:
                    return "failed"
                allowed = await wait_approval(job, "tool", task)
                if not allowed and job.stop_requested.is_set():
                    continue
                task = await activity.call(client, "tasks.tool_decision", {
                    "project_id": project_id, "task_id": task["id"], "request_id": request_id,
                    "decision": "approve" if allowed else "decline"})
                if not allowed:
                    job.emit("output", "Protected tool declined.")
                    return "blocked"
            elif status in {"failed", "blocked", "awaiting_implementer", "integration_conflict"}:
                summary = (task.get("failures") or [{}])[-1].get("summary") or "No further work can proceed."
                job.emit("error", f"Task {status}: {str(summary)[:2000]}")
                return "failed"
            elif status in {"accepted", "completed"}:
                return "completed"
            elif status in {"cancelled", "cancellation_requested"}:
                return "cancelled"
            else:
                job.emit("stage", f"Engine state: {status}")
                return "failed"
    finally:
        activity.close()


def _fail(job, exc):
    job.status = "failed"
    job.error = (f"{exc.code}: {str(exc)[:700]}" if isinstance(exc, EngineError)
                 else f"{type(exc).__name__}: {str(exc)[:700]}")
    job.emit("error", job.error)


def create_new(job):
    """User-authorized goal-first creation, followed by the same typed task flow."""
    job.status = "running"
    job.emit("start", "Preparing new-project request")
    try:
        tentative = {"id": job.id, "goal": job.goal}
        if not asyncio.run(wait_approval(job, "new", tentative)):
            job.status = "cancelled"
            job.emit("finish", "New-project creation declined; no files created")
            return
        record = client_for(job).call("projects.create", {"goal": job.goal}, None, 600)
        job.project = record["path"]
        job.new_project = True
        if getattr(job, "on_project_created", None):
            job.on_project_created(record["path"])
        job.emit("output", "New project: " + record["path"])
        job.emit("stage", "Git baseline and safe stack scaffold created")
        outcome = asyncio.run(execute_goal(job))
        job.status = outcome
        job.emit("finish", f"New-project build {outcome}")
    except Exception as exc:
        _fail(job, exc)
    finally:
        job.awaiting_approval = False
        job.decision_event.set()


def run(job):
    """Thread target, because the web server must stay responsive while models work."""
    job.status = "running"
    job.emit("start", "Starting the Coding Brain engine")
    try:
        result = asyncio.run(execute_goal(job))
        job.status = result
        job.emit("finish", f"Coding Brain task {result}")
    except Exception as exc:
        _fail(job, exc)
    finally:
        job.awaiting_approval = False
        job.decision_event.set()


def chat(job):
    """A conversation turn through the engine. It never executes: proposed work is shown with
    how to authorize it (Run task or New project), and nothing is changed."""
    job.status = "running"
    try:
        client = client_for(job)
        params = {"message": job.goal}
        if job.project:  # read-only lookup: a conversation never registers or changes anything
            selected = Path(job.project).resolve()
            for item in client.call("projects.list", {}, None, 60):
                if Path(item["root"]).resolve() == selected:
                    params["project_id"] = item["id"]
        answer = client.call("conversation.send", params, None, 300)
        if answer.get("reply"):
            job.emit("output", answer["reply"])
        action = answer.get("action")
        if action:
            where = "New project" if action.get("kind") == "create_project" else "Run task"
            job.emit("output", f"To do this, use {where} to review and authorize it. Nothing was changed.")
        job.status = "completed"
    except Exception as exc:
        job.error = (f"{exc.code}: {str(exc)[:500]}" if isinstance(exc, EngineError)
                     else f"{type(exc).__name__}: {str(exc)[:500]}")
        job.status = "failed"
        job.emit("error", job.error)
    finally:
        job.emit("finish", f"Chat {job.status}")
