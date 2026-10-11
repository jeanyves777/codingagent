"""Continuous orchestration: a persistent capability backlog and a conductor that keeps every
available agent working on the highest-value ready item until it is independently verified.

The pieces it builds on already exist and are reused, not duplicated: isolated task worktrees
(workspaces.py), the task lifecycle and repair loop (service.py), supervisor budgets
(supervision.py), the activity journal (telemetry.py) and the offline sandbox (sandbox.py).
What this module adds:

- Backlog: items with a priority, dependencies, acceptance tests written before any work starts,
  attempts, and a hash-chained evidence ledger and decision log, all in one SQLite file, so the
  whole state survives a restart.
- Conductor: on every tick it reconciles running attempts, detects stalls, verifies finished
  work independently, escalates within budget, and fills every free agent slot with the next
  ready item (priority first, then age). An agent only idles when nothing safe is ready.
- Independent verification: an implementer's "passed" is never enough. The item's own frozen
  acceptance tests are restored over a throwaway copy of the agent's workspace and run in the
  sandbox. Only that makes an item VERIFIED, and an item without acceptance tests can never be.

Human approval boundaries are unchanged: a proposal is executed without asking only when the
user configured autonomy.execution = auto; acceptance onto a branch is always the user's; a
dependency is satisfied only by accepted (DONE) work. While an item waits for a person, the
conductor keeps the other agents busy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .redaction import redact

PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
# ready -> running -> (awaiting_approval) -> verifying -> verified -> done (accepted by the user)
# waiting: dependencies not done yet. blocked: no further safe attempt, with the reason recorded.
STATES = {"ready", "waiting", "running", "awaiting_approval", "verifying", "verified", "done",
          "blocked", "cancelled"}
SETTLED = {"verified", "done", "blocked", "cancelled"}  # verified only waits for the user's acceptance
STRATEGIES = ("local", "local_with_feedback", "premium_plan")
ACTIVE_TASK = {"queued", "planning", "running", "testing", "cancellation_requested", "waiting"}
FAILED_TASK = {"failed", "blocked", "cancelled", "integration_conflict", "awaiting_implementer"}
# Files through which code under test can change how tests are collected or run (skip everything,
# add options). The verifier puts back the project's own versions before running acceptance tests.
TEST_CONFIG = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "coding-brain.json"}
# Top-level names that would replace the test runner itself (`python -m pytest` imports from the
# project folder first). A change that adds one is never verified.
RUNNER_SHADOWS = {"pytest", "pytest.py", "_pytest", "pluggy", "py.py", "iniconfig", "sitecustomize.py",
                  "usercustomize.py"}
NODE_ID = re.compile(r"[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_\[\]-]+)*")


class BacklogError(ValueError):
    pass


def _safe_relative(path: str) -> str:
    pure = PurePosixPath(path.replace("\\", "/"))
    if not path or pure.is_absolute() or ".." in pure.parts or ":" in path or pure.parts[0] == ".git":
        raise BacklogError(f"acceptance test path must stay inside the project: {path!r}")
    return pure.as_posix()


def _check_acceptance(acceptance: dict | None) -> dict | None:
    """Acceptance tests: files written before work starts plus the pytest node ids that must pass."""
    if not acceptance:
        return None
    files = {_safe_relative(path): str(content) for path, content in (acceptance.get("files") or {}).items()}
    nodes = list(acceptance.get("nodes") or [])
    if not files or not nodes:
        raise BacklogError("acceptance needs test files and the pytest node ids to run")
    for node in nodes:
        if not NODE_ID.fullmatch(node) or node.split("::")[0] not in files:
            raise BacklogError(f"acceptance node {node!r} must name a test in the acceptance files")
    return {"files": files, "nodes": nodes, "criteria": str(acceptance.get("criteria") or "")[:4000]}


class Backlog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, title TEXT, goal TEXT, priority INTEGER,
                    depends_on TEXT, acceptance TEXT, state TEXT, reason TEXT, attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER, task_id TEXT, feedback TEXT, last_signature TEXT,
                    created REAL, updated REAL);
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, item_id TEXT, number INTEGER,
                    strategy TEXT, agent TEXT, task_id TEXT, started REAL, ended REAL, outcome TEXT,
                    signature TEXT, detail TEXT, executed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS ledger (seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT,
                    attempt_id TEXT, at REAL, kind TEXT, verdict TEXT, evidence TEXT, digest TEXT);
                CREATE TABLE IF NOT EXISTS decisions (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL,
                    item_id TEXT, decision TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS integrations (id TEXT PRIMARY KEY, base TEXT, head TEXT, workspace TEXT,
                    included TEXT, excluded TEXT, state TEXT, branch TEXT, created REAL, updated REAL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
            if "base_commit" not in columns:  # backlogs created before integration existed
                db.execute("ALTER TABLE items ADD COLUMN base_commit TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # items ------------------------------------------------------------------------------------
    def add(self, goal: str, title: str | None = None, priority: str = "P1", depends_on=(),
            acceptance: dict | None = None, max_attempts: int = 3) -> dict:
        if priority not in PRIORITIES:
            raise BacklogError(f"priority must be one of {', '.join(PRIORITIES)}")
        if not goal.strip():
            raise BacklogError("an item needs a goal")
        if not 1 <= int(max_attempts) <= len(STRATEGIES) * 2:
            raise BacklogError("max_attempts must be between 1 and 6")
        depends_on = list(dict.fromkeys(depends_on))
        known = {item["id"] for item in self.list()}
        missing = [dependency for dependency in depends_on if dependency not in known]
        if missing:
            raise BacklogError(f"unknown dependencies: {', '.join(missing)}")
        item_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO items (id, title, goal, priority, depends_on, acceptance, state, attempts, "
                       "max_attempts, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (item_id, (title or goal)[:200], goal[:8000], PRIORITIES[priority], json.dumps(depends_on),
                        json.dumps(_check_acceptance(acceptance)), "waiting" if depends_on else "ready", 0,
                        int(max_attempts), now, now))
        self.decide(item_id, "added", f"{priority}: {(title or goal)[:200]}")
        return self.get(item_id)

    def _row(self, row) -> dict:
        item = dict(row)
        item["depends_on"] = json.loads(item["depends_on"] or "[]")
        item["acceptance"] = json.loads(item["acceptance"] or "null")
        item["priority_name"] = next(name for name, value in PRIORITIES.items() if value == item["priority"])
        return item

    def get(self, item_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM items WHERE id LIKE ?", (item_id + "%",)).fetchall()
        if len(rows) != 1:
            raise KeyError(f"no single backlog item matches {item_id!r}")
        return self._row(rows[0])

    def list(self, states=None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM items ORDER BY priority, created").fetchall()
        items = [self._row(row) for row in rows]
        return [item for item in items if not states or item["state"] in states]

    def update(self, item_id: str, **fields):
        fields["updated"] = time.time()
        with self.connect() as db:
            db.execute(f"UPDATE items SET {', '.join(f'{key}=?' for key in fields)} WHERE id=?",
                       (*fields.values(), item_id))

    # attempts, ledger, decisions ---------------------------------------------------------------
    def start_attempt(self, item: dict, strategy: str, agent: str, task_id: str, started: float | None = None) -> str:
        attempt_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO attempts (id, item_id, number, strategy, agent, task_id, started) "
                       "VALUES (?,?,?,?,?,?,?)", (attempt_id, item["id"], item["attempts"] + 1, strategy, agent,
                                                  task_id, time.time() if started is None else started))
        self.update(item["id"], state="running", task_id=task_id, attempts=item["attempts"] + 1)
        return attempt_id

    def open_attempts(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM attempts WHERE ended IS NULL ORDER BY started")]

    def attempts(self, item_id: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM attempts" + (" WHERE item_id=?" if item_id else "") +
                              " ORDER BY started", (item_id,) if item_id else ()).fetchall()
        return [dict(row) for row in rows]

    def end_attempt(self, attempt_id: str, outcome: str, signature: str = "", detail: str = ""):
        with self.connect() as db:
            db.execute("UPDATE attempts SET ended=?, outcome=?, signature=?, detail=? WHERE id=?",
                       (time.time(), outcome, signature, redact(detail)[-4000:], attempt_id))

    def mark_executed(self, attempt_id: str):
        with self.connect() as db:
            db.execute("UPDATE attempts SET executed=1 WHERE id=?", (attempt_id,))

    def record(self, item_id: str, attempt_id: str | None, kind: str, verdict: str, evidence: dict) -> dict:
        """Append evidence. Each entry's digest covers the previous one, so a changed or removed
        entry breaks the chain (verify_ledger)."""
        body = json.dumps(evidence, sort_keys=True, default=str)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT digest FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            at = time.time()
            digest = hashlib.sha256("|".join([previous["digest"] if previous else "", item_id, attempt_id or "",
                                              f"{at:.6f}", kind, verdict, body]).encode()).hexdigest()
            db.execute("INSERT INTO ledger (item_id, attempt_id, at, kind, verdict, evidence, digest) "
                       "VALUES (?,?,?,?,?,?,?)", (item_id, attempt_id, at, kind, verdict, body, digest))
        return {"kind": kind, "verdict": verdict, "digest": digest}

    def ledger(self, item_id: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM ledger" + (" WHERE item_id=?" if item_id else "") + " ORDER BY seq",
                              (item_id,) if item_id else ()).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]

    def verify_ledger(self) -> bool:
        previous = ""
        for entry in self.ledger():
            body = json.dumps(entry["evidence"], sort_keys=True, default=str)
            expected = hashlib.sha256("|".join([previous, entry["item_id"], entry["attempt_id"] or "",
                                                f"{entry['at']:.6f}", entry["kind"], entry["verdict"],
                                                body]).encode()).hexdigest()
            if expected != entry["digest"]:
                return False
            previous = entry["digest"]
        return True

    def decide(self, item_id: str | None, decision: str, detail: str = ""):
        with self.connect() as db:
            db.execute("INSERT INTO decisions (at, item_id, decision, detail) VALUES (?,?,?,?)",
                       (time.time(), item_id, decision, redact(detail)[:2000]))

    def decisions(self, item_id: str | None = None, limit: int = 200) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM decisions" + (" WHERE item_id=?" if item_id else "") +
                              " ORDER BY seq DESC LIMIT ?", ((item_id, limit) if item_id else (limit,))).fetchall()
        return [dict(row) for row in reversed(rows)]

    # integrations -----------------------------------------------------------------------------
    def save_integration(self, record: dict) -> dict:
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO integrations (id, base, head, workspace, included, excluded, state, created, updated) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (record["id"], record["base"], record["head"], record["workspace"], json.dumps(record["included"]),
                        json.dumps(record["excluded"]), record.get("state", "candidate"), now, now))
        return self.integration(record["id"])

    def integrations(self, states=None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM integrations ORDER BY created").fetchall()
        records = [{**dict(row), "included": json.loads(row["included"]), "excluded": json.loads(row["excluded"])}
                   for row in rows]
        return [record for record in records if not states or record["state"] in states]

    def integration(self, integration_id: str | None = None) -> dict | None:
        """One integration by id prefix, or (without an id) the candidate waiting for the user."""
        records = self.integrations() if integration_id else self.integrations({"candidate"})
        if integration_id:
            records = [record for record in records if record["id"].startswith(integration_id)]
            if len(records) != 1:
                raise KeyError(f"no single integration matches {integration_id!r}")
        return records[-1] if records else None

    def set_integration(self, integration_id: str, **fields):
        fields["updated"] = time.time()
        with self.connect() as db:
            db.execute(f"UPDATE integrations SET {', '.join(f'{key}=?' for key in fields)} WHERE id=?",
                       (*fields.values(), integration_id))

    def accepted_head(self) -> str | None:
        """Where new work starts: the latest integration the user accepted (None: the project's HEAD)."""
        accepted = self.integrations({"accepted"})
        return accepted[-1]["head"] if accepted else None


def backlog_status(backlog: Backlog | None, local_slots: int) -> dict:
    """The backlog as people and programs see it: Conductor.status, `codingbrain backlog status`
    and the engine API's backlog.status all use this one function. It only reads: it starts no
    agent, no task and no Brain service. `None` is a project that has no backlog yet."""
    items = backlog.list() if backlog else []
    by_id = {item["id"]: item for item in items}
    counts = {state: sum(1 for item in items if item["state"] == state) for state in sorted(STATES)}
    running = [attempt for attempt in (backlog.open_attempts() if backlog else [])
               if by_id.get(attempt["item_id"], {}).get("state") in {"running", "verifying"}]
    people = {"awaiting_approval", "verified"}
    return {
        "counts": {state: count for state, count in counts.items() if count},
        "agents": {"local_slots": local_slots,
                   "busy": [{"item": a["item_id"], "strategy": a["strategy"], "task": a["task_id"]} for a in running]},
        "items": [{"id": item["id"], "title": item["title"], "priority": item["priority_name"],
                   "state": item["state"], "attempts": item["attempts"], "reason": item["reason"] or ""}
                  for item in items],
        "finished": all(item["state"] in {"done", "blocked", "cancelled"} for item in items),
        # nothing is running or ready, and what is left waits for a person (an approval, an
        # acceptance, or a dependency that is verified but not yet accepted)
        "waiting_on_people_only": bool(items) and not running and
                                  not any(item["state"] in {"ready", "running", "verifying"} for item in items) and
                                  any(item["state"] in people for item in items),
        "ledger_intact": backlog.verify_ledger() if backlog else True,
        "integration": _integration_summary(backlog.integration() if backlog else None),
    }


def _integration_summary(record: dict | None) -> dict | None:
    if not record:
        return None
    return {"id": record["id"], "state": record["state"], "base": record["base"], "head": record["head"],
            "included": record["included"], "excluded": {key: value.get("reason") for key, value in record["excluded"].items()}}


def ledger_report(backlog: Backlog | None, item: str | None = None) -> dict:
    """The evidence ledger (optionally one item's entries, by id prefix) and whether its hash
    chain is intact. Read-only; used by `codingbrain backlog ledger` and the engine API."""
    if backlog is None:
        if item:
            raise KeyError(f"no single backlog item matches {item!r}")
        return {"intact": True, "entries": []}
    return {"intact": backlog.verify_ledger(), "entries": backlog.ledger(backlog.get(item)["id"] if item else None)}


def add_from_project(backlog: Backlog, root: Path, goal: str, *, title: str | None = None, priority: str = "P1",
                     depends_on=(), accept_files=(), accept_nodes=(), criteria: str | None = None,
                     max_attempts: int = 3) -> dict:
    """Add an item whose acceptance tests are files of the project at `root`, frozen now: later
    edits to those files (by anyone, including an agent) do not change what verification runs.
    Shared by `codingbrain backlog add` and the engine API's backlog.add. Dependencies may be
    given by id prefix."""
    acceptance = None
    if accept_files or accept_nodes:
        files = {}
        root = Path(root).resolve()
        for name in accept_files:
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise BacklogError(f"acceptance file must be a file inside the project: {name}")
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")  # frozen now
        acceptance = {"files": files, "nodes": list(accept_nodes), "criteria": criteria or ""}
    return backlog.add(goal, title=title, priority=priority,
                       depends_on=[backlog.get(ref)["id"] for ref in depends_on],
                       acceptance=acceptance, max_attempts=max_attempts)


class Resources:
    """How many local agents this machine runs efficiently: one per `per_agent_gb` of memory and
    two cores, never more than `max_local` or than the generations the model server was measured
    to run at once (more agents would only queue for it), at least one. Read again on every tick."""

    def __init__(self, facts=None, per_agent_gb: float = 6.0, max_local: int = 4, inference: int | None = None):
        self.facts, self.per_agent_gb, self.max_local = facts, per_agent_gb, max(1, int(max_local))
        self.inference = inference  # generations the model server was measured to run at once

    def local_slots(self) -> int:
        try:
            facts = self.facts() if self.facts else {}
        except Exception:
            facts = {}
        limits = [self.max_local] + ([max(1, int(self.inference))] if self.inference else [])
        if self.facts and not facts:
            return 1  # the machine could not be read: run one agent, not the maximum
        if facts.get("ram"):
            limits.append(int(float(facts["ram"]) / 1e9 // self.per_agent_gb))
        if facts.get("cores"):
            limits.append(int(facts["cores"]) // 2)
        return max(1, min(limits))


def acceptance_passed(result: dict, nodes: list[str]) -> bool:
    """Every acceptance test must actually pass: a zero exit code is not enough when tests were
    skipped, expected to fail, deselected or not collected."""
    output = str(result.get("output") or "")
    passed = sum(int(count) for count in re.findall(r"(\d+) passed", output))
    weakened = re.search(r"\d+ (skipped|xfailed|xpassed|deselected)|no tests ran", output)
    return result.get("exit_code") == 0 and bool(result.get("passed")) and passed >= len(nodes) and not weakened


def signature(text: str) -> str:
    """A stable fingerprint of a failure: digits, hex ids and paths vary between runs."""
    normalized = re.sub(r"0x[0-9a-f]+|[0-9a-f]{12,}|\d+(\.\d+)?|/[^\s:]+", "#", (text or "").lower())
    return hashlib.sha256(" ".join(normalized.split())[-2000:].encode()).hexdigest()[:16]


class Conductor:
    """Keeps every agent slot busy with the highest-value ready backlog item until each item is
    VERIFIED (independently), DONE (accepted by the user) or BLOCKED (with the reason).

    `executor` runs the actual work (BrainExecutor for the real Brain; fakes in tests). Its
    methods are coroutines: start(item, strategy, feedback) -> task id; poll(task id) -> {status,
    progress_at, digest, failure}; approve(task id, digest); stop(task id); verify(task id, item)
    -> {passed, exit_code, output, command}; premium_available() -> bool."""

    def __init__(self, backlog: Backlog, executor, *, auto_execute: bool = False, stall_seconds: float = 900,
                 resources: Resources | None = None, clock=time.time):
        self.backlog, self.executor = backlog, executor
        self.auto_execute, self.stall_seconds = auto_execute, stall_seconds
        self.resources, self.clock = resources or Resources(), clock
        self.progress: dict[str, float] = {}  # attempt -> last time progress was seen
        self._premium = False

    # one scheduling pass ----------------------------------------------------------------------
    async def tick(self) -> dict:
        actions = {"started": [], "verified": [], "requeued": [], "blocked": [], "stalled": [], "waiting": [],
                   "integrated": []}
        self._premium = await self.executor.premium_available()  # budgets can change between ticks
        self._dependencies(actions)
        for attempt in self.backlog.open_attempts():
            await self._reconcile(attempt, actions)
        await self._integrate(actions)
        await self._dispatch(actions)
        return actions

    async def _integrate(self, actions):
        """When no agent is working, combine every verified item into one candidate on top of the
        accepted work: each item is applied in turn and must keep every included item's frozen
        acceptance tests and the project's own tests passing together. An item that conflicts or
        breaks the combination goes back to an agent, to be redone on top of the others. Nothing
        reaches the user's branches: the candidate waits for `codingbrain backlog accept`."""
        if not hasattr(self.executor, "integrate"):
            return
        items = self.backlog.list()
        if any(item["state"] in {"running", "verifying"} for item in items):
            return
        verified = [item for item in items if item["state"] == "verified"]
        current = self.backlog.integration()
        if not verified or (current and set(current["included"]) == {item["id"] for item in verified}):
            return
        first = set(current["included"]) if current else set()  # keep what already combined first
        ordered = [item for item in verified if item["id"] in first] + [item for item in verified if item["id"] not in first]
        attempts = {attempt["item_id"]: attempt for attempt in self.backlog.open_attempts()}
        ordered = [{**item, "task_id": attempts[item["id"]]["task_id"]} for item in ordered if item["id"] in attempts]
        if not ordered:
            return
        result = await self.executor.integrate(ordered, self.backlog.accepted_head())
        if current:
            self.backlog.set_integration(current["id"], state="superseded")
        for item in ordered:
            verdict = "included" if item["id"] in result["included"] else result["excluded"][item["id"]]["reason"]
            evidence = {"integration": result["id"], "base": result["base"], "head": result["head"],
                        **{key: value for key, value in result["excluded"].get(item["id"], {}).items() if key != "reason"}}
            self.backlog.record(item["id"], attempts[item["id"]]["id"], "integration", verdict, evidence)
        if result["included"]:
            self.backlog.save_integration({**result, "state": "candidate"})
            for item_id in result["included"]:
                self.backlog.update(item_id, reason=f"verified and combined in integration {result['id']} "
                                                    "with the other verified items; waits for your acceptance")
            self.backlog.decide(result["included"][0], "integrated",
                                f"integration {result['id']}: {len(result['included'])} item(s) pass together")
            actions["integrated"].extend(result["included"])
        for item_id, excluded in result["excluded"].items():
            item = self.backlog.get(item_id)
            self.backlog.update(item_id, base_commit=result["head"] if result["included"] else None)
            detail = (f"your change {excluded['reason'].replace('_', ' ')} when combined with the other verified "
                      f"items ({', '.join(result['included']) or 'none'}); redo it on top of them:\n"
                      + str(excluded.get("detail", ""))[-2500:])
            await self._failed(item, attempts[item_id], excluded["reason"], detail, actions)

    def _dependencies(self, actions):
        items = {item["id"]: item for item in self.backlog.list()}
        for item in items.values():
            if item["state"] not in {"waiting", "ready"} or not item["depends_on"]:
                continue
            dependencies = [items[dependency] for dependency in item["depends_on"]]
            stuck = [dependency for dependency in dependencies if dependency["state"] in {"blocked", "cancelled"}]
            if stuck:
                reason = f"dependency {stuck[0]['id']} ({stuck[0]['title'][:80]}) is {stuck[0]['state']}"
                self.backlog.update(item["id"], state="blocked", reason=reason)
                self.backlog.decide(item["id"], "blocked", reason)
                actions["blocked"].append(item["id"])
            else:
                state = "ready" if all(dependency["state"] == "done" for dependency in dependencies) else "waiting"
                if state != item["state"]:
                    self.backlog.update(item["id"], state=state)

    async def _reconcile(self, attempt: dict, actions):
        item = self.backlog.get(attempt["item_id"])
        try:
            task = await self.executor.poll(attempt["task_id"])
        except KeyError:  # the task is gone (for example, its data was removed while we were down)
            return await self._failed(item, attempt, "lost", "the task record no longer exists", actions)
        status = task["status"]
        if status in {"accepted", "completed"}:
            if item["state"] == "verified":
                self.backlog.end_attempt(attempt["id"], "done")
                self.backlog.update(item["id"], state="done", reason="")
                self.backlog.record(item["id"], attempt["id"], "acceptance", "accepted",
                                    {"task_id": attempt["task_id"], "by": "user"})
                self.backlog.decide(item["id"], "done", "accepted by the user after independent verification")
            elif item["state"] != "done":
                await self._verify(item, attempt, actions)  # accepted before we verified: verify now
            return
        if status == "passed":
            if item["state"] != "verified":
                await self._verify(item, attempt, actions)
            return
        if status == "proposed":
            if self.auto_execute and task.get("digest") and not attempt["executed"]:
                self.backlog.mark_executed(attempt["id"])
                self.backlog.decide(item["id"], "executed", "proposal executed: autonomy.execution is auto")
                await self.executor.approve(attempt["task_id"], task["digest"])
                self.progress[attempt["id"]] = self.clock()
                self.backlog.update(item["id"], state="running")
            elif item["state"] != "awaiting_approval":
                self.backlog.update(item["id"], state="awaiting_approval",
                                    reason="the proposal waits for your approval")
                actions["waiting"].append(item["id"])
            return
        if status == "awaiting_tool_approval":
            if item["state"] != "awaiting_approval":
                self.backlog.update(item["id"], state="awaiting_approval", reason="a protected tool waits for you")
                actions["waiting"].append(item["id"])
            return
        if status in FAILED_TASK:
            return await self._failed(item, attempt, "failed", task.get("failure") or status, actions)
        # still working: a stall is no new progress for stall_seconds
        seen = max(filter(None, [task.get("progress_at"), self.progress.get(attempt["id"]), attempt["started"]]))
        self.progress[attempt["id"]] = seen
        if item["state"] != "running":
            self.backlog.update(item["id"], state="running", reason="")
        if self.clock() - seen > self.stall_seconds:
            await self.executor.stop(attempt["task_id"])
            actions["stalled"].append(item["id"])
            await self._failed(item, attempt, "stalled",
                               f"no progress for {int(self.clock() - seen)} s (limit {int(self.stall_seconds)} s)", actions)

    async def _verify(self, item: dict, attempt: dict, actions):
        if not item["acceptance"]:
            self.backlog.end_attempt(attempt["id"], "unverifiable")
            reason = "no acceptance tests: the work cannot be verified independently (add them, then retry)"
            self.backlog.update(item["id"], state="blocked", reason=reason)
            self.backlog.record(item["id"], attempt["id"], "verification", "impossible", {"reason": reason})
            self.backlog.decide(item["id"], "blocked", reason)
            actions["blocked"].append(item["id"])
            return
        self.backlog.update(item["id"], state="verifying")
        result = await self.executor.verify(attempt["task_id"], item)
        output = redact(str(result.get("output") or ""))
        evidence = {"task_id": attempt["task_id"], "strategy": attempt["strategy"], "nodes": item["acceptance"]["nodes"],
                    "command": result.get("command"), "exit_code": result.get("exit_code"),
                    "output_sha256": hashlib.sha256(output.encode()).hexdigest(), "output_tail": output[-1500:]}
        if result.get("passed"):
            self.backlog.record(item["id"], attempt["id"], "verification", "passed", evidence)
            # The attempt stays open (it uses no agent slot) so the user's acceptance is seen.
            self.backlog.update(item["id"], state="verified", reason="independently verified; waits for your acceptance",
                                feedback=None)
            self.backlog.decide(item["id"], "verified", f"acceptance tests passed (exit {result.get('exit_code')})")
            actions["verified"].append(item["id"])
        else:
            self.backlog.record(item["id"], attempt["id"], "verification", "failed", evidence)
            await self._failed(item, attempt, "verification_failed",
                               "the agent reported success but the acceptance tests failed:\n" + output[-3000:], actions)

    async def _failed(self, item: dict, attempt: dict, outcome: str, detail: str, actions):
        fingerprint = signature(detail)
        self.backlog.end_attempt(attempt["id"], outcome, fingerprint, detail)
        self.progress.pop(attempt["id"], None)
        item = self.backlog.get(item["id"])
        repeated = fingerprint == item["last_signature"]
        if item["attempts"] >= item["max_attempts"]:
            reason = f"{item['attempts']} attempts used; last: {outcome}: {detail[:300]}"
            return self._block(item, reason, actions)
        nxt = self._strategy(item["attempts"] + 1, repeated)
        if nxt is None:
            why = "the same failure repeated" if repeated else "no further strategy"
            return self._block(item, f"{why} and no premium supervisor budget is available; last: {outcome}: "
                                     f"{detail[:300]}", actions)
        self.backlog.update(item["id"], state="ready", reason=f"retrying after {outcome}",
                            feedback=f"Previous attempt {outcome}: {detail[-2500:]}", last_signature=fingerprint)
        self.backlog.decide(item["id"], "requeued", f"{outcome}; next strategy {nxt}" + (" (repeated failure)" if repeated else ""))
        actions["requeued"].append(item["id"])

    def _block(self, item, reason, actions):
        self.backlog.update(item["id"], state="blocked", reason=reason)
        self.backlog.decide(item["id"], "blocked", reason)
        actions["blocked"].append(item["id"])

    def _strategy(self, number: int, repeated: bool = False) -> str | None:
        """First a local attempt, then local with the failure as feedback, then a premium plan
        (only within the supervisor budget). A repeated identical failure skips straight to premium."""
        wanted = STRATEGIES[min(number, len(STRATEGIES)) - 1]
        if repeated and wanted == "local_with_feedback":
            wanted = "premium_plan"
        if wanted == "premium_plan" and not self._premium:
            return None if repeated or number > len(STRATEGIES) else "local_with_feedback"
        return wanted

    async def _dispatch(self, actions):
        running = [attempt for attempt in self.backlog.open_attempts()
                   if self.backlog.get(attempt["item_id"])["state"] in {"running", "verifying"}]
        free = {"local": self.resources.local_slots() - sum(1 for a in running if a["strategy"] != "premium_plan"),
                "premium": (1 if self._premium else 0) - sum(1 for a in running if a["strategy"] == "premium_plan")}
        for item in self.backlog.list({"ready"}):
            item = self.backlog.get(item["id"])
            strategy = self._strategy(item["attempts"] + 1, bool(item["last_signature"]) and item["attempts"] > 0
                                      and self._last_repeated(item))
            if strategy is None:
                self._block(item, "no further strategy within the configured budgets", actions)
                continue
            kind = "premium" if strategy == "premium_plan" else "local"
            if free[kind] <= 0:
                continue
            item["base_commit"] = item.get("base_commit") or self.backlog.accepted_head()
            task_id = await self.executor.start(item, strategy, item["feedback"] or "")
            if item.get("base_commit"):
                self.backlog.update(item["id"], base_commit=None)  # used once: later work starts from the latest
            attempt_id = self.backlog.start_attempt(item, strategy, kind, task_id, started=self.clock())
            self.progress[attempt_id] = self.clock()
            self.backlog.decide(item["id"], "assigned", f"attempt {item['attempts'] + 1}: {strategy} agent, task {task_id}")
            free[kind] -= 1
            actions["started"].append(item["id"])

    def _last_repeated(self, item) -> bool:
        ended = [attempt for attempt in self.backlog.attempts(item["id"]) if attempt["ended"]]
        return len(ended) >= 2 and ended[-1]["signature"] and ended[-1]["signature"] == ended[-2]["signature"]

    async def cancel(self, item_id: str) -> dict:
        """The user withdraws an item: its running task is stopped and nothing depends on it any more."""
        item = self.backlog.get(item_id)
        for attempt in self.backlog.open_attempts():
            if attempt["item_id"] == item["id"]:
                await self.executor.stop(attempt["task_id"])
                self.backlog.end_attempt(attempt["id"], "cancelled")
        self.backlog.update(item["id"], state="cancelled", reason="cancelled by the user")
        self.backlog.decide(item["id"], "cancelled", "by the user")
        return self.backlog.get(item["id"])

    # the loop ---------------------------------------------------------------------------------
    async def run(self, poll: float = 5.0, max_ticks: int | None = None, deadline: float | None = None) -> dict:
        """Tick until every item is settled, or only people can move it on (approval, acceptance),
        or the tick/time limit is reached. Returns the status."""
        ticks = 0
        while True:
            await self.tick()
            ticks += 1
            status = self.status()
            if status["finished"] or status["waiting_on_people_only"]:
                return status
            if (max_ticks and ticks >= max_ticks) or (deadline and self.clock() >= deadline):
                return {**status, "stopped": "limit reached"}
            await asyncio.sleep(poll)

    def status(self) -> dict:
        return backlog_status(self.backlog, self.resources.local_slots())


class BrainExecutor:
    """Runs backlog attempts as ordinary Brain tasks: an isolated worktree each, the normal
    proposal/repair loop and the configured supervisor budgets. Verification is independent of
    the agent: the item's frozen acceptance files are restored over a copy of the workspace and
    run in the offline sandbox."""

    def __init__(self, brain, repository: str):
        self.brain, self.repository = brain, repository

    async def start(self, item: dict, strategy: str, feedback: str) -> str:
        goal = item["goal"]
        criteria = (item.get("acceptance") or {}).get("criteria")
        if criteria:
            goal += "\n\nAcceptance criteria (checked independently after you finish):\n" + criteria
        if feedback and strategy != "local":
            goal += "\n\n" + feedback[-3000:]
        base = item.get("base_commit")
        if base and subprocess.run(["git", "-C", str(self.brain.repository(self.repository)), "merge-base",
                                    "--is-ancestor", base, "HEAD"], capture_output=True, timeout=60).returncode == 0:
            base = None  # the project's branch already contains it: start from the newest work
        task = self.brain.submit(self.repository, goal, base_commit=base, premium_plan=strategy == "premium_plan")
        return task["id"]

    async def poll(self, task_id: str) -> dict:
        task = self.brain.store.get(task_id)
        with self.brain.telemetry.connect() as db:  # newest journal event: model heartbeats, stages, tests
            progress = db.execute("SELECT MAX(at) FROM journal WHERE task_id=?", (task_id,)).fetchone()[0]
        failures = task.get("failure_log") or []
        failure = failures[-1].get("summary") if failures and isinstance(failures[-1], dict) else None
        return {"status": task["status"], "digest": task.get("digest"), "progress_at": progress,
                "failure": failure or task.get("error") or task["status"]}

    async def approve(self, task_id: str, digest: str):
        self.brain.schedule(task_id, "execute_task", {"digest": digest})

    async def stop(self, task_id: str):
        try:
            self.brain.cancel(task_id)
        except ValueError:
            pass

    async def verify(self, task_id: str, item: dict) -> dict:
        from . import sandbox
        workspace = self.brain.workspace(task_id)
        if not workspace.exists():
            return {"passed": False, "exit_code": None, "output": "the task workspace no longer exists", "command": None}
        scratch = Path(tempfile.mkdtemp(prefix="cb-verify-"))
        try:
            copy = scratch / "workspace"
            shutil.copytree(workspace, copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            refused = self._prepare(copy, lambda relative: self._original(task_id, workspace, relative),
                                    item["acceptance"]["files"])
            if refused:
                return {"passed": False, "exit_code": None, "command": None, "output": refused}
            result = await asyncio.to_thread(sandbox.run_tests, copy, self.brain.image, None, item["acceptance"]["nodes"])
            return {"passed": acceptance_passed(result, item["acceptance"]["nodes"]), "exit_code": result.get("exit_code"),
                    "output": result.get("output", ""), "command": "sandbox pytest " + " ".join(item["acceptance"]["nodes"])}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _prepare(copy: Path, original, files: dict) -> str | None:
        """Make a copy of a workspace safe to verify: the project's test configuration as it was
        at the baseline (`original(relative)` gives that content, or None), no file that would
        replace the test runner, and the frozen acceptance files. Returns why it was refused."""
        found = [Path(folder) / name for folder, _, names in os.walk(copy) for name in names if name in TEST_CONFIG]
        for path in found:  # os.walk never descends through a linked folder
            content = original(path.relative_to(copy).as_posix())
            path.unlink()
            if content is not None:
                path.write_bytes(content)
        shadows = sorted(entry.name for entry in copy.iterdir() if entry.name in RUNNER_SHADOWS or
                         entry.name.endswith(".pth"))
        added = [name for name in shadows if original(name) is None]
        if added:
            return f"the change adds {', '.join(added)}, which would replace the test runner"
        for relative, content in files.items():  # frozen: the agent's edits are replaced
            folder = copy
            for part in PurePosixPath(relative).parts[:-1]:  # a linked folder must not redirect the write
                folder = folder / part
                if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                    folder.unlink()
                folder.mkdir(exist_ok=True)
            target = folder / PurePosixPath(relative).name
            if target.is_symlink() or target.is_dir():
                shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
            target.write_text(content, encoding="utf-8")
        return None

    # integration --------------------------------------------------------------------------------
    async def integrate(self, items: list[dict], base: str | None) -> dict:
        return await asyncio.to_thread(self._integrate, items, base)

    def _integrate(self, items: list[dict], base: str | None) -> dict:
        """Apply each item's change in turn on a private worktree at `base` (the project's HEAD
        when None), keeping it only if every included item's frozen acceptance tests and the
        project's own tests still pass together. The user's branches are not touched."""
        manager = self.brain.manager
        source = self.brain.repository(self.repository)
        base = base or manager._git(source, "rev-parse", "HEAD")
        record_id = uuid.uuid4().hex[:12]
        workspace = Path(self.brain.data) / "backlog-integrations" / record_id
        manager.integration(source, workspace, base)
        included, excluded, head, checks = [], {}, base, None
        for item in items:
            files = {}
            for other in [*included, item]:
                for relative, content in other["acceptance"]["files"].items():
                    if files.setdefault(relative, content) != content:
                        excluded[item["id"]] = {"reason": "integration_conflict",
                                                "detail": f"its acceptance file {relative} differs from another item's"}
            if item["id"] in excluded:
                continue
            try:
                commit = self._commit_of(item)
                new_head = manager.integrate(workspace, commit)
            except ValueError as error:
                excluded[item["id"]] = {"reason": "integration_conflict", "detail": str(error)[-2000:]}
                continue
            result = self._combined(workspace, source, base, [*included, item], files)
            if not result["passed"]:
                manager._git(workspace, "reset", "--hard", head)
                excluded[item["id"]] = {"reason": "integration_failed", "detail": result["output"][-2500:],
                                        "exit_code": result.get("exit_code")}
                continue
            included.append(item)
            head, checks = new_head, result
        if included:  # pinned: removing the worktree later cannot let Git collect the candidate
            manager.keep(source, f"refs/codingbrain/backlog/{record_id}", head)
        return {"id": record_id, "base": base, "head": head, "workspace": str(workspace),
                "included": [item["id"] for item in included], "excluded": excluded,
                "checks": {key: (checks or {}).get(key) for key in ("command", "exit_code")}}

    def _commit_of(self, item: dict) -> str:
        """The task's change as a commit on its base, made without touching the task's worktree
        or index (a private index file)."""
        task = self.brain.store.get(item["task_id"])
        if task.get("commit"):
            return task["commit"]
        workspace = self.brain.workspace(item["task_id"])
        scratch = Path(tempfile.mkdtemp(prefix="cb-index-"))
        try:
            env = {**os.environ, "GIT_INDEX_FILE": str(scratch / "index")}

            def git(*arguments):
                result = subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Coding Brain",
                                         "-c", "user.email=coding-brain@localhost", *arguments],
                                        capture_output=True, text=True, timeout=120, env=env)
                if result.returncode:
                    raise ValueError(result.stderr.strip() or "Git operation failed")
                return result.stdout.strip()
            git("read-tree", "HEAD")
            git("add", "--all", "--", ".", ":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc")
            tree = git("write-tree")
            if tree == git("rev-parse", "HEAD^{tree}"):
                raise ValueError("the task changed nothing")
            return git("commit-tree", tree, "-p", "HEAD", "-m", f"backlog {item['id']}: {item['title'][:120]}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _combined(self, workspace: Path, source: Path, base: str, items: list[dict], files: dict) -> dict:
        from . import sandbox
        scratch = Path(tempfile.mkdtemp(prefix="cb-integrate-"))
        try:
            copy = scratch / "workspace"
            shutil.copytree(workspace, copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))

            def original(relative):
                shown = subprocess.run(["git", "-C", str(source), "show", f"{base}:{relative}"], capture_output=True,
                                       timeout=60)
                return shown.stdout if shown.returncode == 0 else None
            refused = self._prepare(copy, original, files)
            if refused:
                return {"passed": False, "exit_code": None, "output": refused, "command": None}
            nodes = [node for item in items for node in item["acceptance"]["nodes"]]
            together = sandbox.run_tests(copy, self.brain.image, None, nodes)
            if not acceptance_passed(together, nodes):
                return {"passed": False, "exit_code": together.get("exit_code"), "command": "sandbox pytest " + " ".join(nodes),
                        "output": "acceptance tests failed together:\n" + str(together.get("output", ""))}
            suite = sandbox.run_tests(copy, self.brain.image, None, None)
            if suite.get("exit_code") not in (0, 5):  # 5: the project has no tests of its own
                return {"passed": False, "exit_code": suite.get("exit_code"), "command": "sandbox (project tests)",
                        "output": "the project's own tests failed:\n" + str(suite.get("output", ""))}
            return {"passed": True, "exit_code": 0, "output": str(together.get("output", ""))[-1500:],
                    "command": "sandbox pytest " + " ".join(nodes) + " and the project's tests"}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _original(self, task_id: str, workspace: Path, relative: str) -> bytes | None:
        """The file's content at the task's baseline, or None if it did not exist (or was not a
        regular file)."""
        base = self.brain.store.get(task_id).get("base_commit")
        if base:
            shown = subprocess.run(["git", "-C", str(workspace), "show", f"{base}:{relative}"],
                                   capture_output=True, timeout=60)
            return shown.stdout if shown.returncode == 0 else None
        original = workspace.parent / "baseline" / relative
        return original.read_bytes() if original.is_file() and not original.is_symlink() else None

    async def premium_available(self) -> bool:
        policy = getattr(self.brain, "supervision", None)
        if not policy or not policy.supervisors or policy.plan_budget <= 0:
            return False
        return policy.ledger.count(since=time.time() - 86400) < policy.daily_limit
