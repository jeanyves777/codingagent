"""SQLite under the continuous conductor: N agents write from one process while `backlog status`,
the desktop and the engine API read from other processes. Each test reproduces a problem measured
with stress runs (or guards an invariant that the stress runs confirmed)."""
import asyncio
import json
import sqlite3
import threading

import brain.service
from brain.conductor import Backlog
from brain.service import Brain
from brain.store import Store
from test_brain import make_repository
from test_conductor_brain import StandIn


def _read_while_locked(path, read, seconds=3.0):
    """Run `read` in a thread while another connection holds the database's write lock (as a
    committing writer does). Returns (finished in time, error)."""
    writer = sqlite3.connect(path, isolation_level=None)
    outcome = {}

    def reader():
        try:
            read()
        except sqlite3.Error as error:
            outcome["error"] = error
        outcome["done"] = True

    try:
        writer.execute("BEGIN EXCLUSIVE")
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(seconds)
        finished = outcome.get("done", False)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    thread.join()
    return finished, outcome.get("error")


def test_backlog_status_is_not_blocked_by_a_writer(tmp_path):
    backlog = Backlog(tmp_path / "backlog.sqlite3")
    backlog.add("goal")
    finished, error = _read_while_locked(backlog.path, lambda: (backlog.list(), backlog.ledger(), backlog.decisions()))
    assert finished and error is None


def test_task_store_reads_are_not_blocked_by_a_writer(tmp_path):
    store = Store(tmp_path / "brain.sqlite3")
    store.save({"id": "t1", "status": "running"})
    finished, error = _read_while_locked(store.path, lambda: (store.tasks(), store.get("t1")))
    assert finished and error is None


def test_concurrent_ledger_appends_keep_one_unbroken_chain(tmp_path):
    backlog = Backlog(tmp_path / "backlog.sqlite3")
    item = backlog.add("goal")
    writers, records = 6, 25
    barrier = threading.Barrier(writers)

    def append(number):
        mine = Backlog(backlog.path)
        barrier.wait()
        for index in range(records):
            mine.record(item["id"], None, "evidence", "pass", {"writer": number, "index": index})

    threads = [threading.Thread(target=append, args=(number,)) for number in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(backlog.ledger()) == writers * records
    assert backlog.verify_ledger()


def test_a_cancel_survives_a_write_back_of_the_running_tasks_copy(tmp_path):
    store = Store(tmp_path / "brain.sqlite3")
    store.save({"id": "t1", "status": "running", "cancel_requested": False})
    running = store.get("t1")                     # the execution loop's in-memory task
    request = store.get("t1")                     # the conductor stops the task meanwhile
    request.update(cancel_requested=True, status="cancellation_requested")
    store.save(request)
    running["status"] = "testing"                 # the loop records its next step
    store.save(running)
    assert store.get("t1")["cancel_requested"] is True
    store.save({**running, "cancel_requested": False}, clear_cancel=True)  # retry clears it on purpose
    assert store.get("t1")["cancel_requested"] is False


class Repairing(StandIn):
    """Each proposal (the first and every repair) is a new revision of alpha.py."""

    async def propose(self, root, goal, memories, repository_context=None):
        self.calls.append(goal)
        return json.dumps({"plan": "write alpha",
                           "changes": [{"path": "alpha.py", "content": f"VALUE = 'alpha'  # {len(self.calls)}\n"}]})


def test_conductor_stop_during_the_sandbox_run_cancels_the_task(tmp_path, monkeypatch):
    """Brain.cancel (the conductor's stop on a stall) lands while the task's first test run is
    finishing; the loop then saves its own copy of the task. The cancel must not be overwritten:
    the task stops at the next boundary instead of repairing and testing again."""
    the_brain = Brain(make_repository(tmp_path / "repos" / "demo", use_git=True).parent, tmp_path / "data",
                      Repairing(), "img", workers=2)
    state = {}

    def run_tests(workspace, image, should_cancel=None, only=None):
        state["runs"] = state.get("runs", 0) + 1
        if state["runs"] == 1:  # the run already finished (failing) when the stop arrives
            state["stopped"] = the_brain.cancel(state["task_id"])["status"]
            return {"passed": False, "exit_code": 1, "output": "FAILED test_main.py::test_ok - assert 1 == 2\n"
                    "1 failed in 0.01s", "profile": "python"}
        return {"passed": True, "exit_code": 0, "output": "1 passed in 0.01s", "profile": "python"}

    monkeypatch.setattr(brain.service, "run_tests", run_tests)

    async def scenario():
        task = the_brain.submit("demo", "write alpha")
        state["task_id"] = task["id"]
        for _ in range(400):
            task = the_brain.store.get(task["id"])
            if task["status"] == "proposed":
                break
            await asyncio.sleep(0.02)
        try:
            await the_brain.execute(task["id"], task["digest"])
        except asyncio.CancelledError:
            pass
        return the_brain.store.get(task["id"])

    final = asyncio.run(scenario())
    assert state["stopped"] == "cancellation_requested"
    assert final["cancel_requested"] is True
    assert final["status"] == "cancelled"
    assert state["runs"] == 1


def test_an_update_can_release_the_task_store_after_it_was_used(tmp_path):
    """The updater folds each database out of WAL before restoring a backup, which SQLite refuses
    while any connection is open. Connections must be closed when used, not when the garbage
    collector happens to run (a sqlite3 connection sits in a reference cycle with its cache)."""
    import gc
    from brain.local import updater
    from brain.local.memory import _coding_brain_records
    from brain.local.paths import Layout
    from brain.local.session import running_tasks

    layout = Layout(tmp_path / "home")
    path = layout.projects / "app" / "brain.sqlite3"
    store = Store(path)
    enabled = gc.isenabled()
    gc.disable()
    try:
        store.save({"id": "t1", "status": "running"})
        store.tasks()
        store.remember("t1", {"repository": "app", "content": "x"})
        running_tasks(layout)        # the updater's own guard reads every project's tasks
        _coding_brain_records(path)
        updater._release_database(path)
    finally:
        if enabled:
            gc.enable()
    assert not path.with_name("brain.sqlite3-wal").exists()
