"""Session locks: an update never replaces application code while a coding session is active."""
import json
import os
import sqlite3
import sys
import time
import socket
import uuid
from contextlib import contextmanager

from .paths import Layout

RUNNING = {"planning", "running", "testing", "queued", "cancellation_requested"}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied or an unknown query failure is not evidence of exit.
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID absent
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
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


def recover_interrupted_tasks(layout: Layout) -> list[dict]:
    """Back up and block tasks with a confirmed dead owner on this computer.

    Unknown owners, other hosts, live processes and queued jobs without an owner
    remain guarded. Nothing is killed, resumed, accepted or deleted.
    """
    recovered = []
    for database in sorted(layout.projects.glob('*/brain.sqlite3')) if layout.projects.exists() else []:
        with sqlite3.connect(database, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            for task_id, body in db.execute('SELECT id, body FROM tasks').fetchall():
                task = json.loads(body)
                owner = task.get('owner') or {}
                owner_pid = owner.get('pid')
                if task.get('status') not in RUNNING or owner.get('host') != socket.gethostname() or \
                        not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0 or pid_alive(owner_pid):
                    continue
                backup = layout.backups / 'interrupted-tasks' / (uuid.uuid4().hex + '.json')
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(body, encoding='utf-8')
                previous = task['status']
                task['status'] = 'blocked'
                task['interruption'] = {'previous_status': previous, 'at': time.time(), 'backup': str(backup)}
                task.setdefault('events', []).append({'time': time.time(), 'kind': 'interrupted',
                    'detail': 'Owning engine process exited; work preserved. Retry or resume explicitly.'})
                db.execute('UPDATE tasks SET body=? WHERE id=? AND body=?', (json.dumps(task), task_id, body))
                recovered.append({'project': database.parent.name, 'id': task_id, 'previous_status': previous,
                                  'backup': str(backup)})
    return recovered
