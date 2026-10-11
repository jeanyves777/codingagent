"""`codingbrain backlog`: add with frozen acceptance tests, status, ledger, the run lock, and a
full `backlog run` on the real Brain (stand-in model, local pytest instead of Docker)."""
import argparse
import json
import os
import subprocess
import sys

import pytest

import brain.sandbox
import brain.service
from brain.conductor import Backlog
from brain.local import cli
from brain.local import config as settings
from brain.local.paths import Layout
from brain.service import Brain
from test_brain import make_repository
from test_conductor_brain import StandIn, acceptance, local_pytest

ACCEPT = "from alpha import VALUE\n\n\ndef test_value():\n    assert VALUE == 'alpha'\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    layout = Layout(tmp_path / "home").ensure()
    config = settings.load(layout)
    config["models"]["model"] = "stand-in"
    config["autonomy"]["execution"] = "auto"
    settings.save(layout, config)
    monkeypatch.setenv("CODINGBRAIN_HOME", str(layout.home))
    monkeypatch.setattr("brain.local.inference.cached_slots", lambda *args, **kwargs: 2)  # never probe a server
    repository = make_repository(tmp_path / "projects" / "demo", use_git=True)
    (repository / "acceptance").mkdir()
    (repository / "acceptance" / "test_alpha.py").write_text(ACCEPT)
    monkeypatch.chdir(repository)
    return {"layout": layout, "repo": repository}


def args(action, *goal, **options):
    defaults = dict(title=None, priority="P1", depends=None, accept_file=None, accept_node=None, criteria=None,
                    max_attempts=3, max_agents=2, stall_minutes=15, poll=0.05, once=False, json=False)
    return argparse.Namespace(command="backlog", action=action, goal=list(goal), **{**defaults, **options})


def test_add_freezes_acceptance_files_from_the_project_only(project, capsys):
    cli.cmd_backlog(args("add", "Create", "alpha.py", priority="P0", accept_file=["acceptance/test_alpha.py"],
                         accept_node=["acceptance/test_alpha.py::test_value"]), project["layout"])
    (project["repo"] / "acceptance" / "test_alpha.py").write_text("def test_value():\n    pass\n")  # edited later
    context = cli.Context(project["layout"], project["repo"])
    item = Backlog(context.data / "backlog.sqlite3").list()[0]
    assert item["acceptance"]["files"]["acceptance/test_alpha.py"] == ACCEPT  # the version at add time
    with pytest.raises(SystemExit, match="inside the project"):
        cli.cmd_backlog(args("add", "x", accept_file=["../../outside.py"], accept_node=["x.py::t"]), project["layout"])
    cli.cmd_backlog(args("add", "no", "tests"), project["layout"])
    assert "can never be verified" in capsys.readouterr().out


def test_status_and_ledger_are_read_only_and_json(project, capsys):
    cli.cmd_backlog(args("add", "Create alpha.py", accept_file=["acceptance/test_alpha.py"],
                         accept_node=["acceptance/test_alpha.py::test_value"]), project["layout"])
    capsys.readouterr()
    cli.cmd_backlog(args("status", json=True), project["layout"])
    status = json.loads(capsys.readouterr().out)
    assert status["counts"] == {"ready": 1} and status["agents"]["busy"] == [] and status["ledger_intact"]
    cli.cmd_backlog(args("ledger", json=True), project["layout"])
    assert json.loads(capsys.readouterr().out) == {"intact": True, "entries": []}


def test_a_second_run_is_refused_while_one_is_working(project):
    context = cli.Context(project["layout"], project["repo"])
    context.data.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (context.data / "backlog.lock").write_text(str(sleeper.pid))
        with pytest.raises(SystemExit, match="already being worked on"):
            cli.backlog_run(args("run"), context, Backlog(context.data / "backlog.sqlite3"))
    finally:
        sleeper.kill()
        sleeper.wait()


def test_backlog_run_works_through_the_items_and_stops_for_the_user(project, monkeypatch, capsys):
    monkeypatch.setattr(brain.service, "run_tests", local_pytest)
    monkeypatch.setattr(brain.sandbox, "run_tests", local_pytest)
    context = cli.Context(project["layout"], project["repo"])
    context.data.mkdir(parents=True, exist_ok=True)
    context._brain = Brain(project["repo"].parent, context.data, StandIn(), "img", workers=2)
    backlog = Backlog(context.data / "backlog.sqlite3")
    backlog.add("Create alpha.py with VALUE = 'alpha'", title="alpha", acceptance=acceptance("alpha"))
    backlog.add("Create beta.py with VALUE = 'beta' (cheat)", title="beta", acceptance=acceptance("beta"))
    assert cli.backlog_run(args("run"), context, backlog) == 0
    out = capsys.readouterr().out
    assert "verified" in out and "Waiting for you" in out
    assert {item["title"]: item["state"] for item in backlog.list()} == {"alpha": "verified", "beta": "verified"}
    assert not (context.data / "backlog.lock").exists()


def test_accepting_the_combined_result_makes_one_branch_and_dependents_start_from_it(project, monkeypatch, capsys):
    monkeypatch.setattr(brain.service, "run_tests", local_pytest)
    monkeypatch.setattr(brain.sandbox, "run_tests", local_pytest)
    context = cli.Context(project["layout"], project["repo"])
    context.data.mkdir(parents=True, exist_ok=True)
    context._brain = Brain(project["repo"].parent, context.data, StandIn(), "img", workers=2)
    backlog = Backlog(context.data / "backlog.sqlite3")
    first = backlog.add("Create alpha.py with VALUE = 'alpha'", title="alpha", acceptance=acceptance("alpha"))
    backlog.add("Create beta.py with VALUE = 'beta'", title="beta", acceptance=acceptance("beta"))
    head = subprocess.run(["git", "-C", str(project["repo"]), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert cli.backlog_run(args("run"), context, backlog) == 0
    assert "backlog accept" in capsys.readouterr().out
    candidate = backlog.integration()
    assert cli.backlog_accept(context, backlog) == 0
    out = capsys.readouterr().out
    branch = backlog.integration(candidate["id"])["branch"]
    assert branch in out and {item["state"] for item in backlog.list()} == {"done"}
    git = lambda *a: subprocess.run(["git", "-C", str(project["repo"]), *a], capture_output=True, text=True).stdout  # noqa: E731
    assert git("rev-parse", branch) == candidate["head"] + "\n" and git("rev-parse", "HEAD") == head  # current branch unchanged
    assert "VALUE = 'alpha'" in git("show", f"{branch}:alpha.py") and "VALUE = 'beta'" in git("show", f"{branch}:beta.py")
    assert [entry["verdict"] for entry in backlog.ledger() if entry["kind"] == "acceptance"] == ["accepted", "accepted"]
    with pytest.raises(SystemExit, match="Nothing to accept"):
        cli.backlog_accept(context, backlog)
    # a dependent added now builds on the accepted, combined work
    backlog.add("Create gamma.py", title="gamma", depends_on=[first["id"]], acceptance=acceptance("alpha"))
    assert cli.backlog_run(args("run", once=True), context, backlog) == 0
    task_id = backlog.list({"running", "verifying", "verified", "awaiting_approval"})[0]["task_id"]
    assert context.brain.store.get(task_id)["base_commit"] == candidate["head"]
