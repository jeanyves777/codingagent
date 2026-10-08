"""Persistent, fail-closed approvals for side-effecting model tools."""
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


class ApprovalRequired(Exception):
    def __init__(self, request: dict):
        self.request = request
        super().__init__(f"Tool approval required: {request['id']}")


class ToolApprovalStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS tool_approvals ("
                       "id TEXT PRIMARY KEY, task_id TEXT NOT NULL, capability TEXT NOT NULL, "
                       "arguments TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, "
                       "result TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS approvals_task ON tool_approvals(task_id, created_at)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    @staticmethod
    def _arguments(arguments: dict) -> str:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"))

    def request(self, task_id: str, capability: str, arguments: dict) -> dict:
        encoded = self._arguments(arguments)
        digest = hashlib.sha256(f"{capability}\n{encoded}".encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM tool_approvals WHERE task_id=? AND digest=? "
                "ORDER BY created_at DESC LIMIT 1", (task_id, digest)
            ).fetchone()
            if row:
                db.commit()
                return self.get(row[0])
            request_id = uuid.uuid4().hex
            db.execute("INSERT INTO tool_approvals VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (request_id, task_id, capability, encoded, digest, "pending",
                        None, None, now, now))
            db.commit()
        return self.get(request_id)

    def get(self, request_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT id,task_id,capability,arguments,digest,status,result,error,"
                             "created_at,updated_at FROM tool_approvals WHERE id=?",
                             (request_id,)).fetchone()
        if not row:
            raise KeyError(request_id)
        keys = ("id", "task_id", "capability", "arguments", "digest", "status",
                "result", "error", "created_at", "updated_at")
        item = dict(zip(keys, row))
        item["arguments"] = json.loads(item["arguments"])
        return item

    def list(self, task_id=None, status=None, limit=100) -> list[dict]:
        query, values = "SELECT id FROM tool_approvals", []
        clauses = []
        if task_id:
            clauses.append("task_id=?")
            values.append(task_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        values.append(max(1, min(500, limit)))
        with self.connect() as db:
            ids = [row[0] for row in db.execute(query, values).fetchall()]
        return [self.get(item) for item in ids]

    def decide(self, request_id: str, approved: bool) -> dict:
        status = "approved" if approved else "denied"
        with self.connect() as db:
            cursor = db.execute("UPDATE tool_approvals SET status=?,updated_at=? "
                                "WHERE id=? AND status='pending'",
                                (status, time.time(), request_id))
        if cursor.rowcount != 1:
            raise ValueError("Only pending tool requests can be decided")
        return self.get(request_id)

    def authorize(self, task_id: str, capability: str, arguments: dict) -> tuple[dict, str | None]:
        request = self.request(task_id, capability, arguments)
        if request["status"] == "pending":
            raise ApprovalRequired(request)
        if request["status"] == "completed":
            return request, request["result"]
        if request["status"] != "approved":
            raise PermissionError(f"Tool request is {request['status']}")
        with self.connect() as db:
            cursor = db.execute("UPDATE tool_approvals SET status='executing',updated_at=? "
                                "WHERE id=? AND status='approved'", (time.time(), request["id"]))
        if cursor.rowcount != 1:
            raise PermissionError("Tool approval was already consumed")
        return self.get(request["id"]), None

    def complete(self, request_id: str, result: str):
        with self.connect() as db:
            cursor = db.execute("UPDATE tool_approvals SET status='completed',result=?,updated_at=? "
                                "WHERE id=? AND status='executing'",
                                (result, time.time(), request_id))
        if cursor.rowcount != 1:
            raise ValueError("Tool request is not executing")

    def fail(self, request_id: str, error: str):
        with self.connect() as db:
            db.execute("UPDATE tool_approvals SET status='failed',error=?,updated_at=? "
                       "WHERE id=? AND status='executing'", (error[:1000], time.time(), request_id))
