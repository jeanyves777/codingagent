"""Continuous orchestration: the conductor keeps every agent busy with the highest-value ready
backlog item, recovers from failures and stalls, escalates within budget, survives restarts and
never marks work verified on an agent's word. These tests drive brain.conductor with a scripted
executor and a controlled clock; test_conductor_brain.py runs the same conductor on the real Brain."""
import asyncio

import pytest

from brain.conductor import Backlog, BacklogError, Conductor, Resources, signature

ACCEPTANCE = {"files": {"tests/test_accept.py": "def test_it():\n    assert True\n"},
              "nodes": ["tests/test_accept.py::test_it"], "criteria": "it works"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Agents:
    """A scripted executor. Each item's goal names its script: a list of outcomes, one per attempt:
    'pass' (the agent reports success and the work is really right), 'lie' (the agent reports
    success but the acceptance tests fail), 'fail' (the task fails), 'hang' (no progress ever),
    'slow' (works, needs more ticks)."""

    def __init__(self, scripts, clock, premium=False):
        self.scripts, self.clock, self.premium = scripts, clock, premium
        self.tasks, self.started, self.stopped, self.approved = {}, [], [], []
        self.concurrent = self.peak = 0

    def _outcome(self, item):
        script = self.scripts[item["title"]]
        return script[min(item["attempts"], len(script) - 1)]

    async def start(self, item, strategy, feedback):
        self.counter = getattr(self, "counter", 0) + 1
        task_id = f"t{self.counter}"
        outcome = self._outcome(item)
        self.tasks[task_id] = {"item": item["title"], "outcome": outcome, "ticks": 0, "status": "planning",
                               "strategy": strategy, "feedback": feedback, "progress_at": self.clock()}
        self.started.append((item["title"], strategy))
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        return task_id

    async def poll(self, task_id):
        task = self.tasks[task_id]
        if task["status"] in {"planning", "running"}:
            task["ticks"] += 1
            if task["outcome"] != "hang":
                task["progress_at"] = self.clock()
            needed = 3 if task["outcome"] == "slow" else 1
            if task["outcome"] == "interrupted":  # a restart stopped it; the next attempt runs the rest
                task["status"], self.scripts[task["item"]] = "cancelled", self.scripts[task["item"]][1:]
                self.concurrent -= 1
                return {"status": "cancelled", "digest": None, "progress_at": task["progress_at"],
                        "failure": "cancelled", "interrupted": True}
            if task["outcome"] != "hang" and task["ticks"] >= needed:
                task["status"] = "failed" if task["outcome"] == "fail" else "proposed"
                if task["status"] == "failed":
                    self.concurrent -= 1
        return {"status": task["status"], "digest": "d" + task_id, "progress_at": task["progress_at"],
                "failure": "AssertionError: expected 3 got 2" if task["outcome"] == "fail" else None}

    async def approve(self, task_id, digest):
        self.approved.append(task_id)
        self.tasks[task_id]["status"] = "passed"
        self.concurrent -= 1

    async def stop(self, task_id):
        self.stopped.append(task_id)
        self.tasks[task_id]["status"] = "cancelled"
        self.concurrent -= 1

    async def verify(self, task_id, item):
        good = self.tasks[task_id]["outcome"] in {"pass", "slow"}
        return {"passed": good, "exit_code": 0 if good else 1, "command": "pytest",
                "output": "1 passed" if good else "FAILED tests/test_accept.py::test_it - assert False"}

    async def premium_available(self):
        return self.premium

    def accept(self, title):  # the user accepts the result
        for task in self.tasks.values():
            if task["item"] == title and task["status"] == "passed":
                task["status"] = "accepted"


def setup(tmp_path, scripts, slots=2, premium=False, auto=True, stall=300):
    clock = Clock()
    backlog = Backlog(tmp_path / "backlog.sqlite3")
    agents = Agents(scripts, clock, premium)
    conductor = Conductor(backlog, agents, auto_execute=auto, stall_seconds=stall, clock=clock,
                          resources=Resources(max_local=slots))
    return backlog, agents, conductor, clock


def run(coroutine):
    return asyncio.run(coroutine)


async def ticks(conductor, count, clock=None, step=1):
    for _ in range(count):
        await conductor.tick()
        if clock:
            clock.now += step


def states(backlog):
    return {item["title"]: item["state"] for item in backlog.list()}


# 1. Backlog ---------------------------------------------------------------------------------------

def test_backlog_validates_priorities_dependencies_and_acceptance(tmp_path):
    backlog = Backlog(tmp_path / "b.sqlite3")
    with pytest.raises(BacklogError):
        backlog.add("x", priority="P9")
    with pytest.raises(BacklogError):
        backlog.add("x", depends_on=["nope"])
    with pytest.raises(BacklogError):
        backlog.add("x", acceptance={"files": {"../escape.py": "x"}, "nodes": ["../escape.py::t"]})
    with pytest.raises(BacklogError):
        backlog.add("x", acceptance={"files": {"t.py": "x"}, "nodes": ["other.py::t"]})
    for unrunnable in ("t.py", "t.py::C::test_x"):  # the sandbox would refuse to run these
        with pytest.raises(BacklogError, match="test_name"):
            backlog.add("x", acceptance={"files": {"t.py": "x"}, "nodes": [unrunnable]})
    first = backlog.add("first", priority="P2", acceptance=ACCEPTANCE)
    second = backlog.add("second", priority="P0", depends_on=[first["id"]], acceptance=ACCEPTANCE)
    assert second["state"] == "waiting" and first["state"] == "ready"
    assert [item["title"] for item in backlog.list()] == ["second", "first"]  # P0 first


# 2. Parallel execution and no idle agents ------------------------------------------------------------

def test_independent_items_run_in_parallel_up_to_the_slots_and_highest_priority_first(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {t: ["slow"] for t in "abcd"}, slots=2)
    for title, priority in zip("abcd", ["P2", "P0", "P1", "P0"]):
        backlog.add(title, title=title, priority=priority, acceptance=ACCEPTANCE)
    run(conductor.tick())
    assert [title for title, _ in agents.started] == ["b", "d"]  # the two P0 items, never more than 2
    assert agents.concurrent == 2
    run(ticks(conductor, 10, clock))
    assert agents.peak == 2  # the machine's limit was respected throughout
    assert set(states(backlog).values()) == {"verified"}


def test_a_finished_agent_immediately_gets_the_next_ready_item(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["pass"], "b": ["slow"], "c": ["pass"]}, slots=2)
    for title in "abc":
        backlog.add(title, title=title, acceptance=ACCEPTANCE)
    run(conductor.tick())                     # a and b start; c waits for a slot
    assert [t for t, _ in agents.started] == ["a", "b"]
    run(conductor.tick())                     # a's proposal is executed; it still holds its slot
    assert [t for t, _ in agents.started] == ["a", "b"]
    run(conductor.tick())                     # a is verified: its slot goes to c in the same pass
    assert states(backlog)["a"] == "verified" and [t for t, _ in agents.started] == ["a", "b", "c"]


# 3. Failure recovery, reassignment, escalation ----------------------------------------------------------

def test_a_failed_attempt_is_reassigned_with_its_failure_as_feedback(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["fail", "pass"], "b": ["slow"]})
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    backlog.add("b", title="b", acceptance=ACCEPTANCE)
    run(ticks(conductor, 8, clock))
    assert [s for t, s in agents.started if t == "a"] == ["local", "local_with_feedback"]
    retry = [task for task in agents.tasks.values() if task["item"] == "a"][1]
    assert "expected 3 got 2" in retry["feedback"]
    assert states(backlog)["a"] == "verified"
    assert states(backlog)["b"] == "verified"  # one agent failing never stopped the other


def test_repeated_identical_failure_escalates_to_a_premium_plan_within_budget(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["fail", "fail", "pass"]}, premium=True)
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(ticks(conductor, 8, clock))
    assert [s for _, s in agents.started] == ["local", "local_with_feedback", "premium_plan"]
    assert states(backlog)["a"] == "verified"


def test_without_premium_budget_a_repeated_failure_is_blocked_with_its_reason(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["fail"] * 6}, premium=False)
    backlog.add("a", title="a", acceptance=ACCEPTANCE, max_attempts=6)
    run(ticks(conductor, 12, clock))
    item = backlog.list()[0]
    assert item["state"] == "blocked" and "no premium supervisor budget" in item["reason"]
    assert len(agents.started) == 2  # no infinite retry loop: local, local with feedback, then stop


def test_attempt_limit_stops_retrying(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["fail"]}, premium=True)
    backlog.add("a", title="a", acceptance=ACCEPTANCE, max_attempts=2)
    run(ticks(conductor, 10, clock))
    assert len(agents.started) == 2 and backlog.list()[0]["state"] == "blocked"
    assert "2 attempts used" in backlog.list()[0]["reason"]


# 4. Stalls ------------------------------------------------------------------------------------------------

def test_a_stalled_agent_is_stopped_and_its_item_reassigned(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["hang", "pass"]}, stall=300)
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(conductor.tick())
    clock.now += 200
    run(conductor.tick())
    assert agents.stopped == []  # quiet but within the limit
    clock.now += 200
    run(conductor.tick())
    assert agents.stopped == ["t1"]
    attempt = backlog.attempts()[0]
    assert attempt["outcome"] == "stalled" and "no progress" in attempt["detail"]
    run(ticks(conductor, 3, clock))
    assert states(backlog)["a"] == "verified" and len(agents.started) == 2


# 5. Honest completion ----------------------------------------------------------------------------------------

def test_an_agent_claiming_success_is_not_verified_when_acceptance_fails(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["lie", "pass"]})
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(ticks(conductor, 3, clock))
    first = backlog.attempts()[0]
    assert first["outcome"] == "verification_failed" and "acceptance tests failed" in first["detail"]
    verdicts = [(entry["kind"], entry["verdict"]) for entry in backlog.ledger()]
    assert verdicts[0] == ("verification", "failed")
    run(ticks(conductor, 3, clock))
    assert states(backlog)["a"] == "verified"
    assert backlog.ledger()[-1]["verdict"] == "passed" and backlog.verify_ledger()


def test_work_without_acceptance_tests_can_never_be_verified(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["pass"]})
    backlog.add("a", title="a")
    run(ticks(conductor, 3, clock))
    item = backlog.list()[0]
    assert item["state"] == "blocked" and "no acceptance tests" in item["reason"]


def test_the_evidence_ledger_detects_tampering(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["pass"]})
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(ticks(conductor, 3, clock))
    assert backlog.ledger() and backlog.verify_ledger()  # not vacuously intact
    with backlog.connect() as db:
        db.execute("UPDATE ledger SET verdict='passed', evidence='{\"exit_code\": 0}'")
    assert not backlog.verify_ledger()


# 6. Dependencies and human acceptance ------------------------------------------------------------------------

def test_dependents_start_only_after_the_user_accepts_and_inherit_a_blocked_dependency(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["pass"], "b": ["pass"], "x": ["fail"]},
                                              premium=False)
    a = backlog.add("a", title="a", acceptance=ACCEPTANCE)
    backlog.add("b", title="b", depends_on=[a["id"]], acceptance=ACCEPTANCE)
    x = backlog.add("x", title="x", acceptance=ACCEPTANCE, max_attempts=1)
    backlog.add("y", title="y", depends_on=[x["id"]], acceptance=ACCEPTANCE)
    run(ticks(conductor, 3, clock))
    assert states(backlog)["a"] == "verified" and states(backlog)["b"] == "waiting"
    assert states(backlog)["y"] == "blocked"  # its dependency x ran out of attempts
    agents.accept("a")  # only the user accepts work onto a branch
    run(ticks(conductor, 4, clock))
    assert states(backlog)["a"] == "done" and states(backlog)["b"] == "verified"


def test_without_auto_execution_proposals_wait_for_the_user_and_other_agents_keep_working(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {t: ["pass"] for t in "abc"}, slots=1, auto=False)
    for title in "abc":
        backlog.add(title, title=title, acceptance=ACCEPTANCE)
    run(ticks(conductor, 4, clock))
    assert agents.approved == []  # never approves on the user's behalf
    assert set(states(backlog).values()) == {"awaiting_approval"}
    assert len(agents.started) == 3  # the single slot moved on while each proposal waited
    status = conductor.status()
    assert status["waiting_on_people_only"] and not status["finished"]


def test_a_dependent_of_unaccepted_work_counts_as_waiting_for_people(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["pass"], "b": ["pass"]})
    first = backlog.add("a", title="a", acceptance=ACCEPTANCE)
    backlog.add("b", title="b", depends_on=[first["id"]], acceptance=ACCEPTANCE)
    status = run(conductor.run(poll=0, max_ticks=20))
    assert status["waiting_on_people_only"] and "stopped" not in status  # returns at once, no spinning
    assert status["counts"] == {"verified": 1, "waiting": 1}


# 7. Restart -----------------------------------------------------------------------------------------------------

def test_a_restarted_conductor_resumes_from_the_persisted_backlog(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["slow"], "b": ["slow"], "c": ["slow"]}, slots=2)
    for title in "abc":
        backlog.add(title, title=title, acceptance=ACCEPTANCE)
    run(conductor.tick())
    assert len(agents.started) == 2
    # the process stops; a new one opens the same backlog and the same agents' task records
    restarted = Conductor(Backlog(tmp_path / "backlog.sqlite3"), agents, auto_execute=True, clock=clock,
                          resources=Resources(max_local=2))
    run(ticks(restarted, 8, clock))
    assert len(agents.started) == 3  # no duplicate starts of a or b
    assert set(states(backlog).values()) == {"verified"}


def test_a_task_lost_during_a_restart_is_requeued(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["slow", "pass"]})
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(conductor.tick())
    del agents.tasks["t1"]

    async def missing(task_id):
        raise KeyError(task_id)
    original = agents.poll
    agents.poll = lambda task_id: missing(task_id) if task_id == "t1" else original(task_id)
    run(ticks(conductor, 4, clock))
    assert backlog.attempts()[0]["outcome"] == "lost" and states(backlog)["a"] == "verified"


# 8. Loop termination, status, resources --------------------------------------------------------------------------

def test_run_stops_when_every_item_is_settled_or_waits_only_for_people(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["pass"], "b": ["fail"]})
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    backlog.add("b", title="b", acceptance=ACCEPTANCE, max_attempts=1)
    status = run(conductor.run(poll=0, max_ticks=50))
    assert status["waiting_on_people_only"] and "stopped" not in status
    assert status["counts"] == {"blocked": 1, "verified": 1} and status["ledger_intact"]


def test_status_reports_busy_agents_and_reasons(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["slow"]})
    backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(conductor.tick())
    status = conductor.status()
    assert status["agents"]["busy"][0]["strategy"] == "local" and status["counts"] == {"running": 1}


def test_cancel_stops_the_running_task(tmp_path):
    backlog, agents, conductor, clock = setup(tmp_path, {"a": ["slow"]})
    item = backlog.add("a", title="a", acceptance=ACCEPTANCE)
    run(conductor.tick())
    assert run(conductor.cancel(item["id"]))["state"] == "cancelled" and agents.stopped == ["t1"]


def test_local_slots_follow_memory_and_cores():
    gb = 1e9
    assert Resources(lambda: {"ram": 16 * gb, "cores": 8}, per_agent_gb=6, max_local=4).local_slots() == 2
    assert Resources(lambda: {"ram": 64 * gb, "cores": 4}, per_agent_gb=6, max_local=4).local_slots() == 2
    assert Resources(lambda: {"ram": 64 * gb, "cores": 32}, per_agent_gb=6, max_local=4).local_slots() == 4
    assert Resources(lambda: {"ram": 4 * gb, "cores": 2}).local_slots() == 1  # never zero
    assert Resources(lambda: (_ for _ in ()).throw(OSError())).local_slots() == 1


def test_failure_signatures_ignore_numbers_ids_and_paths():
    assert signature("AssertionError at /tmp/a1/x.py:12 got 3") == signature("AssertionError at /tmp/b9/x.py:40 got 7")
    assert signature("TypeError") != signature("KeyError")


def test_an_attempt_interrupted_by_a_restart_is_retried_without_using_up_the_item(tmp_path):
    clock = Clock()
    backlog = Backlog(tmp_path / "b.sqlite3")
    item = backlog.add("Create alpha", title="alpha", max_attempts=1, acceptance=ACCEPTANCE)
    agents = Agents({"alpha": ["interrupted", "pass"]}, clock)
    conductor = Conductor(backlog, agents, auto_execute=True, resources=Resources(max_local=2), clock=clock)
    for _ in range(6):
        asyncio.run(conductor.tick())
    assert backlog.get(item["id"])["state"] == "verified"  # one allowed attempt, still verified
    assert [attempt["outcome"] for attempt in backlog.attempts()] == ["interrupted", None]
    assert backlog.get(item["id"])["attempts"] == 1
