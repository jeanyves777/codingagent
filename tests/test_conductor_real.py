"""The conductor with real agents: a real local model through Ollama, the real Brain and the real
Docker sandbox for both the agents' own test runs and the independent verification.

Two separate verdicts, as in test_new.py::test_real_goal_first_pipeline:
- this test (a merge gate) asserts what the conductor itself must guarantee whatever the model
  writes: both agents dispatched at once, the run settles on its own, nothing is called verified
  without passing frozen acceptance tests re-run independently, failures are retried with feedback
  or blocked with a reason, nothing is accepted for the user, the main branch is untouched and the
  ledger is intact;
- how many items the model actually got right is the capability verdict, written to
  $CODINGBRAIN_SWARM_REPORT and reported separately by CI. It is never hidden."""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from brain.conductor import Backlog, BrainExecutor, Conductor, Resources, acceptance_passed
from brain.local import cli
from brain.local import config as settings
from brain.local.paths import Layout
from test_brain import make_repository

REAL = bool(os.environ.get("CODINGBRAIN_TEST_CODING_MODEL") and os.environ.get("CODINGBRAIN_TEST_DOCKER"))

ITEMS = [
    ("slugify", "Create a module slug.py with a function slugify(text) that lowercases the text, replaces every "
                "run of characters that are not letters or digits with a single hyphen and strips hyphens from "
                "both ends. Example: slugify('Hello, World!') == 'hello-world'",
     "tests/test_slug_accept.py",
     "from slug import slugify\n\n\ndef test_slugify():\n    assert slugify('Hello, World!') == 'hello-world'\n"
     "    assert slugify('  Many   spaces ') == 'many-spaces'\n    assert slugify('A--B') == 'a-b'\n"),
    ("clamp", "Create a module bounds.py with a function clamp(value, low, high) that returns low when value "
              "is below low, high when value is above high, and value otherwise. It raises ValueError when "
              "low is greater than high",
     "tests/test_bounds_accept.py",
     "import pytest\n\nfrom bounds import clamp\n\n\ndef test_clamp():\n    assert clamp(5, 0, 10) == 5\n"
     "    assert clamp(-1, 0, 10) == 0\n    assert clamp(11, 0, 10) == 10\n    with pytest.raises(ValueError):\n"
     "        clamp(1, 5, 0)\n"),
]


def git(path, *arguments):
    return subprocess.run(["git", *arguments], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(not REAL, reason="needs CODINGBRAIN_TEST_CODING_MODEL (a real Ollama model) and "
                                     "CODINGBRAIN_TEST_DOCKER (the real sandbox)")
def test_real_agents_work_the_backlog_in_parallel_and_only_verified_work_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    layout = Layout(tmp_path / "home").ensure()
    config = settings.load(layout)
    config["models"]["model"] = os.environ["CODINGBRAIN_TEST_CODING_MODEL"]
    config["autonomy"]["execution"] = "auto"
    settings.save(layout, config)
    monkeypatch.setenv("CODINGBRAIN_HOME", str(layout.home))
    repository = make_repository(tmp_path / "projects" / "demo", use_git=True)
    head = git(repository, "rev-parse", "HEAD")
    context = cli.Context(layout, repository)
    backlog = Backlog(context.data / "backlog.sqlite3")
    for title, goal, path, content in ITEMS:
        backlog.add(goal, title=title, max_attempts=2,
                    acceptance={"files": {path: content}, "nodes": [f"{path}::test_{title}"]})
    executor = BrainExecutor(context.brain, repository.name)
    conductor = Conductor(backlog, executor, auto_execute=True, stall_seconds=900, resources=Resources(max_local=2))
    started = time.time()
    limit = float(os.environ.get("CODINGBRAIN_SWARM_MINUTES", "30")) * 60
    status = asyncio.run(conductor.run(poll=2.0, deadline=started + limit))
    seconds = round(time.time() - started)
    items, attempts, ledger = backlog.list(), backlog.attempts(), backlog.ledger()

    async def reverify():
        return {item["id"]: await executor.verify(item["task_id"], item) for item in items if item["state"] == "verified"}
    rechecked = asyncio.run(reverify())
    first = {}
    for attempt in attempts:
        first.setdefault(attempt["item_id"], attempt)
    a, b = (first.get(item["id"]) for item in items)
    from brain.local.inference import overlap
    from brain.telemetry import Telemetry
    calls = [{"first": event["at"] - event["duration"], "last": event["at"]}
             for event in Telemetry(context.data / "telemetry.sqlite3").journal(
                 [attempt["task_id"] for attempt in attempts], limit=100000)
             if event["event_type"] == "model_response" and event["duration"] and event["provider"] == "ollama"]
    capability = {"model": config["models"]["model"], "seconds": seconds, "stopped": status.get("stopped"),
                  "model_calls": len(calls), "model_calls_overlap": overlap(calls),
                  "verified": sum(item["state"] == "verified" for item in items), "items": len(items),
                  "integration": status.get("integration"),
                  "per_item": [{"title": item["title"], "state": item["state"], "attempts": item["attempts"],
                                "reason": (item["reason"] or "")[:300]} for item in items],
                  "attempts": [{key: attempt[key] for key in ("item_id", "number", "strategy", "outcome")}
                               | {"seconds": round((attempt["ended"] or time.time()) - attempt["started"])}
                               for attempt in attempts]}
    if os.environ.get("CODINGBRAIN_SWARM_REPORT"):
        Path(os.environ["CODINGBRAIN_SWARM_REPORT"]).write_text(json.dumps(capability, indent=2))
    print(json.dumps(capability, indent=2))

    # The merge gate: the conductor's own guarantees, whatever the model wrote.
    assert "stopped" not in status, f"the run did not settle within {limit / 60:.0f} minutes: {status}"
    assert status["finished"] or status["waiting_on_people_only"]
    assert a and b and a["started"] < (b["ended"] or time.time()) and b["started"] < (a["ended"] or time.time()), \
        "the two local agents did not work at the same time"
    assert status["ledger_intact"] and backlog.verify_ledger()
    for item in items:
        assert item["state"] in {"verified", "blocked"}, item  # never accepted for the user
        entries = [entry for entry in ledger if entry["item_id"] == item["id"]]
        if item["state"] == "verified":
            checks = [entry for entry in entries if entry["kind"] == "verification"]
            assert checks and checks[-1]["verdict"] == "passed", entries
            assert [entry["verdict"] for entry in entries if entry["kind"] == "integration"][-1:] == ["included"], entries
            assert acceptance_passed(rechecked[item["id"]], item["acceptance"]["nodes"]), \
                rechecked[item["id"]].get("output", "")[-800:]
            assert context.brain.store.get(item["task_id"])["status"] == "passed"
        else:
            assert item["reason"], item  # blocked always says why
            assert item["attempts"] <= 2
        retries = [attempt for attempt in attempts if attempt["item_id"] == item["id"] and attempt["number"] > 1]
        assert all(attempt["strategy"] != "local" for attempt in retries)  # a retry carries the failure as feedback
    verified = {item["id"] for item in items if item["state"] == "verified"}
    if verified:  # every verified item is also combined with the others, and the combination passed
        assert status["integration"] and set(status["integration"]["included"]) == verified, status["integration"]
        assert git(repository, "merge-base", "--is-ancestor", head, status["integration"]["head"]) == ""
    assert git(repository, "rev-parse", "HEAD") == head and git(repository, "status", "--porcelain") == ""
