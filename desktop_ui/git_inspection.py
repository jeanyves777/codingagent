"""Local read-only Git telemetry for the selected desktop project.

No repository URLs are contacted and no Git writes, index locks, fetches, switches,
resets or commits are performed. The UI polls this bounded snapshot for live updates.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

MAX_COMMITS = 20
MAX_BRANCHES = 25
MAX_CHANGES = 75
GIT_TIMEOUT = 5


def _git(project: Path, *args: str, allow_failure: bool = False) -> str:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(project), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, env=env, timeout=GIT_TIMEOUT, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        raise ValueError("Git is not available or did not respond") from error
    if result.returncode and not allow_failure:
        raise ValueError("Git could not inspect the selected project")
    return result.stdout if result.returncode == 0 else ""


def _changes(raw: str) -> tuple[list[dict], dict]:
    """Parse the porcelain -z format, including the extra source path for renames."""
    parts = raw.split("\0")
    entries = []
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicted": 0}
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        if len(item) < 4 or item[2] != " ":
            continue
        code = item[:2]
        path = item[3:]
        if "R" in code or "C" in code:
            # Porcelain -z puts the original path in the next NUL-delimited entry.
            if index < len(parts):
                index += 1
        conflict = "U" in code or code in {"AA", "DD"}
        staged = code[0] not in {" ", "?"}
        unstaged = code[1] not in {" ", "?"}
        untracked = code == "??"
        if conflict:
            counts["conflicted"] += 1
        if staged:
            counts["staged"] += 1
        if unstaged:
            counts["modified"] += 1
        if untracked:
            counts["untracked"] += 1
        if len(entries) < MAX_CHANGES:
            entries.append({"path": path[:420], "status": code, "staged": staged,
                            "untracked": untracked, "conflicted": conflict})
    return entries, counts


def git_snapshot(project: Path | None) -> dict:
    """Give the desktop a fresh read-only Git snapshot for the selected folder."""
    if project is None:
        return {"available": False, "reason": "Choose a project to see Git branches and commit history"}
    project = Path(project).resolve(strict=True)
    if not project.is_dir():
        raise ValueError("Selected project is not a directory")
    # Never follow a browser-supplied repository/path argument; only use Workspace.project.
    top = _git(project, "rev-parse", "--show-toplevel", allow_failure=True).strip()
    if not top:
        return {"available": False, "reason": "This folder is not a Git repository"}
    head = _git(project, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True).strip()
    commit_sha = _git(project, "rev-parse", "--verify", "HEAD", allow_failure=True).strip()
    detached = not head and bool(commit_sha)
    branch = head or ("Detached HEAD" if detached else "Unborn branch")
    # Different refreshes may observe different revisions during a commit/checkout:
    # the UI applies each complete snapshot atomically and polls again within 3 seconds.
    raw_status = _git(project, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    changes, counts = _changes(raw_status)
    branches_raw = _git(project, "branch", "--list", "--format=%(refname:short)", allow_failure=True)
    branches = list(dict.fromkeys(x.strip()[:180] for x in branches_raw.splitlines() if x.strip()))[:MAX_BRANCHES]
    commits = []
    if commit_sha:
        raw_log = _git(project, "log", "-n", str(MAX_COMMITS),
                       "--format=%H%x1f%h%x1f%s%x1f%an%x1f%aI%x1e")
        for record in raw_log.split("\x1e"):
            record = record.strip("\r\n")
            if not record:
                continue
            fields = record.split("\x1f")
            if len(fields) != 5:
                continue
            full, short, subject, author, timestamp = fields
            commits.append({"sha": full[:40], "short": short[:16], "subject": subject[:300],
                            "author": author[:110], "date": timestamp[:40]})
    return {"available": True, "branch": branch, "detached": detached,
            "head": commit_sha[:40] if commit_sha else None,
            "branches": branches, "commits": commits, "changes": changes,
            "counts": counts, "change_count_shown": len(changes),
            "changes_truncated": sum(counts.values()) > MAX_CHANGES,
            "unborn": not bool(commit_sha)}
