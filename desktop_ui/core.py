"""Typed UI adapter for the installed Coding Brain engine.

This file owns no planning, model routing, repositories, or sandbox policy.
It calls the same Brain API used by the CLI, but exposes explicit, non-TTY
approval boundaries. One task runs per Workspace, guarded by its own locks.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
import uuid


class CoreUnavailable(RuntimeError):
    pass


def supported() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("brain.local.cli") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _summary(task: dict, kind: str) -> dict:
    if kind == "new":
        return {"kind": kind, "task_id": task["id"],
                "summary": "Create a NEW local project from this goal and begin work? "
                           "Coding Brain will initialize Git and propose the implementation. "
                           "It will not reuse or overwrite an existing directory.",
                "goal": task.get("goal", "")[:2000]}
    if kind == "proposal":
        proposal = task.get("proposal", {})
        return {"kind": kind, "task_id": task["id"],
                "summary": str(proposal.get("plan", ""))[:4000],
                "files": [change.get("path") for change in proposal.get("changes", [])][:100],
                "diff": str(task.get("diff") or "")[:100000]}
    if kind == "accept":
        evidence = task.get("test_evidence") or {}
        return {"kind": kind, "task_id": task["id"],
                "summary": "Tests passed in the sandbox. Accept the result on a NEW Git branch?",
                "tests_passed": bool(evidence.get("passed")),
                "requirement": (task.get("completion") or {}).get("status", "unchecked"),
                "diff": str(task.get("diff") or "")[:100000]}
    return {"kind": kind, "task_id": task["id"],
            "summary": "The agent requests permission for a protected tool.",
            "request_id": task.get("pending_approval_id")}


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


async def observe(job, brain, task_id: str):
    """Follow the installed engine's actual task events and label silence honestly."""
    seen = set()
    last = time.monotonic()
    while not job.stop_requested.is_set():
        try:
            task = brain.store.get(task_id)
            for event in task.get("events", []):
                key = event.get("id") or (event.get("time"), event.get("kind"), event.get("detail"))
                if key in seen:
                    continue
                seen.add(key)
                kind = str(event.get("kind", "event"))[:70]
                detail = str(event.get("detail") or "")[:3000]
                job.emit("stage", kind + (": " + detail if detail else ""))
                last = time.monotonic()
        except (ValueError, KeyError):
            pass
        if time.monotonic() - last >= 10:
            # A model might be processing without emitting tokens. Do not fake progress.
            job.emit("heartbeat", f"Still waiting on {job.status}; no new engine event for {int(time.monotonic()-last)}s")
            last = time.monotonic()
        await asyncio.sleep(1)


async def execute_goal(job):
    from brain.local.cli import Context, branch_for, slug
    from brain.local.paths import Layout

    context = Context(Layout.default(), Path(job.project))
    context.check()
    brain = context.brain
    task = brain.submit(context.root.name, job.goal, launch=False)
    if getattr(job, "new_project", False):
        task["tests_expected"] = True
        brain.store.save(task)
    job.task_id = task["id"]
    job.emit("stage", f"Task {task['id'][:8]} created in the installed Coding Brain engine")
    watcher = asyncio.create_task(observe(job, brain, task["id"]))
    try:
        job.emit("stage", "Preparing Git workspace, requirements and plan")
        task = await brain.create(task)
        task = brain.store.get(task["id"])
        while not job.stop_requested.is_set():
            status = task["status"]
            if status == "proposed":
                if not task.get("proposal", {}).get("changes"):
                    job.emit("output", str(task.get("proposal", {}).get("plan", "Analysis completed.")))
                    return "completed"
                if not await wait_approval(job, "proposal", task):
                    brain.cancel(task["id"])
                    job.emit("output", "Execution not authorized; your files were not changed.")
                    return "cancelled"
                job.emit("stage", "Approved: reviewing, applying and testing in isolated worktree")
                task = await brain.execute(task["id"], task["digest"])
            elif status == "passed":
                if not await wait_approval(job, "accept", task):
                    job.emit("output", "Result remains in the isolated task workspace; not accepted.")
                    return "completed"
                task = await brain.accept(task["id"], f"Accepted in desktop UI: {job.goal[:200]}")
                if task.get("commit") and task.get("status") == "accepted":
                    branch = branch_for(context, task["commit"], f"{slug(job.goal)}-{task['id'][:6]}")
                    job.emit("output", "Accepted on new branch " + branch + ". Current branch unchanged.")
                return "completed"
            elif status == "awaiting_tool_approval":
                request_id = task.get("pending_approval_id")
                if not request_id:
                    return "failed"
                allow = await wait_approval(job, "tool", task)
                brain.decide_tool_approval(request_id, allow)
                if not allow:
                    job.emit("output", "Protected tool declined.")
                    return "blocked"
                # Resume is scheduled by the engine. Await its existing worker.
                for _ in range(360):
                    await asyncio.sleep(.5)
                    task = brain.store.get(task["id"])
                    if task["status"] not in {"queued", "planning", "running"}:
                        break
            elif status in {"failed", "blocked", "awaiting_implementer", "integration_conflict"}:
                summary = (task.get("failure_log") or [{}])[-1].get("summary") or "No further work can proceed."
                job.emit("error", f"Task {status}: {str(summary)[:2000]}")
                return "failed"
            elif status in {"accepted", "completed"}:
                return "completed"
            elif status in {"cancelled", "cancellation_requested"}:
                return "cancelled"
            else:
                job.emit("stage", f"Engine state: {status}")
                return "failed"
            task = brain.store.get(task["id"])
        try:
            brain.cancel(task["id"])
        except ValueError:
            pass
        return "cancelled"
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


def create_new(job):
    """User-authorized goal-first creation, followed by the same typed task flow."""
    job.status = "running"
    job.emit("start", "Preparing new-project request")
    try:
        from brain.local.create import create_project
        from brain.local.paths import Layout
        tentative = {"id": job.id, "goal": job.goal}
        if not asyncio.run(wait_approval(job, "new", tentative)):
            job.status = "cancelled"
            job.emit("finish", "New-project creation declined; no files created")
            return
        from io import StringIO
        log = StringIO()
        record = create_project(Layout.default(), job.goal, yes=True, interactive=False,
                                out=log)
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
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {str(exc)[:700]}"
        job.emit("error", job.error)
    finally:
        job.awaiting_approval = False
        job.decision_event.set()


def run(job):
    """Thread target, because FastAPI must stay responsive while models work."""
    job.status = "running"
    job.emit("start", "Starting installed Coding Brain service")
    try:
        result = asyncio.run(execute_goal(job))
        job.status = result
        job.emit("finish", f"Coding Brain task {result}")
    except Exception as exc:
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {str(exc)[:700]}"
        job.emit("error", job.error)
    finally:
        job.awaiting_approval = False
        job.decision_event.set()


def chat(job):
    """Use the installed brain's Assistant when available; never route chat into run."""
    job.status = "running"
    try:
        try:
            from brain.local.assistant import Assistant, EXECUTING, GREETING
            from brain.local.paths import Layout
        except ImportError:
            # v0.9.0 has no conversational API. Preserve an actual greeting and
            # report that other chat awaits the installed core's chat release.
            if job.goal.lower().strip(" !.,") in {"hi", "hello", "hey", "bonjour"}:
                job.emit("output", "Hello! I'm Coding Brain. What would you like to work on?")
            else:
                job.emit("output", "Conversation requires the Coding Brain conversational release. "
                         "No engineering task was started. You can still use Run task explicitly.")
            job.status = "completed"
            return
        output = lambda line="": job.emit("output", str(line))
        assistant = Assistant(Layout.default(), Path(job.project or Path.home()),
                              out=output, read=lambda prompt: "no", interactive=False)
        route = assistant.route(job.goal)
        if route.intent in EXECUTING:
            job.emit("output", "I can help with that. Switch to Run task to review and authorize "
                     "coding work. No project was changed.")
        else:
            assistant.handle(job.goal)
        job.status = "completed"
    except Exception as exc:
        job.error = f"{type(exc).__name__}: {str(exc)[:500]}"
        job.status = "failed"
        job.emit("error", job.error)
    finally:
        job.emit("finish", f"Chat {job.status}")
