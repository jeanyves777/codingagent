"""The typed engine API (brain/local/engine.py): schema, validation, conversation that never
executes, projects, the governed task lifecycle with digest-bound single-use approval, providers,
and the JSON-lines transport. Models and the sandbox are fakes here."""
import io
import json
import subprocess
from pathlib import Path

import pytest

from brain.local import config as settings
from brain.local.engine import OPERATIONS, ApiError, Engine, describe, serve_stdio
from brain.local.paths import Layout
from brain.service import Brain

from installer_fakes import FakeMachine
from test_brain import Model


def git(path, *arguments):
    return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "Kkoff"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))  # new projects go to <home>/Projects: never the real home
    monkeypatch.setenv("USERPROFILE", str(home))
    layout = Layout(home / "AppData" / "Local" / "CodingBrain").ensure()
    monkeypatch.setenv("CODINGBRAIN_HOME", str(layout.home))
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test Person\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    config = settings.load(layout)
    config["models"]["model"] = "qwen2.5-coder:7b"
    config["models"]["url"] = "http://127.0.0.1:9"  # nothing listens: no real model is ever reached
    settings.save(layout, config)
    root = home / "Projects" / "calculator"
    root.mkdir(parents=True)
    (root / "main.py").write_text("x = 1\n")
    git(root.parent, "init", "-q", str(root))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    engine = Engine(layout, home, system=FakeMachine(platform="linux", programs={"git", "claude"}))
    project = engine.call("projects.register", {"path": str(root)})
    yield {"engine": engine, "layout": layout, "home": home, "root": root, "project": project}
    engine.close()


def with_fake_brain(world, monkeypatch, passing=True):
    """The registered project's service, with a scripted model and sandbox."""
    monkeypatch.setattr("brain.service.run_tests", lambda *a, **k: {
        "passed": passing, "exit_code": 0 if passing else 1, "profile": "python", "output": "1 passed" if passing else "x"})
    engine, project = world["engine"], world["project"]
    context = engine.context(project["id"])

    async def build():
        return Brain(world["root"].parent, context.data, Model(), "img")
    context._brain = engine.run(build())
    return context._brain


def test_schema_and_info():
    schema = describe()
    assert schema["api_version"] == "1.0" and set(schema["operations"]) == set(OPERATIONS)
    assert {op for op, item in schema["operations"].items() if item["kind"] == "action"} == {
        "projects.register", "projects.create", "tasks.start", "tasks.approve", "tasks.accept", "tasks.stop",
        "tasks.resume"}
    assert schema["operations"]["conversation.send"]["kind"] == "read"


def test_info_reports_versions_and_operations(world):
    info = world["engine"].call("engine.info")
    assert info["api_version"] == "1.0" and info["engine_version"] and "tasks.approve" in info["operations"]


@pytest.mark.parametrize("op,params,code", [
    ("no.such", {}, "unknown_op"),
    ("conversation.send", {}, "bad_params"),
    ("conversation.send", {"message": 3}, "bad_params"),
    ("conversation.send", {"message": "hi", "yes": True}, "bad_params"),  # there is no --yes
    ("tasks.get", {"project_id": "nope", "task_id": "x"}, "not_found"),
])
def test_validation(world, op, params, code):
    with pytest.raises(ApiError) as error:
        world["engine"].call(op, params)
    assert error.value.code == code


def test_conversation_proposes_and_never_executes(world):
    engine = world["engine"]
    hello = engine.call("conversation.send", {"message": "hello"})
    assert "Hello! I'm Coding Brain" in hello["reply"] and hello["action"] is None
    create = engine.call("conversation.send", {"message": "Create a task management application"})
    assert create["action"] == {"kind": "create_project", "goal": "Create a task management application"}
    assert create["needs"] == "confirmation"
    assert not (world["home"] / "Projects" / "task-management").exists()
    fix = engine.call("conversation.send", {"message": "Fix the calculator bug"})
    assert fix["action"]["kind"] == "task" and fix["action"]["project_id"] == world["project"]["id"]
    assert engine.call("tasks.list", {"project_id": world["project"]["id"]}) == []
    projects = engine.call("conversation.send", {"message": "what projects am I working on?"})
    assert "calculator" in projects["reply"] and projects["action"] is None
    history = engine.call("conversation.history", {})
    assert history[0] == {"role": "user", "content": "hello"}


def test_conversation_asks_which_project(world, tmp_path):
    engine = world["engine"]
    other = world["home"] / "Projects" / "recipes"
    other.mkdir()
    (other / "a.py").write_text("x = 1\n")
    git(other.parent, "init", "-q", str(other))
    git(other, "add", "-A")
    git(other, "commit", "-qm", "init")
    engine.call("projects.register", {"path": str(other)})
    answer = engine.call("conversation.send", {"message": "Add input validation"})
    assert answer["needs"] == "choose_project" and {item["name"] for item in answer["options"]} == {"calculator", "recipes"}
    assert answer["action"]["goal"] == "Add input validation"


def test_projects_list_register_and_create(world, monkeypatch):
    engine = world["engine"]
    assert [item["name"] for item in engine.call("projects.list")] == ["calculator"]
    with pytest.raises(ApiError, match="not inside a Git repository"):
        engine.call("projects.register", {"path": str(world["home"])})
    monkeypatch.setattr("brain.local.create.readiness_blockers", lambda *a, **k: ([], []))
    record = engine.call("projects.create", {"goal": "Build a command-line task manager", "name": "tasks"})
    assert Path(record["path"]).is_dir() and record["project_id"]
    assert Path(record["path"]).is_relative_to(world["home"].resolve())
    assert "tasks" in [item["name"] for item in engine.call("projects.list")]
    with pytest.raises(ApiError) as error:
        engine.call("projects.create", {"goal": "Build a command-line task manager", "name": "tasks"})
    assert error.value.code == "refused" and "already exists" in str(error.value)


def test_task_lifecycle_is_digest_bound_and_single_use(world, monkeypatch):
    engine, pid = world["engine"], world["project"]["id"]
    with_fake_brain(world, monkeypatch)
    events = []
    task = engine.call("tasks.start", {"project_id": pid, "goal": "Fix x"}, emit=events.append)
    assert task["status"] == "proposed" and task["digest"] and task["files"] == ["main.py"]
    assert git(world["root"], "status", "--porcelain") == ""  # nothing applied yet
    assert any(event["event_type"] == "stage" for event in events)  # live journal events were streamed
    with pytest.raises(ApiError, match="digest does not match"):
        engine.call("tasks.approve", {"project_id": pid, "task_id": task["id"], "digest": "0" * 64, "decision": "approve"})
    tested = engine.call("tasks.approve", {"project_id": pid, "task_id": task["id"], "digest": task["digest"],
                                           "decision": "approve"})
    assert tested["status"] == "passed" and tested["tests"]["passed"] is True
    with pytest.raises(ApiError, match="not waiting for approval"):  # the approval cannot be replayed
        engine.call("tasks.approve", {"project_id": pid, "task_id": task["id"], "digest": task["digest"],
                                      "decision": "approve"})
    accepted = engine.call("tasks.accept", {"project_id": pid, "task_id": task["id"]})
    assert accepted["status"] == "accepted" and accepted["branch"]
    assert git(world["root"], "branch", "--show-current") in {"main", "master"}  # checked-out branch unchanged
    assert (world["root"] / "main.py").read_text() == "x = 1\n"
    journal = engine.call("tasks.events", {"project_id": pid, "task_id": task["id"]})
    assert journal and all(event["task_id"] == task["id"] for event in journal)


def test_decline_and_stop(world, monkeypatch):
    engine, pid = world["engine"], world["project"]["id"]
    with_fake_brain(world, monkeypatch)
    task = engine.call("tasks.start", {"project_id": pid, "goal": "Fix x"})
    declined = engine.call("tasks.approve", {"project_id": pid, "task_id": task["id"], "digest": task["digest"],
                                             "decision": "decline"})
    assert declined["status"] in {"cancelled", "cancellation_requested"}
    with pytest.raises(ApiError):
        engine.call("tasks.approve", {"project_id": pid, "task_id": task["id"], "digest": task["digest"],
                                      "decision": "maybe"})


def test_providers_are_truthful(world):
    providers = {item["id"]: item for item in world["engine"].call("providers.list")}
    assert providers["claude"]["installed"] and providers["claude"]["readiness"] == "needs_sign_in"
    assert providers["claude"]["cost_gate"]["api_key_billing"] == "refused"
    assert providers["codex"]["readiness"] == "needs_setup" and not providers["codex"]["installed"]
    for name in ("gemini", "grok", "muse"):
        assert providers[name]["readiness"] == "not_supported" and providers[name]["supported_actions"] == []
    assert providers["ollama"]["billing_type"] == "local"
    for item in providers.values():
        assert {"installed", "authentication", "capabilities", "enabled", "readiness", "billing_type", "cost_gate",
                "last_error", "supported_actions"} <= set(item)


def test_stdio_transport(world):
    requests = "\n".join([json.dumps({"id": 1, "op": "engine.info"}), "not json",
                          json.dumps({"id": 2, "op": "nope"}),
                          json.dumps({"id": 3, "op": "conversation.send", "params": {"message": "hi"}})]) + "\n"
    out = io.StringIO()
    serve_stdio(Engine(world["layout"], world["home"]), io.StringIO(requests), out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["event"]["event_type"] == "ready"
    by_id = {line.get("id"): line for line in lines if "event" not in line}
    assert by_id[1]["ok"] and by_id[1]["result"]["api_version"] == "1.0"
    assert by_id[None]["error"]["code"] == "bad_request"
    assert by_id[2]["error"]["code"] == "unknown_op"
    assert by_id[3]["ok"] and "Hello" in by_id[3]["result"]["reply"]


def test_cli_describe(world):
    import os
    import sys
    result = subprocess.run([sys.executable, "-m", "brain.local", "api", "--describe"], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}, timeout=60)
    assert result.returncode == 0 and json.loads(result.stdout)["api_version"] == "1.0"
