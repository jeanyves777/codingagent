"""After a task is accepted as a pull request, the desktop watches its checks and asks before
merging only once every check is green; the merge is bound to the head the user was asked about."""
import asyncio
import threading

from desktop_ui import core
from desktop_ui.engine_client import EngineError
from desktop_ui.server import Job


class Client:
    info = {"features": ["tasks.pull_requests"]}

    def __init__(self, states):
        self.states, self.calls = list(states), []

    def call(self, op, params=None, on_event=None, timeout=None):
        self.calls.append((op, params))
        if op == "tasks.pull_request":
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if op == "tasks.merge":
            return {"merged": True, "url": "https://github.com/o/r/pull/1"}
        raise EngineError("unknown_op", op)


GREEN = {"state": "OPEN", "ask_merge": True, "failed": 0, "pending": 0, "head": "a" * 40,
         "url": "https://github.com/o/r/pull/1", "question": "All 3 checks are green on https://github.com/o/r/pull/1. Merge it?"}
PENDING = {**GREEN, "ask_merge": False, "pending": 2, "question": None}


def decide(job, allow):
    def answer():
        while not job.awaiting_approval:
            job.stop_requested.wait(.01)
        assert job.pending_payload["kind"] == "merge" and "Merge it?" in job.pending_payload["summary"]
        job.decision_allow = allow
        job.decision_event.set()
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def run(job, client):
    asyncio.run(core.offer_merge(job, client, "p", "t", every=0))


def test_asks_only_when_green_and_merges_the_confirmed_head():
    job, client = Job("run", None, ["goal"]), Client([PENDING, PENDING, GREEN])
    decide(job, True)
    run(job, client)
    assert [op for op, _ in client.calls] == ["tasks.pull_request"] * 3 + ["tasks.merge"]
    assert client.calls[-1][1]["head"] == "a" * 40


def test_declining_leaves_the_pull_request_open():
    job, client = Job("run", None, ["goal"]), Client([GREEN])
    decide(job, False)
    run(job, client)
    assert "tasks.merge" not in [op for op, _ in client.calls]
    assert any("stays open" in event["message"] for event in job.dump())


def test_failed_checks_are_reported_and_never_offered():
    job, client = Job("run", None, ["goal"]), Client([{**PENDING, "pending": 0, "failed": 1}])
    run(job, client)
    assert not job.awaiting_approval and "tasks.merge" not in [op for op, _ in client.calls]
    assert any("failed" in event["message"] for event in job.dump() if event["kind"] == "error")


def test_older_engines_are_not_asked():
    job, client = Job("run", None, ["goal"]), Client([GREEN])
    client.info = {"features": []}
    run(job, client)
    assert client.calls == []
