"""SQLite-backed job queue with atomic claims, leases, and heartbeats."""
import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path


class DurableQueue:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS jobs ("
                       "id TEXT PRIMARY KEY, task_id TEXT, action TEXT, payload TEXT, status TEXT, "
                       "dedupe_key TEXT, lease_owner TEXT, lease_until REAL, attempts INTEGER, "
                       "last_error TEXT, created_at REAL, updated_at REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def enqueue(self, task_id: str, action: str, payload: dict | None = None) -> str:
        if action not in {"create_task", "plan_group", "execute_task", "resume_task",
                          "advance_group"}:
            raise ValueError("Unknown durable job action")
        now, dedupe = time.time(), f"{task_id}:{action}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM jobs WHERE dedupe_key=? AND status IN ('queued','running')",
                             (dedupe,)).fetchone()
            if row:
                db.commit()
                return row[0]
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, task_id, action, json.dumps(payload or {}), "queued", dedupe,
                        None, None, 0, None, now, now))
            db.commit()
            return job_id

    def claim(self, owner: str, lease_seconds=120) -> dict | None:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='failed', last_error='worker lease expired', "
                       "lease_owner=NULL, lease_until=NULL, updated_at=? "
                       "WHERE status='running' AND lease_until < ?", (now, now))
            row = db.execute("SELECT id, task_id, action, payload, attempts FROM jobs "
                             "WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                db.commit()
                return None
            db.execute("UPDATE jobs SET status='running', lease_owner=?, lease_until=?, "
                       "attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
                       (owner, now + lease_seconds, now, row[0]))
            db.commit()
        return {"id": row[0], "task_id": row[1], "action": row[2],
                "payload": json.loads(row[3]), "attempts": row[4] + 1}

    def heartbeat(self, job_id: str, owner: str, lease_seconds=120) -> bool:
        now = time.time()
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET lease_until=?, updated_at=? "
                                "WHERE id=? AND status='running' AND lease_owner=?",
                                (now + lease_seconds, now, job_id, owner))
        return cursor.rowcount == 1

    def finish(self, job_id: str, owner: str, error: str | None = None):
        status = "failed" if error else "completed"
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET status=?, last_error=?, lease_owner=NULL, "
                                "lease_until=NULL, updated_at=? WHERE id=? AND status='running' "
                                "AND lease_owner=?", (status, error[:1000] if error else None,
                                time.time(), job_id, owner))
        if cursor.rowcount != 1:
            raise ValueError("Job lease is no longer owned by this worker")

    def cancel_task(self, task_id: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled', updated_at=? "
                       "WHERE task_id=? AND status='queued'", (time.time(), task_id))

    def jobs(self, limit=100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,task_id,action,status,attempts,last_error,lease_owner,lease_until "
                              "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ("id", "task_id", "action", "status", "attempts", "last_error",
                "lease_owner", "lease_until")
        return [dict(zip(keys, row)) for row in rows]


class QueueWorker:
    def __init__(self, queue: DurableQueue, handler, owner=None, lease_seconds=120):
        self.queue, self.handler = queue, handler
        self.owner = owner or ("worker-" + uuid.uuid4().hex)
        self.lease_seconds = max(30, lease_seconds)

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.queue.claim, self.owner, self.lease_seconds)
        if not job:
            return False
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.lease_seconds / 3)
                except TimeoutError:
                    valid = await asyncio.to_thread(self.queue.heartbeat, job["id"], self.owner,
                                                    self.lease_seconds)
                    if not valid:
                        return
        pulse = asyncio.create_task(heartbeat())
        error = None
        try:
            await self.handler(job)
        except Exception as exception:
            error = str(exception)
        finally:
            stop.set()
            await pulse
        await asyncio.to_thread(self.queue.finish, job["id"], self.owner, error)
        return True
