"""Task snapshots: what a task's workspace and state looked like at each boundary.

Stored outside the user's project in <data>/snapshots: a content-addressed object store (files,
diffs and screenshots by SHA-256, so unchanged content is stored once) and a small SQLite index.
A snapshot records the task's state and identifiers, the Git baseline, the changed-file
manifest relative to that baseline, the diff, plan, requirements, completed and pending stages,
test and visual evidence, errors and retries, model usage and what is needed to resume.
Restoring never touches the user's working tree: it creates a new branch from the snapshot.
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .redaction import redact_value

BOUNDARIES = ("initial", "plan_approval", "before_changes", "after_proposal", "after_changes", "before_tests",
              "after_tests", "before_repair", "after_repair", "after_visual", "before_acceptance", "accepted")


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, task_id TEXT, parent_task_id TEXT, "
                       "label TEXT, at REAL, status TEXT, base_commit TEXT, body TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS snapshots_task ON snapshots(task_id, at)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "snapshots.sqlite3", timeout=30)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self.objects / digest[:2] / digest
        if not target.exists():
            target.parent.mkdir(exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        return digest

    def get(self, digest: str) -> bytes:
        if len(digest) != 64 or not all(character in "0123456789abcdef" for character in digest):
            raise ValueError("invalid object id")
        return (self.objects / digest[:2] / digest).read_bytes()

    def capture(self, task: dict, workspace: Path | None, label: str, artifacts: list | None = None) -> str:
        manifest, diff_ref = [], None
        if workspace and workspace.is_dir():
            manifest, diff_text = changed_files(workspace, task.get("base_commit"))
            for item in manifest:
                if item["change"] != "deleted":
                    item["sha256"] = self.put((workspace / item["path"]).read_bytes())
            if diff_text:
                diff_ref = self.put(diff_text.encode("utf-8"))
        stored_artifacts = []
        for artifact in artifacts or []:
            path = Path(artifact["path"])
            if path.is_file():
                stored_artifacts.append({**{key: value for key, value in artifact.items() if key != "path"},
                                         "name": path.name, "sha256": self.put(path.read_bytes())})
        body = redact_value({
            "task_id": task["id"], "parent_task_id": task.get("parent_id"), "kind": task.get("kind"),
            "goal": task.get("goal", "")[:2000], "status": task.get("status"), "label": label,
            "base_commit": task.get("base_commit"), "workspace_kind": task.get("workspace_kind"),
            "manifest": manifest, "diff": diff_ref, "plan": (task.get("proposal") or {}).get("plan", "")[:4000],
            "proposal_author": task.get("proposal_author"), "digest": task.get("digest"),
            "requirements": {key: (task.get("requirement_tests") or {}).get(key) for key in ("path", "tests")},
            "completion": task.get("completion"),
            "test_evidence": {key: (task.get("test_evidence") or {}).get(key) for key in ("passed", "exit_code", "profile")},
            "test_output": ((task.get("test_evidence") or {}).get("output") or "")[-4000:],
            "failures": task.get("failure_log", [])[-6:], "visual": {key: (task.get("visual_verification") or {}).get(key)
                                                                     for key in ("status", "statement", "blocking", "viewports")},
            "visual_repairs": task.get("visual_repairs", 0), "metrics": task.get("metrics", {}),
            "usage": task.get("usage"), "artifacts": stored_artifacts,
            "resume": {"status": task.get("status"), "digest": task.get("digest"),
                       "command": f"codingbrain resume {task['id'][:8]}"},
        })
        snapshot_id = "s" + uuid.uuid4().hex[:15]
        with self.connect() as db:
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
                       (snapshot_id, task["id"], task.get("parent_id"), label, time.time(), task.get("status"),
                        task.get("base_commit"), json.dumps(body)))
        return snapshot_id

    def list(self, task_id: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id, task_id, label, at, status FROM snapshots " +
                              ("WHERE task_id LIKE ? " if task_id else "") + "ORDER BY at",
                              ((task_id + "%",) if task_id else ())).fetchall()
        return [dict(zip(("id", "task_id", "label", "at", "status"), row)) for row in rows]

    def show(self, snapshot_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT id, at, body FROM snapshots WHERE id LIKE ?", (snapshot_id + "%",)).fetchall()
        if len(rows) != 1:
            raise KeyError(f"no single snapshot matches {snapshot_id!r}")
        return {"id": rows[0][0], "at": rows[0][1], **json.loads(rows[0][2])}

    def diff(self, first: str, second: str) -> dict:
        a, b = self.show(first), self.show(second)
        files_a = {item["path"]: item for item in a["manifest"]}
        files_b = {item["path"]: item for item in b["manifest"]}
        changed = []
        for path in sorted(set(files_a) | set(files_b)):
            before, after = files_a.get(path), files_b.get(path)
            if (before or {}).get("sha256") != (after or {}).get("sha256") or (before or {}).get("change") != (after or {}).get("change"):
                entry = {"path": path, "in_a": (before or {}).get("change"), "in_b": (after or {}).get("change")}
                if before and after and before.get("sha256") and after.get("sha256"):
                    import difflib
                    text_a = self.get(before["sha256"]).decode("utf-8", errors="replace").splitlines(True)
                    text_b = self.get(after["sha256"]).decode("utf-8", errors="replace").splitlines(True)
                    entry["diff"] = "".join(difflib.unified_diff(text_a, text_b, f"a/{path}", f"b/{path}"))[:20000]
                changed.append(entry)
        fields = {key: {"a": a.get(key), "b": b.get(key)} for key in ("status", "label", "test_evidence", "visual",
                                                                      "visual_repairs", "plan")
                  if a.get(key) != b.get(key)}
        return {"a": a["id"], "b": b["id"], "files": changed, "state": fields}

    def restore(self, snapshot_id: str, repository: Path, branch: str) -> str:
        """Create `branch` in the user's repository with the snapshot's files on top of its Git
        baseline. The working tree and the checked-out branch are untouched."""
        snapshot = self.show(snapshot_id)
        base = snapshot.get("base_commit")
        if not base:
            raise ValueError("this snapshot has no Git baseline to restore onto")
        if git(repository, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            raise ValueError(f"branch {branch} already exists")
        scratch = self.root / "restore" / uuid.uuid4().hex[:8]
        git(repository, "worktree", "add", "--detach", str(scratch), base)
        try:
            for item in snapshot["manifest"]:
                target = (scratch / item["path"]).resolve()
                if not target.is_relative_to(scratch.resolve()):
                    raise ValueError(f"unsafe path in snapshot: {item['path']}")
                if item["change"] == "deleted":
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(self.get(item["sha256"]))
            git(scratch, "add", "--all")
            git(scratch, "-c", "user.name=Coding Brain", "-c", "user.email=coding-brain@localhost", "commit",
                "--allow-empty", "-m", f"coding-brain: restore snapshot {snapshot['id']} ({snapshot['label']})")
            commit = git(scratch, "rev-parse", "HEAD").strip()
            git(repository, "branch", branch, commit)
            return commit
        finally:
            git(repository, "worktree", "remove", "--force", str(scratch), check=False)
            shutil.rmtree(scratch, ignore_errors=True)

    def purge(self, older_than: float | None = None, task_id: str | None = None) -> int:
        with self.connect() as db:
            if task_id:
                removed = db.execute("DELETE FROM snapshots WHERE task_id LIKE ?", (task_id + "%",)).rowcount
            else:
                removed = db.execute("DELETE FROM snapshots WHERE at < ?", (older_than or time.time(),)).rowcount
            bodies = [json.loads(row[0]) for row in db.execute("SELECT body FROM snapshots")]
        referenced = {item.get("sha256") for body in bodies for item in body["manifest"] + body["artifacts"]}
        referenced |= {body.get("diff") for body in bodies}
        for path in self.objects.glob("*/*"):
            if path.name not in referenced:
                path.unlink(missing_ok=True)
        return removed


def git(path: Path, *arguments, check=True) -> str:
    result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or f"git {arguments[0]} failed")
    return result.stdout if result.returncode == 0 else ""


def changed_files(workspace: Path, base: str | None) -> tuple[list[dict], str]:
    """Files that differ from the baseline (Git) or from the baseline copy (snapshot workspaces)."""
    if (workspace / ".git").exists() and base:
        status = git(workspace, "status", "--porcelain=v1", "-uall", check=False)
        committed = git(workspace, "diff", "--name-status", base, "HEAD", check=False) if base else ""
        entries = {}
        for line in committed.splitlines():
            code, _, path = line.partition("\t")
            entries[path] = {"D": "deleted", "A": "added"}.get(code[:1], "modified")
        for line in status.splitlines():
            code, path = line[:2], line[3:].strip().strip('"')
            entries[path] = "added" if code == "??" or "A" in code else "deleted" if "D" in code else "modified"
        diff = git(workspace, "diff", base, check=False)
        untracked = {line[3:].strip().strip('"') for line in status.splitlines() if line.startswith("??")}
        import difflib
        for path in sorted(untracked):  # new files are not in `git diff` until added
            if (workspace / path).is_file():
                diff += "".join(difflib.unified_diff([], (workspace / path).read_text(errors="replace").splitlines(True),
                                                     "/dev/null", f"b/{path}"))
        return [{"path": path, "change": change} for path, change in sorted(entries.items())], diff
    baseline = workspace.parent / "baseline"
    if not baseline.is_dir():
        return [], ""
    import difflib
    manifest, diff = [], []
    current = {str(path.relative_to(workspace)).replace("\\", "/") for path in workspace.rglob("*") if path.is_file()}
    original = {str(path.relative_to(baseline)).replace("\\", "/") for path in baseline.rglob("*") if path.is_file()}
    for path in sorted(current | original):
        if path not in original:
            change = "added"
        elif path not in current:
            change = "deleted"
        elif (workspace / path).read_bytes() != (baseline / path).read_bytes():
            change = "modified"
        else:
            continue
        manifest.append({"path": path, "change": change})
        before = (baseline / path).read_text(errors="replace").splitlines(True) if path in original else []
        after = (workspace / path).read_text(errors="replace").splitlines(True) if path in current else []
        diff.extend(difflib.unified_diff(before, after, f"a/{path}", f"b/{path}"))
    return manifest, "".join(diff)
