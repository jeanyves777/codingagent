"""Phase 2 supervision policy: when premium supervisors are consulted, and how often.

Premium calls are rare and bounded. Each task has a small budget per kind of consultation, a daily
cap applies across all tasks, and every call is recorded. A human can grant one more consultation
to a specific task. An unavailable free model never triggers escalation.
"""
import sqlite3
import time
from pathlib import Path
from .subscriptions import SubscriptionError

KINDS = ("plan", "diagnose", "review", "decompose")


class SupervisorBudgetExceeded(RuntimeError):
    pass


class SupervisorLedger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, task_id TEXT, "
                       "supervisor TEXT NOT NULL, kind TEXT NOT NULL, started REAL NOT NULL, "
                       "seconds REAL, ok INTEGER NOT NULL, detail TEXT)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, task_id, supervisor, kind, started, ok, detail=""):
        with self.connect() as db:
            db.execute("INSERT INTO calls (task_id, supervisor, kind, started, seconds, ok, detail) "
                       "VALUES (?,?,?,?,?,?,?)", (task_id, supervisor, kind, started,
                                                   round(time.time() - started, 1), int(ok), detail[:500]))

    def count(self, task_id=None, kind=None, since=None, ok=None) -> int:
        query, values = "SELECT COUNT(*) FROM calls WHERE 1=1", []
        for column, value in (("task_id", task_id), ("kind", kind), ("ok", ok)):
            if value is not None:
                query += f" AND {column}=?"
                values.append(value)
        if since is not None:
            query += " AND started>=?"
            values.append(since)
        with self.connect() as db:
            return db.execute(query, values).fetchone()[0]

    def calls(self, task_id=None, limit=100) -> list[dict]:
        query = "SELECT task_id, supervisor, kind, started, seconds, ok, detail FROM calls"
        values = []
        if task_id:
            query += " WHERE task_id=?"
            values.append(task_id)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY id DESC LIMIT ?", values + [limit]).fetchall()
        keys = ("task_id", "supervisor", "kind", "started", "seconds", "ok", "detail")
        return [dict(zip(keys, row)) for row in rows]


class SupervisionPolicy:
    DEFAULTS = {"escalate_after": 2, "plan_budget": 1, "diagnose_budget": 1, "review_budget": 0,
                "decompose_budget": 1, "daily_limit": 20, "plan_complex_tasks": True,
                "plan_orchestrations": True, "final_review": False, "takeover": False}

    def __init__(self, supervisors: list, ledger: SupervisorLedger, **settings):
        unknown = set(settings) - set(self.DEFAULTS)
        if unknown:
            raise ValueError("Unknown supervision setting: " + ", ".join(sorted(unknown)))
        self.supervisors, self.ledger = supervisors, ledger
        self.settings = {**self.DEFAULTS, **settings}

    def __getattr__(self, name):
        settings = self.__dict__.get("settings", {})
        if name in settings:
            return settings[name]
        raise AttributeError(name)

    def allowance(self, task: dict, kind: str) -> int:
        return self.settings[f"{kind}_budget"] + task.get("supervisor_grants", {}).get(kind, 0)

    def remaining(self, task: dict, kind: str) -> int:
        return self.allowance(task, kind) - self.ledger.count(task["id"], kind, ok=1)

    def report(self, task: dict | None = None) -> dict:
        today = time.time() - 86400
        result = {"supervisors": [getattr(item, "name", "unknown") for item in self.supervisors],
                  "settings": self.settings, "calls_last_24h": self.ledger.count(since=today)}
        if task:
            result["task_remaining"] = {kind: self.remaining(task, kind) for kind in KINDS}
        return result

    async def consult(self, task: dict, kind: str, payload: dict, workspace: Path | None = None) -> dict:
        if self.remaining(task, kind) <= 0:
            raise SupervisorBudgetExceeded(f"Supervisor {kind} budget for this task is used")
        if self.ledger.count(since=time.time() - 86400) >= self.settings["daily_limit"]:
            raise SupervisorBudgetExceeded("Daily supervisor limit reached")
        errors = []
        for supervisor in self.supervisors:
            started = time.time()
            try:
                result = await supervisor.ask(kind, payload, workspace)
            except (SubscriptionError, ValueError, OSError, TimeoutError) as error:
                self.ledger.record(task["id"], supervisor.name, kind, started, False, str(error))
                errors.append(f"{supervisor.name}: {error}"[:300])
                continue
            self.ledger.record(task["id"], supervisor.name, kind, started, True)
            return {"supervisor": supervisor.name, "kind": kind, "result": result}
        raise SubscriptionError("No supervisor could answer: " + " | ".join(errors))
