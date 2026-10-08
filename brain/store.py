import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS memories (task_id TEXT PRIMARY KEY, content TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS repository_indexes (repository TEXT PRIMARY KEY, content TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def save(self, task: dict):
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (task["id"], json.dumps(task)))

    def get(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row[0])

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM tasks ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def memories(self, repository: str, goal: str = "") -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT task_id, content FROM memories ORDER BY rowid DESC LIMIT 200").fetchall()
        candidates = [dict(task_id=task_id, **json.loads(content)) for task_id, content in rows]
        words = set(goal.lower().split())
        candidates = [item for item in candidates if item["repository"] == repository]
        candidates.sort(key=lambda item: len(words & set(item["content"].lower().split())), reverse=True)
        return candidates[:5]

    def remember(self, task_id: str, content: dict):
        with self.connect() as db:
            db.execute("INSERT INTO memories VALUES (?, ?) ON CONFLICT(task_id) DO UPDATE SET content=excluded.content",
                       (task_id, json.dumps(content)))

    def memory(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT content FROM memories WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return {"task_id": task_id, **json.loads(row[0])}

    def save_index(self, repository: str, index: dict):
        with self.connect() as db:
            db.execute("INSERT INTO repository_indexes VALUES (?, ?) "
                       "ON CONFLICT(repository) DO UPDATE SET content=excluded.content",
                       (repository, json.dumps(index)))

    def index(self, repository: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT content FROM repository_indexes WHERE repository=?",
                             (repository,)).fetchone()
        return json.loads(row[0]) if row else {"symbols": [], "dependencies": [], "parse_errors": []}
