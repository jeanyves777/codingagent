"""Session locks: an update never replaces application code while a coding session is active."""
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager

from .paths import Layout

RUNNING = {"planning", "running", "testing", "queued", "cancellation_requested"}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def session(layout: Layout, project: str, purpose: str):
    """Mark a coding session as active for the duration of the block."""
    layout.locks.mkdir(parents=True, exist_ok=True)
    lock = layout.locks / f"session-{os.getpid()}.json"
    lock.write_text(json.dumps({"pid": os.getpid(), "project": project, "purpose": purpose,
                                "started": time.time()}), encoding="utf-8")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def active_sessions(layout: Layout) -> list[dict]:
    found = []
    for lock in sorted(layout.locks.glob("session-*.json")) if layout.locks.exists() else []:
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if pid_alive(int(info.get("pid", 0))):
            found.append(info)
        else:
            lock.unlink(missing_ok=True)  # left behind by a closed terminal
    return found


def running_tasks(layout: Layout) -> list[dict]:
    """Tasks recorded as mid-execution in any project (interrupted ones are resumable later)."""
    found = []
    for database in sorted(layout.projects.glob("*/brain.sqlite3")) if layout.projects.exists() else []:
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                rows = db.execute("SELECT body FROM tasks").fetchall()
        except sqlite3.Error:
            continue
        for (body,) in rows:
            task = json.loads(body)
            if task.get("status") in RUNNING:
                found.append({"project": database.parent.name, "id": task["id"], "status": task["status"]})
    return found
