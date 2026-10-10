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
    import os
    environment = dict(os.environ)  # building a real service writes the project's settings here
    yield from _world(tmp_path, monkeypatch)
    os.environ.clear()
    os.environ.update(environment)


def _world(tmp_path, monkeypatch):
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
        "tasks.resume", "tasks.tool_decision"}
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


def stdio_session(engine, requests):
    """Run a stdio session over `requests` (lines, or callables that return a line once the test is
    ready to send it) and return every output line, parsed."""
    def reader():
        for item in requests:
            yield (item() if callable(item) else item) + "\n"
    out = io.StringIO()
    serve_stdio(engine, reader(), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_every_request_gets_exactly_one_structured_response(world):
    """Valid JSON that is not a request object, and ids of the wrong type, are answered with
    bad_request instead of being dropped (the UI would otherwise wait forever)."""
    requests = ["[]", "null", "123", '"hi"', "true", "{not json", json.dumps({"id": [1], "op": "engine.info"}),
                json.dumps({"id": True, "op": "engine.info"}), json.dumps({"id": 7, "op": "engine.info", "params": []}),
                json.dumps({"id": 8}), json.dumps({"id": "ok", "op": "engine.info", "params": None})]
    engine = Engine(world["layout"], world["home"])
    lines = [line for line in stdio_session(engine, requests) if "event" not in line]
    assert len(lines) == len(requests)
    errors = [line for line in lines if not line["ok"]]
    assert len(errors) == len(requests) - 1 and {line["error"]["code"] for line in errors} == {"bad_request"}
    assert {line["id"] for line in errors} == {None, 7, 8}
    assert [line["id"] for line in lines if line["ok"]] == ["ok"]


def test_live_events_belong_to_the_operations_task(world, monkeypatch):
    """Two tasks run at the same time in one project; each operation's feed carries only its own task."""
    import threading
    import time
    engine, pid = world["engine"], world["project"]["id"]
    with_fake_brain(world, monkeypatch)

    def slow_tests(*a, **k):
        time.sleep(1.0)
        return {"passed": True, "exit_code": 0, "profile": "python", "output": "1 passed"}
    monkeypatch.setattr("brain.service.run_tests", slow_tests)
    feeds, results = {"a": [], "b": []}, {}
    starts = {key: engine.call("tasks.start", {"project_id": pid, "goal": f"Fix {key}"}, emit=feeds[key].append)
              for key in feeds}
    for key in feeds:
        assert feeds[key] and {event["task_id"] for event in feeds[key]} == {starts[key]["id"]}
        feeds[key].clear()

    def approve(key):
        task = starts[key]
        results[key] = engine.call("tasks.approve", {"project_id": pid, "task_id": task["id"], "digest": task["digest"],
                                                     "decision": "approve"}, emit=feeds[key].append)
    threads = [threading.Thread(target=approve, args=(key,)) for key in feeds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    for key, other in (("a", "b"), ("b", "a")):
        assert results[key]["status"] == "passed"
        ids = {event["task_id"] for event in feeds[key]}
        assert ids == {starts[key]["id"]} and starts[other]["id"] not in ids
        assert any(event["event_type"] == "test_result" for event in feeds[key])


def test_stop_interrupts_a_running_task_over_the_same_stdio_session(world, monkeypatch):
    import threading
    engine, pid = world["engine"], world["project"]["id"]
    with_fake_brain(world, monkeypatch)
    started = threading.Event()

    def blocking_tests(workspace, image, should_cancel=None, only=None):
        started.set()
        for _ in range(400):  # up to 20 s; the sandbox polls should_cancel the same way
            if should_cancel and should_cancel():
                return {"passed": False, "cancelled": True, "exit_code": None, "profile": "python", "output": ""}
            threading.Event().wait(0.05)
        return {"passed": True, "exit_code": 0, "profile": "python", "output": "never stopped"}
    monkeypatch.setattr("brain.service.run_tests", blocking_tests)
    task = engine.call("tasks.start", {"project_id": pid, "goal": "Fix x"})
    approve = json.dumps({"id": "run", "op": "tasks.approve", "params": {
        "project_id": pid, "task_id": task["id"], "digest": task["digest"], "decision": "approve"}})

    def stop():
        assert started.wait(30), "the sandbox never started"
        return json.dumps({"id": "stop", "op": "tasks.stop", "params": {"project_id": pid, "task_id": task["id"]}})
    lines = stdio_session(engine, [approve, stop])
    by_id = {line["id"]: line for line in lines if "event" not in line}
    assert by_id["stop"]["ok"] and by_id["stop"]["result"]["status"] in {"cancellation_requested", "cancelled"}
    final = by_id["run"]
    assert final["ok"] and final["result"]["status"] == "cancelled"
    assert lines.index(by_id["stop"]) < lines.index(final)  # stop answered while the run was in flight
    assert git(world["root"], "status", "--porcelain") == ""


def test_restart_recovers_from_the_journal_without_duplicates(world, monkeypatch):
    """The engine process died while a task was testing: a new engine reports the task as
    interrupted (blocked, retry required), never as passed, and starts nothing on its own."""
    engine, pid, layout = world["engine"], world["project"]["id"], world["layout"]
    brain = with_fake_brain(world, monkeypatch)
    task = engine.call("tasks.start", {"project_id": pid, "goal": "Fix x"})
    dead = subprocess.Popen(["true"])
    dead.wait()
    stored = brain.store.get(task["id"])
    stored.update(status="testing", owner={**stored["owner"], "pid": dead.pid})
    brain.store.save(stored)
    engine.close()

    restarted = Engine(layout, world["home"], system=FakeMachine(platform="linux", programs={"git"}))
    try:
        recovered = restarted.call("tasks.get", {"project_id": pid, "task_id": task["id"]})
        assert recovered["status"] == "testing" and recovered["interrupted"]  # a read starts nothing
        assert recovered["tests"] is None  # never reported as passed
        assert restarted.call("tasks.events", {"project_id": pid, "task_id": task["id"]})  # the journal persists
        with pytest.raises(ApiError) as error:  # nothing is resumed or duplicated on its own
            restarted.call("tasks.resume", {"project_id": pid, "task_id": task["id"]})
        assert error.value.code == "refused"
        after = restarted.call("tasks.get", {"project_id": pid, "task_id": task["id"]})
        assert after["status"] == "blocked" and not after["interrupted"]  # the service marked it: retry required
        assert any(event["data"].get("kind") == "interrupted" or "interrupted" in event["summary"]
                   for event in restarted.call("tasks.events", {"project_id": pid, "task_id": task["id"]}))
        assert [item["id"] for item in restarted.call("tasks.list", {"project_id": pid})] == [task["id"]]
    finally:
        restarted.close()


def test_child_processes_never_touch_the_protocol_pipes(tmp_path):
    """While the reader waits on the request pipe, the engine starts a child process (as git and
    the sandbox are started). The child gets NUL as stdin and stderr as stdout: it is started at
    once (on Windows, inheriting the pipe blocked until the next request line), cannot read a
    request, and cannot write into the response stream; stray prints go to stderr too."""
    import os
    import sys
    script = tmp_path / "engine_side.py"
    script.write_text(
        "import subprocess, sys, threading\n"
        "from brain.local.engine import isolate_stdio\n"
        "reader, writer = isolate_stdio()\n"
        "lines = []\n"
        "thread = threading.Thread(target=lambda: lines.append(reader.readline()))\n"
        "thread.start()  # a read is now pending on the request pipe\n"
        "child = subprocess.run([sys.executable, '-c', 'import sys; print(\"child-out\"); "
        "print(\"stdin:\", repr(sys.stdin.read()), file=sys.stderr)'], timeout=30)\n"
        "print('stray print')\n"
        "writer.write('{\"ok\": true, \"child\": %d}\\n' % child.returncode)\n"
        "thread.join(30)\n"
        "writer.write('{\"echo\": %r}\\n' % lines[0].strip())\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    import threading
    first = []
    reading = threading.Thread(target=lambda: first.append(process.stdout.readline()), daemon=True)
    reading.start()
    reading.join(60)  # arrives before any request is sent: the child was not blocked
    if not first:
        process.kill()
        pytest.fail("starting a child process blocked on the request pipe")
    assert json.loads(first[0]) == {"ok": True, "child": 0}
    out, err = process.communicate("request-1\n", timeout=60)
    assert out.splitlines() == ["{\"echo\": 'request-1'}"]  # the child did not consume the request
    assert "child-out" in err and "stray print" in err and "stdin: ''" in err


def test_tool_decision_is_bound_to_the_pending_request(world, monkeypatch):
    engine, pid = world["engine"], world["project"]["id"]
    with_fake_brain(world, monkeypatch)
    task = engine.call("tasks.start", {"project_id": pid, "goal": "Fix x"})
    with pytest.raises(ApiError) as error:  # the task is not waiting on a tool request
        engine.call("tasks.tool_decision", {"project_id": pid, "task_id": task["id"], "request_id": "r1",
                                            "decision": "approve"})
    assert error.value.code == "refused"
    brain = engine.brain(pid)
    stored = brain.store.get(task["id"])
    stored.update(status="awaiting_tool_approval", pending_approval_id="r1")
    brain.store.save(stored)
    assert engine.call("tasks.get", {"project_id": pid, "task_id": task["id"]})["pending_tool_approval"] == "r1"
    with pytest.raises(ApiError) as error:  # another request id is refused
        engine.call("tasks.tool_decision", {"project_id": pid, "task_id": task["id"], "request_id": "r2",
                                            "decision": "approve"})
    assert error.value.code == "refused"
    with pytest.raises(ApiError) as error:
        engine.call("tasks.tool_decision", {"project_id": pid, "task_id": task["id"], "request_id": "r1",
                                            "decision": "maybe"})
    assert error.value.code == "bad_params"
