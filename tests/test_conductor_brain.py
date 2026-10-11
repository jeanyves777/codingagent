"""The conductor on the real Brain: real git worktrees, the real task lifecycle, proposal execution
and the independent verifier running real pytest on frozen acceptance tests. Only the model is a
scripted stand-in and the sandbox runs pytest locally instead of in Docker."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import brain.sandbox
import brain.service
from brain.conductor import Backlog, BrainExecutor, Conductor, Resources
from brain.service import Brain
from test_brain import make_repository


def local_pytest(workspace, image, should_cancel=None, only=None):
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(only or [])],
                            cwd=workspace, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})  # as the real sandbox
    return {"passed": result.returncode == 0, "exit_code": result.returncode,
            "output": result.stdout + result.stderr, "profile": "python"}


class StandIn:
    """Writes `value.py`. For goals containing 'cheat', the first attempt writes the wrong value and
    a conftest.py that skips every test, so the agent's own run 'passes'; for 'shadow' it adds a
    pytest.py that prints a pass instead of running anything."""

    def __init__(self, latency=0.3):
        self.latency, self.calls, self.active, self.peak = latency, [], 0, 0

    async def propose(self, root, goal, memories, repository_context=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.latency)
        finally:
            self.active -= 1
        self.calls.append(goal)
        name = "beta" if "beta" in goal else "alpha"
        if "cheat" in goal and "Previous attempt" not in goal:
            changes = [{"path": f"{name}.py", "content": "VALUE = 'wrong'\n"},
                       {"path": "conftest.py", "content": "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n"
                                                          "    for item in items:\n"
                                                          "        item.add_marker(pytest.mark.skip(reason='done'))\n"}]
        elif "append" in goal:  # edits the same lines of main.py as the other 'append' item
            current = (Path(root) / "main.py").read_text()
            changes = [{"path": f"{name}.py", "content": f"VALUE = '{name}'\n"},
                       {"path": "main.py", "content": current + f"{name.upper()} = True\n"}]
        elif "guard" in goal and "Previous attempt" not in goal:  # passes alone, breaks alpha's work
            changes = [{"path": f"{name}.py", "content": f"VALUE = '{name}'\n"},
                       {"path": "test_guard.py", "content": "import os\n\n\ndef test_guard():\n"
                                                            "    assert not os.path.exists('alpha.py')\n"}]
        elif "shadow" in goal and "Previous attempt" not in goal:  # replaces pytest itself
            changes = [{"path": f"{name}.py", "content": "VALUE = 'wrong'\n"},
                       {"path": "pytest.py", "content": "print('1 passed')\n"}]
        else:
            changes = [{"path": f"{name}.py", "content": f"VALUE = '{name}'\n"}]
        return json.dumps({"plan": f"write {name}", "changes": changes})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}

    async def decompose(self, goal):
        raise AssertionError("not used")


def acceptance(name):
    return {"files": {f"tests/test_{name}_accept.py": f"from {name} import VALUE\n\n\ndef test_value():\n"
                                                      f"    assert VALUE == '{name}'\n"},
            "nodes": [f"tests/test_{name}_accept.py::test_value"], "criteria": f"{name}.VALUE is '{name}'"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(brain.service, "run_tests", local_pytest)   # the agent's own test run
    monkeypatch.setattr(brain.sandbox, "run_tests", local_pytest)   # the independent verifier
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    model = StandIn()
    the_brain = Brain(repository.parent, tmp_path / "data", model, "img", workers=2)
    backlog = Backlog(tmp_path / "data" / "backlog.sqlite3")
    conductor = Conductor(backlog, BrainExecutor(the_brain, "demo"), auto_execute=True,
                          resources=Resources(max_local=2), stall_seconds=120)
    return {"brain": the_brain, "backlog": backlog, "conductor": conductor, "model": model, "repo": repository}


async def drive(conductor, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await conductor.tick()
        status = conductor.status()
        if status["finished"] or status["waiting_on_people_only"]:
            return status
        await asyncio.sleep(0.05)
    return conductor.status()


def test_two_agents_work_in_parallel_and_both_results_are_independently_verified(world):
    world["backlog"].add("Create alpha.py with VALUE = 'alpha'", title="alpha", acceptance=acceptance("alpha"))
    world["backlog"].add("Create beta.py with VALUE = 'beta'", title="beta", acceptance=acceptance("beta"))
    status = asyncio.run(drive(world["conductor"]))
    assert {item["title"]: item["state"] for item in status["items"]} == {"alpha": "verified", "beta": "verified"}
    assert world["model"].peak == 2  # both agents' model calls really overlapped
    assert all("def test_value" in goal and "run unchanged" in goal for goal in world["model"].calls)  # the spec
    assert status["waiting_on_people_only"]  # acceptance onto a branch stays with the user
    ledger = world["backlog"].ledger()
    assert [entry["verdict"] for entry in ledger if entry["kind"] == "verification"] == ["passed", "passed"]
    assert [entry["verdict"] for entry in ledger if entry["kind"] == "integration"] == ["included", "included"]
    assert status["ledger_intact"] and len(status["integration"]["included"]) == 2  # both pass together
    tasks = [world["brain"].store.get(item["task_id"]) for item in world["backlog"].list()]
    assert all(task["status"] == "passed" for task in tasks)  # executed under autonomy=auto, never accepted for the user
    assert len({world["brain"].workspace(task["id"]) for task in tasks}) == 2  # isolated worktrees


def test_an_agent_that_skips_the_tests_is_caught_and_its_retry_verified(world):
    world["backlog"].add("Create beta.py with VALUE = 'beta' (cheat)", title="beta", acceptance=acceptance("beta"))
    status = asyncio.run(drive(world["conductor"]))
    attempts = world["backlog"].attempts()
    # the agent's own run passed (its conftest skipped everything); the verifier restored the
    # project's test configuration and the frozen acceptance test failed
    assert attempts[0]["outcome"] == "verification_failed"
    assert world["brain"].store.get(attempts[0]["task_id"])["status"] == "passed"
    assert attempts[1]["strategy"] == "local_with_feedback" and "Previous attempt" in world["model"].calls[-1]
    assert status["items"][0]["state"] == "verified"
    assert [entry["verdict"] for entry in world["backlog"].ledger()] == ["failed", "passed", "included"]


def test_user_acceptance_completes_the_item_and_releases_its_dependent(world):
    first = world["backlog"].add("Create alpha.py with VALUE = 'alpha'", title="alpha", acceptance=acceptance("alpha"))
    world["backlog"].add("Create beta.py with VALUE = 'beta'", title="beta", depends_on=[first["id"]],
                         acceptance=acceptance("beta"))
    asyncio.run(drive(world["conductor"]))
    assert {i["title"]: i["state"] for i in world["backlog"].list()} == {"alpha": "verified", "beta": "waiting"}

    async def accept_then_continue():
        await world["brain"].accept(world["backlog"].get(first["id"])["task_id"], "accepted by the user")
        return await drive(world["conductor"])
    status = asyncio.run(accept_then_continue())
    assert {item["title"]: item["state"] for item in status["items"]} == {"alpha": "done", "beta": "verified"}


def test_an_agent_that_replaces_the_test_runner_is_never_verified(world):
    world["backlog"].add("Create beta.py with VALUE = 'beta' (shadow)", title="beta", acceptance=acceptance("beta"))
    status = asyncio.run(drive(world["conductor"]))
    attempts = world["backlog"].attempts()
    assert attempts[0]["outcome"] == "verification_failed"
    assert "replace the test runner" in world["backlog"].ledger()[0]["evidence"]["output_tail"]
    assert status["items"][0]["state"] == "verified" and len(attempts) == 2


def test_verified_items_that_edit_the_same_lines_are_combined_after_one_is_redone_on_top(world):
    world["backlog"].add("Create alpha.py with VALUE = 'alpha' (append)", title="alpha", acceptance=acceptance("alpha"))
    world["backlog"].add("Create beta.py with VALUE = 'beta' (append)", title="beta", priority="P2",
                         acceptance=acceptance("beta"))
    status = asyncio.run(drive(world["conductor"]))
    backlog, brain = world["backlog"], world["brain"]
    kinds = [(entry["kind"], entry["verdict"]) for entry in backlog.ledger()]
    assert ("integration", "integration_conflict") in kinds  # both changed the end of main.py
    beta = [attempt for attempt in backlog.attempts() if attempt["item_id"] == status["items"][1]["id"]]
    assert beta[0]["outcome"] == "integration_conflict" and beta[1]["strategy"] == "local_with_feedback"
    combined = status["integration"]
    assert combined["state"] == "candidate" and len(combined["included"]) == 2 and not combined["excluded"]
    assert brain.store.get(beta[1]["task_id"])["base_commit"] == backlog.integrations()[0]["head"]  # redone on top
    show = lambda path: subprocess.run(["git", "-C", str(world["repo"]), "show", f"{combined['head']}:{path}"],  # noqa: E731
                                       capture_output=True, text=True).stdout
    assert "ALPHA = True" in show("main.py") and "BETA = True" in show("main.py")
    assert subprocess.run(["git", "-C", str(world["repo"]), "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""  # the user's checkout is untouched


def test_items_that_pass_alone_but_break_the_project_together_are_not_combined(world):
    world["backlog"].add("Create alpha.py with VALUE = 'alpha'", title="alpha", acceptance=acceptance("alpha"))
    world["backlog"].add("Create beta.py with VALUE = 'beta' (guard)", title="beta", priority="P2",
                         acceptance=acceptance("beta"))
    status = asyncio.run(drive(world["conductor"]))
    failed = [entry for entry in world["backlog"].ledger() if entry["verdict"] == "integration_failed"]
    assert len(failed) == 1 and "project's own tests failed" in world["backlog"].attempts()[1]["detail"]
    assert {item["title"]: item["state"] for item in status["items"]} == {"alpha": "verified", "beta": "verified"}
    assert len(status["integration"]["included"]) == 2  # the redone beta no longer breaks alpha


def test_a_blocked_task_reports_why_so_the_retry_and_the_user_see_it(world):
    task = world["brain"].submit("demo", "x", launch=False)
    task["status"] = "blocked"
    task["events"] += [{"kind": "blocked", "detail": "Workspace, index, or model planning failed"},
                       {"kind": "blocked", "detail": "ReadTimeout"}]
    world["brain"].store.save(task)
    polled = asyncio.run(BrainExecutor(world["brain"], "demo").poll(task["id"]))
    assert polled["failure"] == "blocked: Workspace, index, or model planning failed; ReadTimeout"
