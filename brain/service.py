"""Durable orchestration, isolated execution, review, testing, and learning."""
import asyncio
import difflib
import hashlib
import inspect
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .approvals import ApprovalRequired
from .brains import is_unavailable
from .contracts import Assignment, Delegation, Proposal, validate_graph
from .intelligence import build_index, relevant_context
from .orchestration import OrchestrationMixin
from .repository import MAX_FILE, safe_path, snapshot
from .sandbox import run_tests
from .store import Store
from .publishing import PublishingMixin
from .supervised import SupervisionMixin, guidance_text
from .telemetry import Telemetry
from .workspaces import WorkspaceManager


class Brain(OrchestrationMixin, SupervisionMixin, PublishingMixin):
    ACTIVE = {"queued", "planning", "running", "testing", "cancellation_requested"}

    def __init__(self, repositories: Path, data: Path, model, image, reviewer=None,
                 coordinator=None, memory=None, queue=None, approvals=None, telemetry=None, workers=3,
                 supervision=None, max_free_attempts=3):
        self.repositories, self.data = repositories.resolve(), data.resolve()
        if self.data.is_relative_to(self.repositories) or self.repositories.is_relative_to(self.data):
            raise ValueError("Repository and data directories must be separate")
        self.store = Store(self.data / "brain.sqlite3")
        self.model, self.reviewer = model, reviewer or model
        self.coordinator, self.image = coordinator or model, image
        self.memory = memory
        self.supervision = supervision
        self.max_free_attempts = max(1, min(10, max_free_attempts))
        self.queue = queue
        self.approvals = approvals
        self.telemetry = telemetry or Telemetry(self.data / "telemetry.sqlite3")
        self.manager = WorkspaceManager()
        self.locks, self.jobs = {}, {}
        self.slots = asyncio.Semaphore(workers)
        with self.store.connect() as db:
            rows = db.execute("SELECT body FROM tasks").fetchall()
        for row in rows:
            task = json.loads(row[0])
            interrupted = self.ACTIVE - ({"queued"} if self.queue else set())
            if task["status"] in interrupted:
                task["status"] = "blocked"
                self.event(task, "interrupted", "Server restarted; retry is required")

    def repository(self, name: str) -> Path:
        if not name or Path(name).name != name or name.startswith("."):
            raise ValueError("Repository must be the name of a direct child directory")
        source = self.repositories / name
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Repository unavailable")
        return source

    def task_lock(self, task_id):
        return self.locks.setdefault(task_id, asyncio.Lock())

    def event(self, task: dict, kind: str, detail: str):
        trace_id = task.setdefault("trace_id", uuid.uuid4().hex)
        event_id = self.telemetry.publish(task["id"], trace_id, kind, detail)
        task.setdefault("events", []).append({"id": event_id,
            "time": datetime.now(timezone.utc).isoformat(), "kind": kind, "detail": detail,
            "trace_id": trace_id})
        self.store.save(task)

    def launch(self, task_id, operation):
        if task_id in self.jobs and not self.jobs[task_id].done():
            raise ValueError("Task already has an active worker")

        async def worker():
            async with self.slots:
                try:
                    await operation()
                except asyncio.CancelledError:
                    task = self.store.get(task_id)
                    task["status"] = "cancelled"
                    self.event(task, "cancelled", "Queued worker cancelled")
                    raise
                except Exception as error:
                    task = self.store.get(task_id)
                    if task["status"] != "cancelled":
                        task["status"] = "blocked"
                        self.event(task, "blocked", (str(error) or type(error).__name__)[:1000])
                    if task.get("parent_id"):
                        self.advance_group(task["parent_id"])
                finally:
                    self.jobs.pop(task_id, None)
        self.jobs[task_id] = asyncio.create_task(worker())

    def schedule(self, task_id: str, action: str, payload=None):
        if self.queue:
            return self.queue.enqueue(task_id, action, payload)
        operations = {
            "create_task": lambda: self.create(self.store.get(task_id)),
            "plan_group": lambda: self._plan_group(task_id),
            "execute_task": lambda: self.execute(task_id, (payload or {})["digest"]),
            "resume_task": lambda: self.resume(task_id),
            "supervise_task": lambda: self.supervise(task_id),
            "advance_group": lambda: self._advance_group_async(task_id),
        }
        self.launch(task_id, operations[action])
        return task_id

    async def dispatch_job(self, job: dict):
        task = self.store.get(job["task_id"])
        try:
            with self.telemetry.span(task.get("trace_id", uuid.uuid4().hex),
                                     "queue." + job["action"], task["id"],
                                     {"job_id": job["id"], "attempt": job["attempts"]}):
                if job["action"] == "create_task":
                    await self.create(task)
                elif job["action"] == "plan_group":
                    await self._plan_group(job["task_id"])
                elif job["action"] == "execute_task":
                    await self.execute(job["task_id"], job["payload"]["digest"])
                elif job["action"] == "resume_task":
                    await self.resume(job["task_id"])
                elif job["action"] == "supervise_task":
                    await self.supervise(job["task_id"])
                elif job["action"] == "advance_group":
                    self.advance_group(job["task_id"])
                else:
                    raise ValueError("Unknown durable job action")
        except Exception:
            task = self.store.get(job["task_id"])
            if task.get("status") not in {"cancelled", "failed", "integration_conflict"}:
                task["status"] = "blocked"
                self.event(task, "blocked", "Durable worker operation failed")
            raise

    def submit(self, repository: str, goal: str, parent_id=None, name=None,
               dependencies=None, base_commit=None, launch=True, premium_plan=False) -> dict:
        self.repository(repository)
        task = {
            "id": uuid.uuid4().hex, "kind": "task", "repository": repository, "goal": goal,
            "name": name, "parent_id": parent_id, "depends_on": dependencies or [],
            "base_commit": base_commit, "status": "queued" if launch else "waiting",
            "cancel_requested": False, "events": [], "trace_id": uuid.uuid4().hex,
            "premium_plan": bool(premium_plan),
        }
        with self.telemetry.span(task["trace_id"], "api.submit", task["id"]):
            self.store.save(task)
            if launch:
                self.schedule(task["id"], "create_task")
        return task

    def workspace(self, task_id: str) -> Path:
        task = self.store.get(task_id)
        return self.data / "tasks" / task["id"] / "workspace"

    async def create(self, task: dict) -> dict:
        source = self.repository(task["repository"])
        workspace = self.workspace(task["id"])
        task["status"] = "planning"
        self.store.save(task)
        try:
            metadata = await asyncio.to_thread(
                self.manager.prepare, source, workspace, task.get("base_commit")
            )
            task["workspace_kind"] = metadata["kind"]
            task["base_commit"] = metadata["base_commit"]
            baseline = workspace.parent / "baseline"
            await asyncio.to_thread(snapshot, workspace, baseline)
            index = await asyncio.to_thread(build_index, workspace)
            self.store.save_index(task["repository"], index)
            self._check_cancelled(task)
            guidance = await self.premium_plan(task, workspace, relevant_context(index, task["goal"]))
            await self.plan(task, workspace, task["goal"] + guidance)
            self._check_cancelled(task)
        except Exception:
            task["status"] = "blocked"
            self.event(task, "blocked", "Workspace, index, or model planning failed")
            raise
        return task

    async def plan(self, task: dict, workspace: Path, goal: str):
        memories = self.store.memories(task["repository"], goal)
        if self.memory:
            try:
                memories = await self.memory.search(task["repository"], goal, limit=5)
            except Exception as error:
                self.event(task, "memory_fallback", str(error)[:500])
        index = await asyncio.to_thread(build_index, workspace)
        context = relevant_context(index, goal)
        try:
            with self.telemetry.span(task["trace_id"], "model.propose", task["id"]):
                parameters = inspect.signature(self.model.propose).parameters.values()
                supports_task = ("task_id" in inspect.signature(self.model.propose).parameters or
                                 any(item.kind == inspect.Parameter.VAR_KEYWORD
                                     for item in parameters))
                kwargs = {"task_id": task["id"]} if supports_task else {}
                raw = await self.model.propose(workspace, goal, memories, context, **kwargs)
        except ApprovalRequired as pending:
            task["status"] = "awaiting_tool_approval"
            task["pending_approval_id"] = pending.request["id"]
            task["pending_goal"] = goal
            self.event(task, "tool_approval_required",
                       f"{pending.request['capability']} request {pending.request['id']}")
            return False
        except Exception as error:
            if not is_unavailable(error):
                raise
            # A free model being offline is never a reason to spend premium usage.
            task["status"] = "awaiting_implementer"
            task["pending_goal"] = goal
            self.event(task, "implementer_unavailable",
                       f"No free implementer reachable ({type(error).__name__}); paused. Retry when one "
                       "is running, or publish the work for GitHub CI and PR feedback.")
            return False
        try:
            proposal = Proposal.model_validate_json(raw)
        except ValueError as error:
            raise ValueError("Implementer returned an invalid proposal: " + str(error)[:500]) from error
        return self.store_proposal(task, workspace, proposal, "implementer")

    def store_proposal(self, task: dict, workspace: Path, proposal: Proposal, author: str) -> bool:
        paths = [change.path for change in proposal.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate changed paths")
        cumulative = {item["path"]: item for item in task.get("proposal", {}).get("changes", [])}
        cumulative.update({change.path: change.model_dump() for change in proposal.changes})
        proposal = Proposal(plan=proposal.plan, changes=list(cumulative.values()))
        diff, baseline = [], workspace.parent / "baseline"
        for change in proposal.changes:
            target = safe_path(workspace, change.path)
            if len(change.content.encode()) > MAX_FILE:
                raise ValueError("Replacement file exceeds byte limit")
            original = safe_path(baseline, change.path)
            before = original.read_text(encoding="utf-8") if original.exists() else ""
            diff.extend(difflib.unified_diff(before.splitlines(True), change.content.splitlines(True),
                                             fromfile=change.path, tofile=change.path))
        task["proposal"] = proposal.model_dump()
        task["digest"] = hashlib.sha256(json.dumps(task["proposal"], sort_keys=True).encode()).hexdigest()
        task["diff"] = "".join(diff)
        route = getattr(self.model, "last_route", lambda _task_id: None)(task["id"])
        if route:
            task["model_route"] = route
        task["status"] = "proposed"
        task.pop("pending_approval_id", None)
        task.pop("pending_goal", None)
        task["proposal_author"] = author
        self.event(task, author, "Indexed repository and completed proposal" if author == "implementer"
                   else "Proposal supplied by " + author)
        return True

    async def resume(self, task_id: str):
        task = self.store.get(task_id)
        if task["status"] != "queued" or not task.get("pending_goal"):
            raise ValueError("Task is not ready to resume")
        return await self.plan(task, self.workspace(task_id), task["pending_goal"])

    def decide_tool_approval(self, request_id: str, approved: bool) -> dict:
        if not self.approvals:
            raise ValueError("Tool approvals are not configured")
        request = self.approvals.get(request_id)
        task = self.store.get(request["task_id"])
        if task.get("pending_approval_id") != request_id or task["status"] != "awaiting_tool_approval":
            raise ValueError("Task is not waiting for this tool request")
        with self.telemetry.span(task["trace_id"], "api.tool_approval", task["id"],
                                 {"approved": approved, "request_id": request_id}):
            request = self.approvals.decide(request_id, approved)
            if approved:
                task["status"] = "queued"
                self.event(task, "tool_approval_granted", request["capability"])
                self.schedule(task["id"], "resume_task")
            else:
                task["status"] = "blocked"
                self.event(task, "tool_approval_denied", request["capability"])
        return task

    async def review(self, task):
        verdict = await self.reviewer.review(task["goal"], task["diff"])
        if not isinstance(verdict, dict) or type(verdict.get("approved")) is not bool:
            raise ValueError("Invalid reviewer verdict")
        if not isinstance(verdict.get("reason"), str) or len(verdict["reason"]) > 4000:
            raise ValueError("Invalid review reason")
        task["review"] = verdict
        self.event(task, "reviewer", json.dumps(verdict))
        return verdict

    def apply(self, task: dict):
        for change in task["proposal"]["changes"]:
            target = safe_path(self.workspace(task["id"]), change["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change["content"], encoding="utf-8")

    def _check_cancelled(self, task: dict):
        current = self.store.get(task["id"])
        if current.get("cancel_requested"):
            current["status"] = "cancelled"
            self.event(current, "cancelled", "Stopped at a safe orchestration boundary")
            raise asyncio.CancelledError

    async def execute(self, task_id: str, digest: str) -> dict:
        async with self.task_lock(task_id):
            task = self.store.get(task_id)
            if task["status"] != "proposed" or task["digest"] != digest:
                raise ValueError("Approval requires the current proposed digest")
            if not task["proposal"]["changes"]:
                raise ValueError("Analysis-only proposal has no executable changes")
            task["status"] = "running"
            self.event(task, "approved", "Human authorized review, implementation, and testing")
            await self._execute_loop(task)
            self.store.save(task)
            if task["status"] == "failed":
                getattr(self.model, "record_outcome", lambda *args, **kwargs: None)(
                    task_id, False, task.get("model_route", {}).get("model"))
            if task.get("parent_id") and task["status"] == "failed":
                self.advance_group(task["parent_id"])
            return task

    async def _execute_loop(self, task: dict):
        """Review, apply, and test; repair with the free worker, escalating to a supervisor only
        after repeated failures and within budget."""
        task_id, workspace = task["id"], self.workspace(task["id"])
        failures, limit, attempt = 0, self.max_free_attempts, 0
        escalate_after = self.supervision.escalate_after if self.supervision else limit
        while True:
            attempt += 1
            self._check_cancelled(task)
            review = await self.review(task)
            if review["approved"]:
                self.apply(task)
                self._check_cancelled(task)
                task["status"] = "testing"
                self.event(task, "tester", f"Isolated test attempt {attempt}")
                evidence = await asyncio.to_thread(
                    run_tests, workspace, self.image,
                    should_cancel=lambda: self.store.get(task_id).get("cancel_requested", False))
                if evidence.get("cancelled"):
                    self._check_cancelled(task)
                task["test_evidence"] = evidence
                self.event(task, "test_finished", json.dumps(evidence))
                if evidence["passed"]:
                    verdict = await self.final_review(task)
                    if verdict is None or verdict.get("approved"):
                        task["status"] = "passed"
                        return
                    feedback = "\nSupervisor review (untrusted): " + str(verdict.get("reason", ""))
                elif evidence["exit_code"] in (None, 5, 125, 126, 127):
                    # Missing tests or sandbox problems are not the model's fault; never escalate them.
                    task["status"] = "failed"
                    return
                else:
                    feedback = ("\nRepair related failures only. Evidence (untrusted):\n" +
                                json.dumps(evidence)[:12_000])
            else:
                feedback = "\nReviewer feedback (untrusted): " + review["reason"]
            failures += 1
            outcome = await self._repair(task, workspace, feedback, failures, limit, escalate_after)
            if outcome is None:
                return
            failures, limit = outcome
            task["status"] = "running"

    async def _repair(self, task, workspace, feedback, failures, limit, escalate_after):
        """Produce the next proposal, escalating when warranted. Returns (failures, limit), or None
        when the task stopped (failed, paused, or awaiting approval)."""
        while True:
            guidance = ""
            if failures >= escalate_after and self.supervision:
                consultation = await self.diagnose(task, workspace, {
                    "failures": failures, "last_feedback": feedback[:12_000]})
                if consultation:
                    limit = max(limit, failures + escalate_after)
                    takeover = self.takeover_proposal(task, consultation)
                    if takeover:
                        self.store_proposal(task, workspace, takeover, "supervisor")
                        return failures, limit
                    guidance = guidance_text(consultation)
            if failures >= limit and not guidance:
                task["status"] = "failed"
                self.event(task, "attempts_exhausted", f"{failures} unsuccessful attempts")
                return None
            try:
                planned = await self.plan(task, workspace, task["goal"] + feedback + guidance)
            except ValueError as error:
                self.event(task, "implementer_invalid", str(error)[:1000])
                failures += 1
                continue
            return (failures, limit) if planned else None

    async def accept(self, task_id: str, summary: str, kind="episodic") -> dict:
        async with self.task_lock(task_id):
            task = self.store.get(task_id)
            if task["status"] != "passed":
                raise ValueError("Only test-passed tasks can be accepted")
            if task.get("workspace_kind") == "git":
                task["commit"] = await asyncio.to_thread(
                    self.manager.commit, self.workspace(task_id), task_id
                )
            parent_id = task.get("parent_id")
            if parent_id:
                async with self.task_lock(parent_id):
                    group = self.store.get(parent_id)
                    try:
                        group["integration_head"] = await asyncio.to_thread(
                            self.manager.integrate, Path(group["integration_workspace"]), task["commit"]
                        )
                    except ValueError as error:
                        task["status"] = "integration_conflict"
                        group["status"] = "integration_conflict"
                        self.event(task, "integration_conflict", str(error)[:1000])
                        self.event(group, "integration_conflict", f"{task['name']}: {error}"[:1000])
                        return task
                    self.event(group, "integrated", f"{task['name']} at {task['commit']}")
            self.store.remember(task_id, {
                "repository": task["repository"], "content": summary, "evidence": task["test_evidence"],
                "commit": task.get("commit"), "kind": kind,
                "verification": "tests_passed_and_human_accepted",
                "supervision": [{"supervisor": item["supervisor"], "kind": item["kind"]}
                                for item in task.get("supervision", [])],
            })
            task["status"] = "accepted"
            getattr(self.model, "record_outcome", lambda *args, **kwargs: None)(
                task_id, True, task.get("model_route", {}).get("model"))
            self.event(task, "accepted", summary)
            if self.memory:
                try:
                    await self.memory.remember(task_id, task["repository"], kind, summary, {
                        "goal": task["goal"], "commit": task.get("commit"),
                        "test_evidence": task["test_evidence"],
                    }, verified=True)
                    self.event(task, "semantic_memory", "Verified outcome embedded")
                except Exception as error:
                    self.event(task, "memory_deferred", str(error)[:500])
            if parent_id:
                self.advance_group(parent_id)
            return task

    async def sync_memory(self, task_id: str) -> dict:
        if not self.memory:
            raise ValueError("Semantic memory is not configured")
        task = self.store.get(task_id)
        if task.get("status") != "accepted" or not task.get("test_evidence", {}).get("passed"):
            raise ValueError("Only accepted, test-passed tasks can be synchronized")
        record = self.store.memory(task_id)
        await self.memory.remember(task_id, task["repository"], record.get("kind", "episodic"),
                                   record["content"], {"goal": task["goal"],
                                   "commit": task.get("commit"), "test_evidence": task["test_evidence"]},
                                   verified=True)
        self.event(task, "semantic_memory", "Verified outcome synchronized")
        return task

    async def semantic_memories(self, repository: str, query: str, limit=5, kinds=None):
        self.repository(repository)
        if not self.memory:
            raise ValueError("Semantic memory is not configured")
        return await self.memory.search(repository, query, limit, kinds)

    def cancel(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task["status"] in {"accepted", "completed", "cancelled"}:
            raise ValueError("Task is already terminal")
        task["cancel_requested"] = True
        if task["status"] in {"waiting", "queued", "proposed", "blocked", "failed",
                              "awaiting_tool_approval"}:
            job = self.jobs.get(task_id)
            if job and task["status"] == "queued":
                job.cancel()
            task["status"] = "cancelled"
            if self.queue:
                self.queue.cancel_task(task_id)
        else:
            task["status"] = "cancellation_requested"
        self.event(task, "cancellation_requested", "Cancellation will occur at a safe boundary")
        if task.get("kind") == "orchestration":
            for child in task.get("children", []):
                child_task = self.store.get(child["id"])
                if child_task["status"] not in {"accepted", "cancelled"}:
                    self.cancel(child["id"])
        elif task.get("parent_id"):
            self.advance_group(task["parent_id"])
        return task

    def retry(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task.get("kind") != "task" or task["status"] not in {
            "blocked", "failed", "cancelled", "integration_conflict", "awaiting_implementer"
        }:
            raise ValueError("Only interrupted or failed coding tasks can be retried")
        source, workspace = self.repository(task["repository"]), self.workspace(task_id)
        if workspace.exists():
            self.manager.remove(source, workspace)
        baseline = workspace.parent / "baseline"
        if baseline.exists():
            shutil.rmtree(baseline)
        for key in ("proposal", "digest", "diff", "review", "test_evidence", "commit",
                    "pending_approval_id", "pending_goal", "workspace_removed", "retained_ref"):
            task.pop(key, None)
        if task.get("parent_id"):
            task["base_commit"] = self.store.get(task["parent_id"])["integration_head"]
        task["cancel_requested"] = False
        task["status"] = "queued"
        self.event(task, "retry", "Starting a fresh workspace from the current base")
        self.schedule(task_id, "create_task")
        return task

    RETAINABLE = {"accepted", "completed", "failed", "blocked", "cancelled", "integration_conflict"}

    def cleanup(self, task_id: str) -> dict:
        """Remove a finished task's or orchestration's worktrees, pinning any commit first."""
        task = self.store.get(task_id)
        if task["status"] not in self.RETAINABLE:
            raise ValueError("Only finished work can be cleaned up")
        job = self.jobs.get(task_id)
        if job and not job.done():
            raise ValueError("Task still has an active worker")
        source = self.repository(task["repository"])
        if task.get("kind") == "orchestration":
            if any(self.store.get(child["id"])["status"] not in self.RETAINABLE
                   for child in task.get("children", [])):
                raise ValueError("Orchestration still has unfinished assignments")
            for child in task.get("children", []):
                if not self.store.get(child["id"]).get("workspace_removed"):
                    self.cleanup(child["id"])
            directory = self.data / "orchestrations" / task_id
            workspace = Path(task["integration_workspace"]) if task.get("integration_workspace") else None
            head = task.get("integration_head")
            if head and head != task.get("base_commit"):
                self.manager.keep(source, f"refs/coding-brain/orchestrations/{task_id}", head)
                task["retained_ref"] = f"refs/coding-brain/orchestrations/{task_id}"
        else:
            directory = self.data / "tasks" / task_id
            workspace = directory / "workspace"
            if task.get("commit"):
                self.manager.keep(source, f"refs/coding-brain/tasks/{task_id}", task["commit"])
                task["retained_ref"] = f"refs/coding-brain/tasks/{task_id}"
        if workspace and workspace.exists():
            self.manager.remove(source, workspace)
        if directory.exists():
            shutil.rmtree(directory)
        if self.manager.is_git(source):
            self.manager._git(source, "worktree", "prune")
        task["workspace_removed"] = True
        self.event(task, "workspace_removed", task.get("retained_ref") or "No commit to retain")
        return task

    def prune(self, older_than_days: float = 7) -> list[str]:
        """Clean up every finished item whose last event is older than the retention window."""
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        removed = []
        for task in self.store.tasks():
            if task.get("workspace_removed") or task["status"] not in self.RETAINABLE:
                continue
            if task.get("parent_id"):
                continue
            events = task.get("events") or [{"time": "1970-01-01T00:00:00+00:00"}]
            if datetime.fromisoformat(events[-1]["time"]).timestamp() > cutoff:
                continue
            try:
                self.cleanup(task["id"])
                removed.append(task["id"])
            except ValueError:
                continue
        return removed

    def repository_context(self, repository: str, query: str = "") -> dict:
        self.repository(repository)
        return relevant_context(self.store.index(repository), query)

    def reconcile_queue(self) -> list[str]:
        if not self.queue:
            return []
        reconciled = []
        for job in self.queue.jobs():
            if job["status"] != "failed":
                continue
            task = self.store.get(job["task_id"])
            if task.get("status") in self.ACTIVE:
                task["status"] = "blocked"
                self.event(task, "queue_failed", job.get("last_error") or "Durable job failed")
                reconciled.append(task["id"])
        return reconciled

    def dispatch_events(self, limit=100) -> int:
        events = self.telemetry.pending("orchestration", limit)
        for item in events:
            try:
                task = self.store.get(item["task_id"])
                if (task.get("parent_id") and item["kind"] in
                        {"accepted", "blocked", "cancelled", "integration_conflict"}):
                    self.schedule(task["parent_id"], "advance_group")
            except KeyError:
                pass
            self.telemetry.acknowledge("orchestration", item["id"])
        return len(events)
