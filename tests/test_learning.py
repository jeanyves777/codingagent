"""Learning from finished work: a later task receives the verified fix of an earlier accepted task
when, and only when, it is relevant; every way of accepting records the same structured lesson;
retries and restarts do not duplicate it; secrets and raw logs stay out; older records still work."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from brain import lessons
from brain.local import cli
from brain.local import config as settings
from brain.local.paths import Layout
from brain.service import Brain

BUGGY = "def parse_duration(text):\n    hours, _, rest = text.partition('h')\n    return int(hours) * 3600\n"
FIXED = ("def parse_duration(text):\n    hours, _, rest = text.partition('h')\n"
         "    minutes = int(rest.rstrip('m') or 0)\n    return int(hours) * 3600 + minutes * 60\n")


def git(path, *arguments):
    return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True, check=True).stdout


class Recording:
    """A scripted implementer that records exactly what it was given for each goal. For the
    duration goal its first change is wrong (the tests fail) and its repair is right."""

    def __init__(self):
        self.seen = []

    async def propose(self, root, goal, memories, repository_context=None, **kwargs):
        self.seen.append({"goal": goal, "context": json.dumps({"memories": memories, "context": repository_context},
                                                                default=str)})
        if "parse_duration" in goal and "minutes" in goal:
            content = FIXED if "failed" in goal.lower() or "Tests failed" in goal or "test_failure" in goal else BUGGY + "# v2\n"
            return json.dumps({"plan": "count the minutes after the hours", "changes": [{"path": "duration.py",
                                                                                         "content": content}]})
        name = "notes.md" if "README" in goal or "notes" in goal else "duration.py"
        content = "# notes\n" if name == "notes.md" else FIXED + "# seconds\n"
        return json.dumps({"plan": "small change", "changes": [{"path": name, "content": content}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}

    def context_for(self, goal: str) -> str:
        """What the first proposal for this goal was given (repairs add the failing test output to
        the query, which may make other lessons relevant, rightly)."""
        return next(item["context"] for item in self.seen if item["goal"].startswith(goal))


def sandbox(workspace, image, **kwargs):
    text = (Path(workspace) / "duration.py").read_text()
    passed = "minutes" in text
    return {"passed": passed, "exit_code": 0 if passed else 1, "profile": "python",
            "output": "1 passed in 0.01s" if passed else
            "E       assert 3600 == 5400\nFAILED test_duration.py::test_minutes - assert 3600 == 5400\n1 failed in 0.02s"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", sandbox)
    layout = Layout(tmp_path / "home").ensure()
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "m"}})
    repo = tmp_path / "projects" / "app"
    repo.mkdir(parents=True)
    (repo / "duration.py").write_text(BUGGY)
    (repo / "test_duration.py").write_text("from duration import parse_duration\n\n\ndef test_minutes():\n"
                                           "    assert parse_duration('1h30m') == 5400\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    model = Recording()
    context = cli.Context(layout, repo)
    context.register()
    context.attach(Brain(repo.parent, context.data, model, "img"))
    return {"context": context, "model": model, "layout": layout, "repo": repo}


def run(context, goal):
    task = context.brain.submit("app", goal, launch=False)
    task = asyncio.run(context.brain.create(task))
    return asyncio.run(cli.handle(context, task, auto=True))


def test_a_later_task_receives_the_verified_fix_of_an_earlier_accepted_task_and_an_unrelated_one_does_not(project):
    context, model = project["context"], project["model"]
    first = run(context, "Fix parse_duration so minutes are counted: '1h30m' must give 5400")
    assert first["status"] == "passed" and len(first["failure_log"]) >= 1  # the first change failed its tests
    first = asyncio.run(cli.accept(context, first))
    assert first["status"] == "accepted"

    # The same project, a new process (restart): a related task and an unrelated one.
    later = cli.Context(project["layout"], project["repo"])
    later.attach(Brain(project["repo"].parent, later.data, model, "img"))
    run(later, "parse_duration must also accept seconds such as '45s' after the minutes")
    related = model.context_for("parse_duration must also accept seconds")
    assert "Problem: Fix parse_duration so minutes are counted" in related
    assert "Failed before: test_failure" in related and "Fix: count the minutes after the hours" in related
    assert "(files: duration.py)" in related and "Verified fix: tests passed (1 passed)" in related

    run(later, "Add a README notes section describing the deployment checklist")
    unrelated = model.context_for("Add a README notes section")
    assert "Problem: Fix parse_duration" not in unrelated and "Failed before" not in unrelated  # (the code index
    # still lists parse_duration: it is in the repository)


def test_cli_backlog_and_engine_acceptance_record_the_same_structured_lesson_once(project, monkeypatch):
    from brain.conductor import Backlog
    from test_conductor_brain import StandIn, acceptance, local_pytest
    from test_conductor_cli import args
    context = project["context"]
    task = asyncio.run(cli.accept(context, run(context, "Fix parse_duration so minutes are counted")))
    cli_records = [r for r in context.memory.store.records() if r["category"] == "change"]
    assert len(cli_records) == 1 and cli_records[0]["text"].startswith("Verified fix: tests passed") and "Problem: Fix parse_duration" in cli_records[0]["text"]
    assert cli_records[0]["verification"] == "verified"

    # backlog: two items accepted together through `backlog accept` (the project's tests pass first)
    (project["repo"] / "duration.py").write_text(FIXED)
    git(project["repo"], "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "fixed")
    monkeypatch.setattr("brain.service.run_tests", local_pytest)
    monkeypatch.setattr("brain.sandbox.run_tests", local_pytest)
    monkeypatch.setattr("brain.local.inference.cached_slots", lambda *a, **k: 2)
    context.attach(Brain(project["repo"].parent, context.data, StandIn(), "img", workers=2))
    context.config["autonomy"]["execution"] = "auto"  # proposals run; acceptance stays the user's
    backlog = Backlog(context.data / "backlog.sqlite3")
    backlog.add("Create alpha.py with VALUE = 'alpha'", title="alpha", acceptance=acceptance("alpha"))
    backlog.add("Create beta.py with VALUE = 'beta'", title="beta", acceptance=acceptance("beta"))
    assert cli.backlog_run(args("run"), context, backlog) == 0
    assert cli.backlog_accept(context, backlog) == 0
    changes = [r for r in context.memory.store.records() if r["category"] == "change"]
    assert len(changes) == 3 and {r["text"].split(". ")[1].split(".")[0] for r in changes} >= {
        "Problem: Create alpha", "Problem: Create beta"}
    assert all(r["text"].startswith("Verified fix: tests passed") and "Problem: " in r["text"] for r in changes)
    # accepting is not repeatable, and recording the same task's lesson again changes nothing
    context.memory.learn("change", lessons.lesson({**task, "status": "accepted"}), task)
    assert len([r for r in context.memory.store.records() if r["category"] == "change"]) == 3


def test_engine_api_acceptance_records_the_lesson(tmp_path, monkeypatch):
    from test_engine import _world, Model, git as engine_git
    world = next(_world(tmp_path, monkeypatch))
    monkeypatch.setattr("brain.service.run_tests", lambda *a, **k: {
        "passed": True, "exit_code": 0, "profile": "python", "output": "1 passed in 0.01s"})
    engine, project = world["engine"], world["project"]
    context = engine.context(project["id"])

    async def build():
        return context.attach(Brain(world["root"].parent, context.data, Model(), "img"))
    engine.run(build())
    task = engine.call("tasks.start", {"project_id": project["id"], "goal": "Fix x in main.py"})
    engine.call("tasks.approve", {"project_id": project["id"], "task_id": task["id"], "digest": task["digest"],
                                  "decision": "approve"})
    engine.call("tasks.accept", {"project_id": project["id"], "task_id": task["id"]})
    changes = [r for r in context.memory.store.records() if r["category"] == "change"]
    assert len(changes) == 1 and changes[0]["text"].startswith("Verified fix") and "Problem: Fix x in main.py" in changes[0]["text"]
    assert "Verified fix: tests passed (1 passed)" in changes[0]["text"]
    assert engine_git(world["root"], "status", "--porcelain") == ""


def test_failures_are_one_record_per_goal_and_retired_once_solved_across_restarts(project):
    context = project["context"]
    memory = context.memory
    goal = "Fix parse_duration so minutes are counted"
    for number, summary in enumerate(["E   assert 3600 == 5400", "E   assert 0 == 5400"]):
        task = {"id": f"t{number}", "goal": goal, "status": "failed",
                "failure_log": [{"category": "test_failure", "diagnosis": "test_isolation", "summary": summary}]}
        memory.learn("repair", lessons.lesson(task), task)
    restarted = cli.Context(project["layout"], project["repo"]).memory  # a new process
    repairs = [r for r in restarted.store.records() if r["category"] == "repair"]
    assert len(repairs) == 1 and "assert 0 == 5400" in repairs[0]["text"] and "Cause found: test_isolation" in repairs[0]["text"]
    assert repairs[0]["verification"] == "unverified"  # an observed failure, not a verified conclusion
    assert "Not solved yet" in json.dumps(restarted.context_for("parse_duration minutes are counted"))
    solved = {"id": "t9", "goal": goal, "status": "accepted", "failure_log": [],
              "proposal": {"plan": "count minutes", "changes": [{"path": "duration.py"}]},
              "test_evidence": {"passed": True, "exit_code": 0, "output": "1 passed"}}
    restarted.learn("change", lessons.lesson(solved), solved)
    assert not [r for r in restarted.store.records() if r["category"] == "repair"]  # retired
    assert "Not solved yet" not in json.dumps(restarted.context_for("parse_duration minutes are counted"))


def test_lessons_keep_secrets_and_raw_logs_out():
    secret = "ghp_" + "A" * 30
    task = {"id": "t", "goal": f"Use token={secret} to call the API in client.py", "status": "accepted",
            "failure_log": [{"category": "test_failure", "summary": f"E   KeyError: {secret}\n" + "noise line\n" * 2000}],
            "proposal": {"plan": "read the key from the environment, never from code " + "x" * 2000,
                         "changes": [{"path": "client.py"}]},
            "test_evidence": {"passed": True, "exit_code": 0, "output": "log\n" * 5000 + "3 passed in 1.0s"}}
    item = lessons.lesson(task)
    text = lessons.text(item)
    assert secret not in json.dumps(item) and secret not in text and "[redacted]" in text
    assert len(text) <= 1500 and "noise line" not in text and len(item["failed_approaches"][0]) <= 200
    assert item["verification"]["result"] == "3 passed" and "log" not in json.dumps(item["verification"])
    unaccepted = lessons.lesson({**task, "status": "passed"})
    assert unaccepted["solution"] is None and unaccepted["verified"] is False  # only accepted work is a fix


def test_a_shortened_lesson_still_says_whether_it_was_verified():
    """The engineering packet keeps 400 characters of each lesson (brain/knowledge.py): the real
    run showed a long lesson losing its verification there, so it comes first."""
    task = {"id": "t", "goal": "Add parse_minutes(text) to duration.py " + "with details " * 40, "status": "accepted",
            "failure_log": [{"category": "collection", "summary": "E   ImportError: cannot import parse_minutes"}] * 3,
            "proposal": {"plan": "Add the function " + "carefully " * 30, "changes": [{"path": "duration.py"}]},
            "test_evidence": {"passed": True, "exit_code": 0, "output": "1 passed in 0.1s"}}
    shortened = lessons.text(lessons.lesson(task))[:400]
    assert shortened.startswith("Verified fix: tests passed (1 passed). Problem: Add parse_minutes")
    assert lessons.text(lessons.lesson({**task, "status": "blocked"}))[:400].startswith("Not solved yet (blocked)")


def test_relevance_keeps_unrelated_lessons_out_of_both_memories(project):
    assert lessons.relevant("parse_duration should accept seconds", "Problem: Fix parse_duration minutes")
    assert not lessons.relevant("Add a logging section to the README", "Problem: Fix parse_duration minutes")
    assert not lessons.relevant("", "anything")
    brain = project["context"].brain
    brain.store.remember("a", {"repository": "app", "content": "Problem: Fix parse_duration minutes. Verified"})
    brain.store.remember("b", {"repository": "app", "content": "Problem: Render the invoice PDF header. Verified"})
    assert [m["task_id"] for m in brain.store.memories("app", "parse_duration should handle seconds")] == ["a"]
    assert brain.store.memories("app", "Configure the nginx reverse proxy") == []
    assert {m["task_id"] for m in brain.store.memories("app")} == {"a", "b"}  # listing without a goal is unchanged
    memory = project["context"].memory
    memory.remember("rule", "Always run the linter before committing", verified=True, ref="user")
    memory.remember("change", "Problem: Render the invoice PDF header. Verified fix: tests passed", verified=True,
                    ref="task:x", key="change:x")
    context = memory.context_for("Configure the nginx reverse proxy")
    assert "invoice" not in json.dumps(context)  # unrelated lesson left out
    assert "linter" in json.dumps(context)  # project rules still always apply


def test_records_written_before_structured_lessons_still_work(project):
    context = project["context"]
    brain = context.brain
    # an old-format Brain memory row (summary only, no lesson) is still found when relevant
    brain.store.remember("old", {"repository": "app", "content": "Accepted locally: Fix parse_duration minutes"})
    assert [m["task_id"] for m in brain.store.memories("app", "parse_duration seconds")] == ["old"]
    # an old-format project memory record ("Accepted: ...") is still retrieved for a related goal
    context.memory.remember("change", "Accepted: Fix parse_duration minutes. Plan: count minutes", verified=True,
                            ref="task:old")
    assert "Accepted: Fix parse_duration" in json.dumps(context.memory.context_for("parse_duration seconds support"))
    # importing the Brain's records skips structured lessons (already recorded) but keeps old rows
    from brain.local.memory import _coding_brain_records
    brain.store.remember("new", {"repository": "app", "content": "Problem: x", "lesson": {"problem": "x"}})
    imported = [r["text"] for r in _coding_brain_records(context.data / "brain.sqlite3")]
    assert any("Accepted locally: Fix parse_duration" in text for text in imported)
    assert not any("Problem: x" in text for text in imported)
