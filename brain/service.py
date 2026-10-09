"""Durable orchestration, isolated execution, review, testing, and learning."""
import asyncio
import difflib
import hashlib
import inspect
import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from . import accounting
from .approvals import ApprovalRequired
from .capabilities import TASK_CAPABILITIES, TASK_QUERY
from .brains import is_unavailable
from .completion import (accept_requirement_tests, assess_failures, requirement_goal, run_requirement_checks,
                         without_tests)
from .contracts import Assignment, Delegation, Proposal, validate_graph
from .intelligence import build_index, relevant_context
from .activity import ActivityMixin
from .orchestration import OrchestrationMixin
from .repository import MAX_FILE, safe_path, snapshot
from .sandbox import run_tests
from .store import Store
from .publishing import PublishingMixin
from .supervised import SupervisionMixin, guidance_text
from .intelligence import unresolved_imports
from .validators import (Diagnostic, ProposalInvalid, classify_test_failure, mechanical_repair,
                         static_issues, validate_change)
from .telemetry import Telemetry
from .workspaces import WorkspaceManager


def is_junction(path: Path) -> bool:
    """Windows directory junctions are links too, though not symlinks to Path.is_symlink()."""
    check = getattr(path, "is_junction", None)
    if check:
        return check()
    try:
        import os
        import stat
        return bool(getattr(os.lstat(path), "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1))
    except OSError:
        return False


def is_within(path: Path, root: Path) -> bool:
    """Containment on resolved paths, case-insensitively where the platform is (Windows)."""
    import os
    path, root = os.path.normcase(str(Path(path).resolve())), os.path.normcase(str(Path(root).resolve()))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def overlaps(a: Path, b: Path) -> bool:
    return is_within(a, b) or is_within(b, a)


def test_tail(output: str) -> str:
    """The test runner's own summary line ('3 passed, 1 failed in 0.4s'), or the sandbox's error."""
    lines = [line.strip(" =") for line in (output or "").strip().splitlines() if line.strip(" =")]
    errors = [line for line in lines if "Unable to find image" in line or line.lower().startswith(("docker:", "error"))]
    return (errors or lines or ["no output"])[0 if errors else -1][:200]


class Brain(ActivityMixin, OrchestrationMixin, SupervisionMixin, PublishingMixin):
    ACTIVE = {"queued", "planning", "running", "testing", "cancellation_requested"}

    def __init__(self, repositories: Path, data: Path, model, image, reviewer=None,
                 coordinator=None, memory=None, queue=None, approvals=None, telemetry=None, workers=3,
                 supervision=None, max_free_attempts=3, validation_retries=2, knowledge=None, web=None,
                 gates=True, review_mode="advisory", requirement_checks=False):
        self.repositories, self.data = repositories.resolve(), data.resolve()
        # The data directory may sit below the repositories root (a project directly in the user's
        # home folder, with data in AppData), but a served repository never overlaps it: that is
        # checked per repository on resolved paths (repository()). The roots themselves must differ,
        # and repositories are never inside the data directory.
        if is_within(self.repositories, self.data):
            raise ValueError("Repository and data directories must be separate")
        self.store = Store(self.data / "brain.sqlite3")
        self.model, self.reviewer = model, reviewer or model
        self.coordinator, self.image = coordinator or model, image
        self.memory = memory
        self.supervision = supervision
        self.max_free_attempts = max(1, min(10, max_free_attempts))
        self.validation_retries = max(0, min(5, validation_retries))
        self.knowledge = knowledge
        self.web = web
        self.gates = gates  # deterministic validation; disabled only for baseline measurement
        if review_mode not in {"advisory", "gate"}:
            raise ValueError("review_mode must be advisory or gate")
        # advisory: a free reviewer's objection is recorded, but authoritative offline tests still run.
        self.review_mode = review_mode
        # Completion verification: goal-derived checks written before implementation (never committed).
        # The factory enables it (BRAIN_REQUIREMENT_CHECKS, default true).
        self.requirement_checks = requirement_checks
        # Optional callable(goal) -> dict of durable project memory (set by the local CLI).
        self.project_knowledge = None
        # Optional callable(task) -> VisualVerifier | None for tasks with visual requirements (local CLI).
        self.visual_verifier = None
        self.queue = queue
        self.approvals = approvals
        self.telemetry = telemetry or Telemetry(self.data / "telemetry.sqlite3")
        from .snapshots import SnapshotStore
        self.snapshots = SnapshotStore(self.data / "snapshots")
        self.manager = WorkspaceManager()
        self.locks, self.jobs = {}, {}
        self.slots = asyncio.Semaphore(workers)
        import os
        import socket
        # Which process runs a task: another live process on this computer (a second terminal)
        # keeps its tasks; only tasks whose process is gone are marked interrupted.
        self.owner = {"pid": os.getpid(), "host": socket.gethostname()}
        with self.store.connect() as db:
            rows = db.execute("SELECT body FROM tasks").fetchall()
        for row in rows:
            task = json.loads(row[0])
            interrupted = self.ACTIVE - ({"queued"} if self.queue else set())
            if task["status"] in interrupted and not self.owned_elsewhere(task):
                task["status"] = "blocked"
                self.event(task, "interrupted", "Server restarted; retry is required")

    def owned_elsewhere(self, task: dict) -> bool:
        owner = task.get("owner") or {}
        if owner.get("host") != self.owner["host"] or owner.get("pid") in (None, self.owner["pid"]):
            return False
        from .local.session import pid_alive
        return pid_alive(int(owner["pid"]))

    def repository(self, name: str) -> Path:
        if not name or Path(name).name != name or name.startswith("."):
            raise ValueError("Repository must be the name of a direct child directory")
        source = self.repositories / name
        if source.is_symlink() or is_junction(source) or not source.is_dir():
            raise ValueError("Repository unavailable")
        resolved = source.resolve(strict=True)  # follows links, Windows short names and drive aliases
        if not is_within(resolved, self.repositories) or resolved == self.repositories:
            raise ValueError("Repository unavailable: it resolves outside the repositories directory")
        if overlaps(resolved, self.data):
            raise ValueError("Repository unavailable: it overlaps Coding Brain's data directory")
        return source

    def task_lock(self, task_id):
        return self.locks.setdefault(task_id, asyncio.Lock())

    def event(self, task: dict, kind: str, detail: str):
        trace_id = task.setdefault("trace_id", uuid.uuid4().hex)
        task["owner"] = self.owner
        event_id = self.telemetry.publish(task["id"], trace_id, kind, detail)
        task.setdefault("events", []).append({"id": event_id,
            "time": datetime.now(timezone.utc).isoformat(), "kind": kind, "detail": detail,
            "trace_id": trace_id})
        self.store.save(task)
        self.journal_task_event(task, kind, detail)

    def snapshot(self, task: dict, label: str, artifacts=None) -> str | None:
        """Best effort: a snapshot never fails or delays the task's outcome."""
        try:
            workspace = self.workspace(task["id"]) if task.get("kind") == "task" else None
            snapshot_id = self.snapshots.capture(task, workspace, label, artifacts)
        except Exception as error:
            self.journal_event(task, "snapshot", status="FAILED", agent="coding_brain",
                               summary=f"snapshot {label} failed: {type(error).__name__}: {str(error)[:200]}")
            return None
        self.journal_event(task, "snapshot", status="COMPLETED", agent="coding_brain",
                           summary=f"snapshot {label}", artifacts=[{"snapshot": snapshot_id, "label": label}])
        return snapshot_id

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
               dependencies=None, base_commit=None, launch=True, premium_plan=False, attachments=None,
               visual=None) -> dict:
        self.repository(repository)
        task = {
            "id": uuid.uuid4().hex, "kind": "task", "repository": repository, "goal": goal,
            "name": name, "parent_id": parent_id, "depends_on": dependencies or [],
            "base_commit": base_commit, "status": "queued" if launch else "waiting",
            "cancel_requested": False, "events": [], "trace_id": uuid.uuid4().hex,
            "premium_plan": bool(premium_plan),
        }
        if attachments:
            task["attachments"] = attachments  # evidence with provenance (brain.attachments.evidence)
        if visual:
            task["visual"] = visual  # reference images and settings for visual verification
        self.journal_event(task, "stage", phase="goal", status="COMPLETED", agent="user", summary=goal[:500],
                           dedupe=f"{task['id']}:goal", data={"attachments": len((attachments or {}).get("attachments", []))})
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
            with self.stage(task, "project_validation", summary="Validating Git and preparing an isolated worktree"):
                metadata = await asyncio.to_thread(
                    self.manager.prepare, source, workspace, task.get("base_commit")
                )
                task["workspace_kind"] = metadata["kind"]
                task["base_commit"] = metadata["base_commit"]
                baseline = workspace.parent / "baseline"
                await asyncio.to_thread(snapshot, workspace, baseline)
            with self.stage(task, "indexing", summary="Indexing the repository"):
                index = await asyncio.to_thread(build_index, workspace)
                self.store.save_index(task["repository"], index)
            self.snapshot(task, "initial")
            self._check_cancelled(task)
            guidance = await self.premium_plan(task, workspace, self.with_attachments(
                task, relevant_context(index, task["goal"])))
            await self.write_requirement_checks(task, workspace, self.with_attachments(
                task, relevant_context(index, task["goal"])))
            self._check_cancelled(task)
            with self.stage(task, "proposal", agent="implementer", summary="Generating the proposed changes"):
                await self._initial_proposal(task, workspace, task["goal"] + guidance)
            self._check_cancelled(task)
        except Exception:
            task["status"] = "blocked"
            self.event(task, "blocked", "Workspace, index, or model planning failed")
            raise
        return task

    async def write_requirement_checks(self, task: dict, workspace: Path, context: dict):
        """Before implementing, have the free model write checks for what the goal states. It sees the
        goal and the visible repository only. Failing to produce usable checks never blocks the task."""
        if not self.requirement_checks or task.get("requirement_tests") is not None:
            return
        with self.stage(task, "requirements", agent="implementer", summary="Writing checks from the goal's requirements"):
            await self._write_requirement_checks(task, workspace, context)

    async def _write_requirement_checks(self, task: dict, workspace: Path, context: dict):
        started, before = time.monotonic(), self.usage_counters()
        try:
            with self.telemetry.span(task["trace_id"], "model.requirements", task["id"]), \
                    accounting.collect(task):
                raw = await self.model.propose(workspace, requirement_goal(task), [], context)
        except Exception as error:
            self.record_usage(task, started, before)
            self.event(task, "requirement_checks_skipped", f"{type(error).__name__}: {str(error)[:300]}")
            return
        after = self.usage_counters()
        self.record_usage(task, started, before)
        self._count(task, "requirement_generation_seconds", round(time.monotonic() - started, 1))
        self._count(task, "requirement_generation_prompt_tokens", after["prompt_tokens"] - before["prompt_tokens"])
        self._count(task, "requirement_generation_output_tokens", after["output_tokens"] - before["output_tokens"])
        checks = accept_requirement_tests(task, raw)
        if checks and (workspace / checks["path"]).exists():
            checks = None  # never shadow an existing file
        if not checks:
            self._audit(task, "unusable", content=str(raw)[:20_000])
            task["requirement_tests"] = {}
            self.event(task, "requirement_checks_skipped", "The model did not return one usable test file")
            return
        task["requirement_tests"] = checks
        self._audit(task, "written", content=checks["content"], tests=checks["tests"])
        self.event(task, "requirement_checks_written", f"{checks['tests']} check(s) in {checks['path']}")

    def _audit(self, task: dict, stage: str, content: str, **fields):
        """Preserve generated requirement checks as they were, with what happened to them: in the
        task record (copied into Gauntlet results) and as a read-only file outside the workspace.
        Audit records never influence execution."""
        entries = task.setdefault("requirement_audit", [])
        entry = {"seq": len(entries) + 1, "stage": stage, "time": datetime.now(timezone.utc).isoformat(),
                 "sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content, **fields}
        body = json.dumps(entry, sort_keys=True)
        entry["record_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        entries.append(entry)
        try:
            folder = self.data / "tasks" / task["id"] / "audit"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"requirement-{entry['seq']:03d}-{stage}.json"
            target.write_text(body + "\n", encoding="utf-8")
            target.chmod(0o444)
        except OSError as error:
            self.event(task, "audit_write_failed", f"{type(error).__name__}: {str(error)[:300]}")

    @staticmethod
    def _count(task: dict, key: str, amount=1):
        metrics = task.setdefault("metrics", {})
        metrics[key] = round(metrics.get(key, 0) + amount, 1)

    def _completion(self, task: dict, status: str, detail: str):
        """verified, unverified (valid checks still fail), inconclusive (no usable checks left), or
        unchecked (none were written). Only verified counts as verified completion."""
        task["completion"] = {"status": status, "detail": detail[:500]}
        task["completion_verified"] = {"verified": True, "unchecked": None}.get(status, False)

    async def _verify_completion(self, task: dict, workspace: Path) -> dict | None:
        """Run the requirement checks once the visible tests pass. Returns a failure for the repair
        loop; None when the work is verified, or when the checks are inconclusive; or
        {"stop": True} when only failures that were already repaired against remain.

        A failing check drives a repair only after validation: checks that fail on their own defect
        or on an interface the goal does not state are removed without using the repair budget."""
        checks = task.get("requirement_tests")
        if not checks:
            if self.requirement_checks and "completion" not in task:
                self._completion(task, "unchecked", "No requirement checks were written")
            return None
        for _ in range(3):
            evidence = await asyncio.to_thread(
                run_requirement_checks, workspace, checks, run_tests, self.image,
                lambda: self.store.get(task["id"]).get("cancel_requested", False))
            self._count(task, "requirement_check_runs")
            if evidence.get("cancelled"):
                self._check_cancelled(task)
            if evidence["passed"]:
                self._completion(task, "verified", f"{checks['tests']} requirement check(s) passed")
                self.event(task, "completion_verified", task["completion"]["detail"])
                return None
            failure = classify_test_failure(evidence)
            if failure["category"] != "test_failure":
                # Broken generated checks (collection errors) or sandbox problems: never block.
                self._audit(task, "discarded", content=checks["content"], category=failure["category"],
                            output=failure["summary"][:2000])
                task["requirement_tests"] = {}
                self._completion(task, "inconclusive", f"checks discarded ({failure['category']})")
                self.event(task, "requirement_checks_discarded", f"{failure['category']}: {failure['summary'][:500]}")
                return None
            assessment = assess_failures(evidence["output"], checks, task["goal"])
            if not assessment["invalid"]:
                break
            kept = without_tests(checks, set(assessment["invalid"]))
            self._audit(task, "rejected", content=checks["content"], reasons=assessment["invalid"],
                        remaining_sha256=hashlib.sha256(kept["content"].encode()).hexdigest(),
                        remaining_tests=kept["tests"])
            checks = kept
            task["requirement_tests"] = checks if checks["tests"] else {}
            self._count(task, "requirement_tests_rejected", len(assessment["invalid"]))
            self.event(task, "requirement_checks_rejected", json.dumps(assessment["invalid"])[:1500])
            if not checks["tests"]:
                self._completion(task, "inconclusive", "every requirement check was invalid")
                return None
        else:
            self._completion(task, "inconclusive", "requirement checks could not be validated")
            return None
        failures = assessment["failures"] or {"unparsed": failure["summary"][:300]}
        signatures = [f"{name}: {detail}" for name, detail in failures.items()]
        seen = task.setdefault("requirement_repairs", [])
        self.event(task, "requirement_checks_failed", (assessment["summary"] or failure["summary"])[:1000])
        if set(signatures) <= set(seen):
            return {"stop": True, "failing": len(signatures)}
        seen.extend(item for item in signatures if item not in seen)
        self._count(task, "requirement_repairs")
        return {**failure, "category": "requirement", "failing": len(signatures),
                "summary": assessment["summary"] or failure["summary"]}

    def _keep_visible_pass(self, task: dict, workspace: Path, failing: int):
        """Remember the version that passed the visible tests with the fewest failing requirement
        checks, so a requirement repair can never leave the task worse than it was."""
        saved = task.get("visible_pass")
        if saved and saved.get("failing", 0) <= failing:
            return
        files = {}
        for path in task.get("applied_paths", []):
            target = safe_path(workspace, path)
            files[path] = target.read_text(encoding="utf-8") if target.is_file() else None
        task["visible_pass"] = {"files": files, "proposal": task["proposal"], "digest": task.get("digest"),
                                "test_evidence": task["test_evidence"], "failing": failing}

    def _restore_visible_pass(self, task: dict, reason: str) -> bool:
        saved = task.pop("visible_pass", None)
        if not saved:
            return False
        workspace = self.workspace(task["id"])
        for path in task.get("applied_paths", []):
            target = safe_path(workspace, path)
            content = saved["files"].get(path)
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(content, encoding="utf-8")
        task["applied_paths"] = [path for path, content in saved["files"].items() if content is not None]
        task["proposal"], task["digest"] = saved["proposal"], saved["digest"]
        task["test_evidence"] = saved["test_evidence"]
        task["status"] = "passed"
        self._completion(task, "unverified", reason)
        self.event(task, "completion_unverified", reason + "; kept the version that passed the visible "
                   "tests with the fewest failing requirement checks, reported as unverified")
        return True

    async def _initial_proposal(self, task: dict, workspace: Path, goal: str):
        """The first proposal. If it fails repeatedly (every focused correction was rejected, or
        the output was not a valid proposal), that counts as repeated failure: the supervision
        policy may diagnose it within the same budget as execution failures."""
        try:
            return await self.plan(task, workspace, goal)
        except ValueError as error:
            proposals = self.validation_retries + 1
            task.setdefault("failure_log", []).append({
                "attempt": 0, "category": "proposal", "summary": str(error)[:1000], "proposals": proposals})
            self.event(task, "proposal_failed", f"{proposals} rejected proposal(s): {str(error)[:500]}")
            if not self.supervision or proposals < self.supervision.escalate_after:
                raise
            consultation = await self.diagnose(task, workspace, {
                "stage": "initial_proposal", "failures": proposals,
                "last_feedback": str(error)[:6000], "failure_log": task["failure_log"][-2:]})
            if not consultation:
                raise
            takeover = self.takeover_proposal(task, consultation)
            if takeover:
                return self.store_proposal(task, workspace, takeover, "supervisor")
            return await self.plan(task, workspace, goal + guidance_text(consultation))

    async def plan(self, task: dict, workspace: Path, goal: str):
        """Ask the free implementer for a proposal; give it bounded, focused chances to correct
        deterministic validation failures before anything else sees the proposal."""
        attempt_goal = goal
        for correction in range(self.validation_retries + 1):
            try:
                return await self._plan_once(task, workspace, attempt_goal)
            except ProposalInvalid as invalid:
                metrics = task.setdefault("metrics", {})
                metrics["validation_failures"] = metrics.get("validation_failures", 0) + 1
                self.event(task, "validation_failed",
                           json.dumps([item.as_dict() for item in invalid.diagnostics])[:2000])
                if correction == self.validation_retries:
                    raise
                attempt_goal = goal + "\n\nYour previous proposal was rejected before review.\n" + \
                    str(invalid) + invalid.excerpt
        raise AssertionError("unreachable")

    async def _plan_once(self, task: dict, workspace: Path, goal: str):
        with self.stage(task, "memory", summary="Retrieving memory, knowledge and repository context"):
            memories, index, context, tools = await self._gather_context(task, workspace, goal)
        return await self._propose(task, workspace, goal, memories, index, context, tools)

    async def _gather_context(self, task: dict, workspace: Path, goal: str):
        memories = self.store.memories(task["repository"], goal)
        if self.memory:
            try:
                memories = await self.memory.search(task["repository"], goal, limit=5)
            except Exception as error:
                self.event(task, "memory_fallback", str(error)[:500])
        index = await asyncio.to_thread(build_index, workspace)
        context = relevant_context(index, goal)
        tools = ()
        external = await self._web_preflight(task, workspace)
        if self.knowledge:
            context, tools = self._engineering_packet(task, workspace, goal, index, memories, context)
            memories = []  # verified fixes travel inside the packet
            if external:
                context["engineering_packet"]["verified_external"] = external
        elif external:
            context = {**context, "verified_external": external}
        if self.project_knowledge:
            # Durable cross-agent project memory: reference data with provenance, never instructions.
            try:
                knowledge = self.project_knowledge(goal)
            except Exception as error:
                knowledge = None
                self.event(task, "project_memory_unavailable", f"{type(error).__name__}: {str(error)[:300]}")
            if knowledge:
                if isinstance(context.get("engineering_packet"), dict):
                    context["engineering_packet"]["project_memory"] = knowledge
                else:
                    context = {**context, "project_memory": knowledge}
        context = self.with_attachments(task, context)
        if self.web:
            tools = tuple(tools) + self.web.task_capabilities(workspace)
        return memories, index, context, tools

    async def _propose(self, task, workspace, goal, memories, index, context, tools):
        started, before = time.monotonic(), self.usage_counters()
        token, query_token = TASK_CAPABILITIES.set(tools), TASK_QUERY.set(goal)
        try:
            with self.telemetry.span(task["trace_id"], "model.propose", task["id"]), accounting.collect(task):
                parameters = inspect.signature(self.model.propose).parameters.values()
                supports_task = ("task_id" in inspect.signature(self.model.propose).parameters or
                                 any(item.kind == inspect.Parameter.VAR_KEYWORD
                                     for item in parameters))
                kwargs = {"task_id": task["id"]} if supports_task else {}
                raw = await self.model.propose(workspace, goal, memories, context, **kwargs)
        except ApprovalRequired as pending:
            self.record_usage(task, started, before)
            task["status"] = "awaiting_tool_approval"
            task["pending_approval_id"] = pending.request["id"]
            task["pending_goal"] = goal
            self.event(task, "tool_approval_required",
                       f"{pending.request['capability']} request {pending.request['id']}")
            return False
        except Exception as error:
            self.record_usage(task, started, before)
            if not is_unavailable(error):
                raise
            # A free model being offline is never a reason to spend premium usage.
            task["status"] = "awaiting_implementer"
            task["pending_goal"] = goal
            self.event(task, "implementer_unavailable",
                       f"No free implementer reachable ({type(error).__name__}); paused. Retry when one "
                       "is running, or publish the work for GitHub CI and PR feedback.")
            return False
        finally:
            TASK_CAPABILITIES.reset(token)
            TASK_QUERY.reset(query_token)
        self.record_usage(task, started, before)
        try:
            proposal = Proposal.model_validate_json(raw)
        except ValueError as error:
            raise ValueError("Implementer returned an invalid proposal: " + str(error)[:500]) from error
        return self.store_proposal(task, workspace, proposal, "implementer")

    def store_proposal(self, task: dict, workspace: Path, proposal: Proposal, author: str) -> bool:
        paths = [change.path for change in proposal.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate changed paths")
        proposal, repaired = self.validate_proposal(workspace, proposal) if self.gates else (proposal, [])
        if repaired:
            metrics = task.setdefault("metrics", {})
            metrics["mechanical_repairs"] = metrics.get("mechanical_repairs", 0) + len(repaired)
            self.event(task, "mechanical_repair", "Converted literal \\n sequences to line breaks in " +
                       ", ".join(repaired))
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
        route = task.get("model_route") or {}
        model_name = route.get("model") or (getattr(self.model, "name", None) if author == "implementer" else None)
        self.journal_event(task, "decision", phase="proposal", status="COMPLETED", agent=author, model=model_name,
                           summary=proposal.plan[:2000],
                           data={"source": "model-provided", "files": [change.path for change in proposal.changes]})
        self.snapshot(task, "after_proposal")
        self.journal_event(task, "approval", phase="approval", status="WAITING_APPROVAL", agent="user",
                           summary="Waiting for approval to review, apply and test: " +
                                   ", ".join(change.path for change in proposal.changes)[:500],
                           dedupe=f"{task['id']}:approval:{task['digest']}")
        return True

    def _engineering_packet(self, task, workspace, goal, index, memories, context):
        """Phase 2 knowledge routing: compress what the free model needs into one packet."""
        from .knowledge import task_capabilities
        from .routing import complexity
        from .sandbox import profile
        try:
            command = profile(workspace)["command"]
        except (ValueError, OSError):
            command = None
        packet = self.knowledge.packet(
            goal, index, memories, command, task.get("failure_log"),
            read_file=lambda path: safe_path(workspace, path).read_text(encoding="utf-8"))
        size = len(json.dumps(packet))
        task["knowledge"] = {"files": packet["code"]["files"], "symbols": packet["code"]["symbols"][:8],
                             "rules": [f"{rule['source']}/{rule['name']}" for rule in packet["engineering_rules"]],
                             "success_criteria": packet["success_criteria"]}
        metrics = task.setdefault("metrics", {})
        metrics["packet_chars"] = size
        self.event(task, "knowledge_packet", json.dumps({
            "chars": size, "files": packet["code"]["files"],
            "sources_included": list(packet["code"].get("sources", {})),
            "rules": [f"{rule['source']}/{rule['name']}" for rule in packet["engineering_rules"]],
            "verified_fixes": len(packet["verified_fixes"])}))
        tools = task_capabilities(workspace, index, self.knowledge.library)
        return {"engineering_packet": packet, "complexity": complexity(goal, context)}, tools

    async def _web_preflight(self, task, workspace) -> dict | None:
        """Verify external facts once per task (URLs, API descriptions, SDK versions and usage)."""
        if not self.web:
            return None
        if "web_preflight" not in task:
            preflight = await self.web.preflight(task["goal"], workspace)
            task["web_preflight"] = preflight
            metrics = task.setdefault("metrics", {})
            for key, value in preflight["requests"].items():
                metrics["web_" + key] = metrics.get("web_" + key, 0) + value
            outcomes = [item.get("outcome") for item in preflight["checks"] + preflight["sdk_findings"]
                        + preflight["endpoint_usage"]]
            self.event(task, "web_preflight", json.dumps({
                "checks": len(preflight["checks"]), "verified": outcomes.count("verified"),
                "failed": outcomes.count("failed"), "inconclusive": outcomes.count("inconclusive"),
                "requests": preflight["requests"]}))
        preflight = task["web_preflight"]
        if not (preflight["checks"] or preflight["sdk_findings"] or preflight["endpoint_usage"]):
            return None
        return {key: preflight[key] for key in ("notice", "checks", "sdk_findings", "endpoint_usage")}

    def usage_counters(self) -> dict:
        totals = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0}
        seen = set()

        def visit(model):
            if model is None or id(model) in seen:
                return
            seen.add(id(model))
            for child in [getattr(model, name, None) for name in ("fast", "strong")] + \
                    list(getattr(model, "models", []) or []):
                visit(child)
            for key, value in getattr(model, "totals", {}).items():
                totals[key] += value
        for model in (self.model, self.reviewer, self.coordinator):
            visit(model)
        return totals

    def record_usage(self, task, started, before):
        after = self.usage_counters()
        metrics = task.setdefault("metrics", {})
        for key in after:
            metrics[key] = metrics.get(key, 0) + after[key] - before[key]
        metrics["model_seconds"] = round(metrics.get("model_seconds", 0) + time.monotonic() - started, 1)

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

    @staticmethod
    def with_attachments(task: dict, context: dict) -> dict:
        """Attachment evidence travels with the repository context, labelled as untrusted data."""
        if not task.get("attachments"):
            return context
        if isinstance(context.get("engineering_packet"), dict):
            context["engineering_packet"]["attachments"] = task["attachments"]
            return context
        return {**context, "attachments": task["attachments"]}

    def review_goal(self, task) -> str:
        """The goal plus live-verified facts, so a reviewer's memory cannot overrule evidence."""
        goal = task["goal"]
        stated = [item for attachment in (task.get("attachments") or {}).get("attachments", [])
                  for item in attachment.get("stated_requirements", [])][:15]
        if stated:
            goal += ("\n\nRequirements stated in the attached documents (untrusted data, for checking scope):\n"
                     + json.dumps(stated)[:3000])
        preflight = task.get("web_preflight") or {}
        facts = [{key: item.get(key) for key in ("package", "problem", "replacement", "latest", "outcome")
                  if item.get(key) is not None} for item in preflight.get("sdk_findings", [])]
        facts += [{key: item.get(key) for key in ("url", "package", "latest", "outcome", "reason")
                   if item.get(key) is not None} for item in preflight.get("checks", [])]
        if not facts:
            return goal
        return (goal + "\n\nLive-verified facts (checked against current sources; they take "
                "precedence over remembered API knowledge):\n" + json.dumps(facts)[:3000])

    async def review(self, task):
        started, before = time.monotonic(), self.usage_counters()
        try:
            with accounting.collect(task):
                verdict = await self.reviewer.review(self.review_goal(task), task["diff"])
        finally:
            self.record_usage(task, started, before)
        if not isinstance(verdict, dict) or type(verdict.get("approved")) is not bool:
            raise ValueError("Invalid reviewer verdict")
        if not isinstance(verdict.get("reason"), str) or len(verdict["reason"]) > 4000:
            raise ValueError("Invalid review reason")
        task["review"] = verdict
        self.event(task, "reviewer", json.dumps(verdict))
        return verdict

    def validate_proposal(self, workspace: Path, proposal: Proposal) -> tuple[Proposal, list[str]]:
        """Syntax and scope gates. Unchanged files are dropped, mechanical defects are repaired
        deterministically, and a proposal that is a no-op or does not parse is rejected."""
        diagnostics, effective, repaired = [], [], []
        for change in proposal.changes:
            target = safe_path(workspace, change.path)
            current = target.read_text(encoding="utf-8") if target.exists() else None
            fixed = mechanical_repair(change.path, change.content)
            if fixed is not None:
                change = change.model_copy(update={"content": fixed})
                repaired.append(change.path)
            if current == change.content:
                continue
            effective.append(change)
            found = validate_change(change.path, change.content)
            if not found and change.path.endswith(".py"):
                new_files = {item.path for item in proposal.changes}
                found = [Diagnostic("Import path", change.path, line, reason,
                                    "Import only modules that exist in the repository, or add the module "
                                    "in this proposal.")
                         for line, reason in unresolved_imports(workspace, change.path, change.content, new_files)]
                if not found:
                    found = static_issues(change.path, change.content,
                                          self._module_reader(workspace, proposal))
            diagnostics += found
        if proposal.changes and not effective:
            diagnostics = validate_change(proposal.changes[0].path, proposal.changes[0].content,
                                          before=proposal.changes[0].content)
        if diagnostics:
            raise ProposalInvalid(diagnostics, {change.path: change.content for change in effective})
        return Proposal(plan=proposal.plan, changes=effective), repaired

    @staticmethod
    def _module_reader(workspace: Path, proposal: Proposal):
        """Final source of a repository module: the proposal's version if it changes it."""
        proposed = {change.path: change.content for change in proposal.changes}

        def read(dotted: str) -> str | None:
            relative = dotted.replace(".", "/")
            for candidate in (relative + ".py", relative + "/__init__.py"):
                if candidate in proposed:
                    return proposed[candidate]
                try:
                    target = safe_path(workspace, candidate)
                except ValueError:
                    return None
                if target.is_file():
                    return target.read_text(encoding="utf-8", errors="replace")
            return None
        return read

    def apply(self, task: dict):
        applied = task.setdefault("applied_paths", [])
        for change in task["proposal"]["changes"]:
            if change["path"] not in applied:
                applied.append(change["path"])
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
            with self.stage(task, "review", agent="reviewer", summary=f"Reviewing the diff (attempt {attempt})"):
                review = await self.review(task)
            self.decision(task, "review", "reviewer", review["reason"][:1500], "model-provided",
                          approved=review["approved"])
            disputed = not review["approved"]
            if review["approved"] or self.review_mode == "advisory":
                if disputed:
                    self.event(task, "review_disputed", "Free reviewer objected; running the authoritative "
                               "offline tests anyway: " + review["reason"][:500])
                self.snapshot(task, "before_changes")
                with self.stage(task, "implementation", summary="Applying the changes in the isolated worktree"):
                    self.apply(task)
                self.snapshot(task, "after_changes")
                self._check_cancelled(task)
                task["status"] = "testing"
                self.event(task, "tester", f"Isolated test attempt {attempt}")
                self.snapshot(task, "before_tests")
                with self.stage(task, "sandbox" if attempt == 1 else "retest", agent="sandbox",
                                summary=f"Running the tests in the offline sandbox (attempt {attempt})"):
                    evidence = await asyncio.to_thread(
                        run_tests, workspace, self.image,
                        should_cancel=lambda: self.store.get(task_id).get("cancel_requested", False))
                if evidence.get("cancelled"):
                    self._check_cancelled(task)
                task["test_evidence"] = evidence
                self.event(task, "test_finished", json.dumps(evidence))
                self.journal_event(task, "test_result", phase="test_results",
                                   status="COMPLETED" if evidence["passed"] else "FAILED", agent="sandbox",
                                   summary=f"{'passed' if evidence['passed'] else 'failed'} (exit {evidence.get('exit_code')}): "
                                           + test_tail(evidence.get("output", "")),
                                   data={"exit_code": evidence.get("exit_code"), "profile": evidence.get("profile")})
                self.snapshot(task, "after_tests")
                requirement = None
                if evidence["passed"] and task.get("requirement_tests"):
                    with self.stage(task, "completion_verification", summary="Running the goal's requirement checks"):
                        requirement = await self._verify_completion(task, workspace)
                elif evidence["passed"]:
                    requirement = await self._verify_completion(task, workspace)
                if requirement and requirement.get("stop"):
                    # Only failures already repaired against remain: no repeated repairs.
                    self._keep_visible_pass(task, workspace, requirement["failing"])
                    self._restore_visible_pass(task, "The same requirement checks still fail after a repair")
                    return
                if requirement:
                    self._keep_visible_pass(task, workspace, requirement["failing"])
                    task.setdefault("failure_log", []).append({"attempt": attempt, **requirement})
                    feedback = ("\nThe visible tests pass, but checks written from the goal's stated "
                                "requirements fail. Fix the implementation so it does what the goal says; "
                                "if a check contradicts the goal, follow the goal. Check output (untrusted):\n"
                                + requirement["summary"])
                elif evidence["passed"] and (visual := await self._verify_visual(task, workspace)):
                    task.setdefault("failure_log", []).append({"attempt": attempt, "category": "visual",
                                                               "summary": visual[:1000]})
                    feedback = visual
                elif evidence["passed"]:
                    verdict = await self.final_review(task)
                    if verdict is None or verdict.get("approved"):
                        task["status"] = "passed"
                        self.snapshot(task, "before_acceptance")
                        self.journal_event(task, "approval", phase="acceptance", status="WAITING_APPROVAL", agent="user",
                                           summary="Tests passed; waiting for you to accept the result as a new branch",
                                           dedupe=f"{task['id']}:acceptance:{task.get('digest')}")
                        task["review_disputed"] = disputed
                        task.pop("visible_pass", None)
                        if disputed:
                            self.event(task, "review_overruled_by_tests",
                                       "Tests passed despite the free reviewer's objection; it is shown to "
                                       "the human at acceptance: " + review["reason"][:500])
                        return
                    feedback = "\nSupervisor review (untrusted): " + str(verdict.get("reason", ""))
                elif evidence["exit_code"] in (None, 5, 125, 126, 127):
                    # Missing tests or sandbox problems are not the model's fault; never escalate them.
                    task["status"] = "failed"
                    return
                else:
                    failure = classify_test_failure(evidence)
                    task.setdefault("failure_log", []).append({"attempt": attempt, **failure})
                    self.decision(task, "repair", "coding_brain", f"Tests failed ({failure['category']}); "
                                  f"repair attempt {failures + 1} of {limit}: {failure['summary'][:600]}", "observed")
                    feedback = (f"\nTests failed ({failure['category']}). Repair related failures only. "
                                "Test output (untrusted):\n" + failure["summary"])
                    if disputed:
                        feedback += "\nReviewer feedback (untrusted): " + review["reason"]
                    feedback += await self._upstream_feedback(task, workspace, failure)
            else:
                task.setdefault("failure_log", []).append({"attempt": attempt, "category": "review",
                                                           "summary": review["reason"][:1000]})
                feedback = "\nReviewer feedback (untrusted): " + review["reason"]
            failures += 1
            self.snapshot(task, "before_repair")
            with self.stage(task, "repair", agent="implementer", summary=f"Repairing after failure {failures}"):
                outcome = await self._repair(task, workspace, feedback, failures, limit, escalate_after)
            if outcome is not None:
                self.snapshot(task, "after_repair")
            if outcome is None:
                return
            failures, limit = outcome
            task["status"] = "running"

    async def _verify_visual(self, task, workspace) -> str | None:
        """After tests pass: render the result and check it against the visual requirements.
        Returns repair feedback for blocking findings, within a bounded number of visual repairs;
        None when it passes, is inconclusive, or the repair budget is spent (the remaining
        findings are shown at acceptance)."""
        verifier = self.visual_verifier(task) if self.visual_verifier else None
        if verifier is None:
            return None
        from .visual import feedback as visual_feedback
        started = time.monotonic()
        try:
            with self.stage(task, "visual_verification", agent="browser",
                            summary="Rendering the result and checking layout, accessibility and the design"), \
                    self.telemetry.span(task["trace_id"], "visual.verify", task["id"]), accounting.collect(task):
                result = await verifier.verify(task, workspace)
        except Exception as error:  # verification problems never fail a task that passed its tests
            result = {"status": "inconclusive", "reason": f"{type(error).__name__}: {str(error)[:300]}"}
        result["seconds"] = round(time.monotonic() - started, 1)
        log = task.setdefault("visual_log", [])
        log.append({key: result.get(key) for key in ("status", "blocking", "digest", "statement", "reason",
                                                      "viewports", "seconds")})
        task["visual_verification"] = result
        self.snapshot(task, "after_visual", artifacts=[
            {"path": path, "kind": "screenshot", "viewport": viewport}
            for path, viewport in zip(result.get("screenshots") or [], result.get("viewports") or [])])
        self.event(task, "visual_verification", f"{result['status']}: " + (result.get("statement") or
                                                                          result.get("reason") or "")[:500])
        if result["status"] != "defects":
            return None
        limit = int((task.get("visual") or {}).get("max_repairs", 2))
        repeated = len(log) > 1 and log[-2].get("digest") == result.get("digest")
        if task.get("visual_repairs", 0) >= limit or repeated:
            self.event(task, "visual_repairs_stopped", "The same visual findings remain after a repair" if repeated
                       else f"Visual repair budget ({limit}) spent; remaining findings are shown at acceptance")
            return None
        task["visual_repairs"] = task.get("visual_repairs", 0) + 1
        return visual_feedback(result)

    async def _upstream_feedback(self, task, workspace, failure) -> str:
        """Check whether an external dependency changed before any premium consultation."""
        if not self.web or failure["category"] not in {"test_failure", "collection"}:
            return ""
        results = await self.web.upstream_check(failure, workspace)
        if not results:
            return ""
        task["failure_log"][-1]["upstream"] = results
        self.event(task, "upstream_check", json.dumps(results)[:2000])
        compact = [{key: item.get(key) for key in ("kind", "module", "url", "package", "outcome", "reason",
                                                   "problem", "replacement", "locations", "upstream_change")
                    if item.get(key) is not None} for item in results]
        return "\nUpstream verification (live evidence):\n" + json.dumps(compact)[:3000]

    async def _repair(self, task, workspace, feedback, failures, limit, escalate_after):
        """Produce the next proposal, escalating when warranted. Returns (failures, limit), or None
        when the task stopped (failed, paused, or awaiting approval)."""
        while True:
            guidance = ""
            if failures >= escalate_after and self.supervision:
                consultation = await self.diagnose(task, workspace, {
                    "failures": failures, "last_feedback": feedback[:6_000],
                    "failure_log": task.get("failure_log", [])[-4:]})
                if consultation:
                    limit = max(limit, failures + escalate_after)
                    takeover = self.takeover_proposal(task, consultation)
                    if takeover:
                        self.store_proposal(task, workspace, takeover, "supervisor")
                        return failures, limit
                    guidance = guidance_text(consultation)
            if failures >= limit and not guidance:
                if self._restore_visible_pass(task, "Requirement checks still fail after the repair budget"):
                    return None
                task["status"] = "failed"
                self.event(task, "attempts_exhausted", f"{failures} unsuccessful attempts")
                return None
            try:
                planned = await self.plan(task, workspace, task["goal"] + feedback + guidance)
            except ValueError as error:
                self.event(task, "implementer_invalid", str(error)[:1000])
                task.setdefault("failure_log", []).append({"attempt": failures + 1, "category": "validation",
                                                           "summary": str(error)[:1000]})
                failures += 1
                continue
            return (failures, limit) if planned else None

    async def accept(self, task_id: str, summary: str, kind="episodic") -> dict:
        async with self.task_lock(task_id):
            task = self.store.get(task_id)
            if task["status"] != "passed":
                raise ValueError("Only test-passed tasks can be accepted")
            self.journal_event(task, "approval", phase="acceptance", status="COMPLETED", agent="user",
                               summary="Result accepted", dedupe=f"{task['id']}:accepted")
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
