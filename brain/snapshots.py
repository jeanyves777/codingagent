"""Task snapshots: what a task's workspace and state looked like at each boundary.

Stored outside the user's project in <data>/snapshots: a content-addressed object store (files,
diffs and screenshots by SHA-256, so unchanged content is stored once) and a small SQLite index.
A snapshot records the task's state and identifiers, the Git baseline, the changed-file
manifest relative to that baseline, the diff, plan, requirements, completed and pending stages,
test and visual evidence, errors and retries, model usage and what is needed to resume.
Restoring never touches the user's working tree: it creates a new branch from the snapshot.

What is stored (and what is not): a changed file's content is stored only if it is a regular file
inside the task workspace, reached without any symbolic link, junction or other reparse point, its
name does not look like a secret or credential, and its text contains nothing secret-like. Anything
else is listed in the manifest with `excluded` (the reason) and no content: it is never opened past
the checks, and a restore leaves it at its baseline version instead of writing a redacted copy. The
diff is built only from stored files and redacted. Artifacts are stored only if they are image
files. A `--sensitive` task keeps the changed paths and nothing else. `codingbrain snapshot purge`
deletes snapshots and every object no remaining snapshot references.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .attachments import SECRET_NAME
from .local.memory import NEVER_READ
from .redaction import PATTERNS, redact, redact_value

MAX_OBJECT = 10 * 1024 * 1024
REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# A file body is not stored when it holds a credential: any known key format, or a password,
# token, secret or API key assigned a literal (quoted in code; any value in configuration files).
# Ordinary code such as `token = request.headers.get(...)` is still stored. The diff is always redacted.
KEY_FORMATS = [pattern for pattern in PATTERNS if "password" not in pattern.pattern]
QUOTED_SECRET = re.compile(r"""(?i)\b(password|passwd|secret|token|api[_-]?key|client[_-]?secret)\b["']?\s*[:=]\s*"""
                           r"""["'][^"'\s]{8,}["']""")
CONFIG_SECRET = re.compile(r"(?im)^\s*[\w.-]*(password|passwd|secret|token|api[_-]?key)[\w.-]*[\"']?\s*[:=]\s*\S{8,}")
CONFIG_SUFFIXES = {".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".properties", ".json", ".env", ".config"}
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")

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
        manifest, diff_ref, stored_artifacts = [], None, []
        sensitive = bool(task.get("attachments_sensitive"))
        if workspace and workspace.is_dir():
            manifest, before = changed_files(workspace, task.get("base_commit"))
            diff = []
            for item in manifest:
                if sensitive:
                    item["excluded"] = "sensitive task: content is not kept"
                    continue
                old, old_reason = before(item["path"]) if item["change"] != "added" else (b"", None)
                new, reason = (b"", None) if item["change"] == "deleted" else read_contained(workspace, item["path"])
                reason = reason or old_reason
                if reason:
                    item["excluded"] = reason
                    continue
                if item["change"] != "deleted":
                    item["sha256"] = self.put(new)
                diff.append(unified_diff(item["path"], old, new))
            diff_text = redact("".join(diff))
            if diff_text:
                diff_ref = self.put(diff_text.encode("utf-8"))
        for artifact in artifacts or []:
            path = Path(artifact["path"])
            entry = {**{key: value for key, value in artifact.items() if key != "path"}, "name": path.name}
            data, reason = (None, "sensitive task: content is not kept") if sensitive else \
                read_contained(path.parent, path.name, check_content=False)
            if data is not None and not data.startswith(IMAGE_MAGIC) and not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
                data, reason = None, "not an image file"
            stored_artifacts.append({**entry, "sha256": self.put(data)} if data is not None else {**entry, "excluded": reason})
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
                if item.get("excluded"):
                    continue  # never stored: left at its baseline version (listed by the caller)
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


def secret_path(relative: str) -> bool:
    return bool(SECRET_NAME.search(relative) or NEVER_READ.search(relative))


def _linked(info) -> bool:
    """A symbolic link, or on Windows any reparse point (junctions included), from lstat; this does
    not depend on Path.is_junction(), which Python 3.11 lacks."""
    return stat_module.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def read_contained(root: Path, relative: str, limit: int = MAX_OBJECT, check_content: bool = True):
    """(content, None) for a regular file inside `root` reached without following any link, or
    (None, reason). Every component is checked with lstat before anything is opened; the final
    component is opened without following links where the platform allows, and the opened file
    must be the one that was checked. Never truncates: an oversized file is refused, not cut."""
    relative_path = Path(relative)
    parts = relative_path.parts
    if not parts or relative_path.is_absolute() or relative_path.drive or ".." in parts:
        return None, "outside the workspace"
    if secret_path(relative_path.as_posix()):
        return None, "secret-like file name"
    try:
        if _linked(os.lstat(root)):
            return None, "symbolic link or junction"
        current = Path(root)
        for part in parts:
            current = current / part
            info = os.lstat(current)
            if _linked(info):
                return None, "symbolic link or junction"
    except OSError:
        return None, "unreadable"
    if not stat_module.S_ISREG(info.st_mode):
        return None, "not a regular file"
    if info.st_size > limit:
        return None, "too large"
    try:
        descriptor = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None, "unreadable"
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None, "changed while being read"
        data = handle.read(limit + 1)
    if len(data) > limit:
        return None, "too large"
    if check_content and secret_content(relative_path.as_posix(), data):
        return None, "contains secret-like content"
    return data, None


def secret_content(relative: str, data: bytes) -> bool:
    text = data.decode("utf-8", errors="replace")
    if any(pattern.search(text) for pattern in KEY_FORMATS) or QUOTED_SECRET.search(text):
        return True
    return Path(relative).suffix.lower() in CONFIG_SUFFIXES and bool(CONFIG_SECRET.search(text))


def unified_diff(path: str, old: bytes, new: bytes) -> str:
    try:
        before, after = old.decode("utf-8"), new.decode("utf-8")
    except UnicodeDecodeError:
        return f"Binary files a/{path} and b/{path} differ\n" if old != new else ""
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}"))


def _git_bytes(path: Path, *arguments) -> bytes | None:
    result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, timeout=60)
    return result.stdout if result.returncode == 0 else None


def _walk_regular(root: Path) -> set[str]:
    """Relative paths of the files under root, never descending through a link or junction
    (os.walk alone follows junctions on Python 3.11). Links themselves are listed so that they
    show up, as excluded, in the manifest."""
    found = set()
    for folder, directories, files in os.walk(root):
        kept = []
        for name in directories:
            try:
                linked = _linked(os.lstat(Path(folder) / name))
            except OSError:
                continue
            if linked:
                files.append(name)
            else:
                kept.append(name)
        directories[:] = kept
        found |= {(Path(folder) / name).relative_to(root).as_posix() for name in files}
    return found


def changed_files(workspace: Path, base: str | None):
    """Paths that differ from the baseline (Git) or from the baseline copy (snapshot workspaces),
    and a function giving each path's baseline content as (bytes, None) or (None, reason).
    Discovery opens no file; content is read only through read_contained."""
    if (workspace / ".git").exists() and base:
        entries = {}
        committed = (_git_bytes(workspace, "diff", "--name-status", "-z", base, "HEAD") or b"").decode(
            "utf-8", errors="replace").split("\0")
        index = 0
        while index < len(committed) - 1:
            code = committed[index]
            if code[:1] in ("R", "C"):
                if code[:1] == "R":
                    entries[committed[index + 1]] = "deleted"
                entries[committed[index + 2]] = "added"
                index += 3
            else:
                entries[committed[index + 1]] = {"D": "deleted", "A": "added"}.get(code[:1], "modified")
                index += 2
        status = (_git_bytes(workspace, "status", "--porcelain=v1", "-z", "-uall") or b"").decode(
            "utf-8", errors="replace").split("\0")
        index = 0
        while index < len(status):
            line = status[index]
            index += 1
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:]
            if code[0] in ("R", "C"):
                if code[0] == "R":
                    entries[status[index]] = "deleted"
                index += 1
                entries[path] = "added"
            else:
                entries[path] = "added" if code == "??" or "A" in code else "deleted" if "D" in code else "modified"

        def before(path: str):
            if secret_path(path):
                return None, "secret-like file name"
            data = _git_bytes(workspace, "show", f"{base}:{path}")
            if data is None:
                return b"", None
            if len(data) > MAX_OBJECT:
                return None, "too large"
            return (None, "contains secret-like content") if secret_content(path, data) else (data, None)
        return [{"path": path, "change": change} for path, change in sorted(entries.items())], before
    baseline = workspace.parent / "baseline"
    if not baseline.is_dir():
        return [], lambda path: (b"", None)
    current, original = _walk_regular(workspace), _walk_regular(baseline)
    manifest = []
    for path in sorted(current | original):
        if path not in original:
            change = "added"
        elif path not in current:
            change = "deleted"
        else:
            new, _ = read_contained(workspace, path, check_content=False)
            old, _ = read_contained(baseline, path, check_content=False)
            if new is not None and new == old:
                continue
            change = "modified"
        manifest.append({"path": path, "change": change})
    return manifest, lambda path: read_contained(baseline, path)
