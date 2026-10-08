"""Durable event stream, consumer cursors, and cross-worker traces."""
import contextvars
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


_current_span = contextvars.ContextVar("coding_brain_span", default=None)


class Telemetry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS events ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, trace_id TEXT NOT NULL, "
                       "kind TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS consumers ("
                       "name TEXT PRIMARY KEY, event_id INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS spans ("
                       "id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT, task_id TEXT, "
                       "name TEXT NOT NULL, status TEXT NOT NULL, attributes TEXT NOT NULL, "
                       "started_at REAL NOT NULL, ended_at REAL, error TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS events_trace ON events(trace_id,id)")
            db.execute("CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id,started_at)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def publish(self, task_id: str, trace_id: str, kind: str, detail: str) -> int:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO events(task_id,trace_id,kind,detail,created_at) "
                                "VALUES (?,?,?,?,?)", (task_id, trace_id, kind, detail, time.time()))
            return cursor.lastrowid

    @staticmethod
    def _event(row) -> dict:
        keys = ("id", "task_id", "trace_id", "kind", "detail", "created_at")
        return dict(zip(keys, row))

    def events(self, after=0, limit=100, task_id=None, kind=None) -> list[dict]:
        clauses, values = ["id>?"], [max(0, after)]
        if task_id:
            clauses.append("task_id=?")
            values.append(task_id)
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        values.append(max(1, min(500, limit)))
        with self.connect() as db:
            rows = db.execute("SELECT id,task_id,trace_id,kind,detail,created_at FROM events WHERE " +
                              " AND ".join(clauses) + " ORDER BY id LIMIT ?", values).fetchall()
        return [self._event(row) for row in rows]

    def pending(self, consumer: str, limit=100) -> list[dict]:
        with self.connect() as db:
            row = db.execute("SELECT event_id FROM consumers WHERE name=?", (consumer,)).fetchone()
        return self.events(after=row[0] if row else 0, limit=limit)

    def acknowledge(self, consumer: str, event_id: int):
        with self.connect() as db:
            db.execute("INSERT INTO consumers VALUES (?,?) ON CONFLICT(name) DO UPDATE SET "
                       "event_id=MAX(event_id,excluded.event_id)", (consumer, event_id))

    @contextmanager
    def span(self, trace_id: str, name: str, task_id=None, attributes=None):
        span_id, started = uuid.uuid4().hex, time.time()
        parent = _current_span.get()
        with self.connect() as db:
            db.execute("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (span_id, trace_id, parent, task_id, name, "running",
                        json.dumps(attributes or {}), started, None, None))
        token = _current_span.set(span_id)
        try:
            yield span_id
        except Exception as error:
            with self.connect() as db:
                db.execute("UPDATE spans SET status='error',ended_at=?,error=? WHERE id=?",
                           (time.time(), str(error)[:1000], span_id))
            raise
        else:
            with self.connect() as db:
                db.execute("UPDATE spans SET status='ok',ended_at=? WHERE id=?", (time.time(), span_id))
        finally:
            _current_span.reset(token)

    def trace(self, trace_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT id,parent_id,task_id,name,status,attributes,started_at,ended_at,error "
                              "FROM spans WHERE trace_id=? ORDER BY started_at", (trace_id,)).fetchall()
        keys = ("id", "parent_id", "task_id", "name", "status", "attributes",
                "started_at", "ended_at", "error")
        spans = []
        for row in rows:
            item = dict(zip(keys, row))
            item["attributes"] = json.loads(item["attributes"])
            spans.append(item)
        return {"trace_id": trace_id, "spans": spans, "events": self._trace_events(trace_id)}

    def _trace_events(self, trace_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,task_id,trace_id,kind,detail,created_at FROM events "
                              "WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall()
        return [self._event(row) for row in rows]
