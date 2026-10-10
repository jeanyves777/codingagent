"""Live activity journal, terminal view, traces and snapshots.

A fake Ollama HTTP server (real sockets, real OllamaModel client) stands in for slow models in the
deterministic tests; the real-model test runs only when CODINGBRAIN_TEST_CODING_MODEL names a pulled
Ollama model (CI). The Docker sandbox is replaced by a recorded result except where stated.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from brain import accounting
from brain.local.live import LiveView, describe, glyphs, render_trace, trace
from brain.model import OllamaModel
from brain.service import Brain
from brain.supervision import SupervisionPolicy, SupervisorLedger
from tests.test_brain import make_repository
from tests.test_phase7 import FakeSupervisor

PROPOSAL = {"plan": "Return the sum instead of the difference, and test it.",
            "changes": [{"path": "main.py", "content": "x = 2\n"}]}


class FakeOllama:
    """An HTTP server answering /api/chat like Ollama, after a delay."""

    def __init__(self, delay=0.0, reply=None):
        self.delay, self.reply, self.calls = delay, reply or PROPOSAL, 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls += 1
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                system = next((message.get("content", "") for message in request.get("messages", [])
                               if message.get("role") == "system"), "")
                reply = {"approved": True, "reason": "The change does what the goal asks."} \
                    if "code reviewer" in system else owner.reply
                time.sleep(owner.delay)
                body = json.dumps({"model": "fake-coder:7b", "message": {"role": "assistant",
                                                                        "content": json.dumps(reply)},
                                   "prompt_eval_count": 1234, "eval_count": 56}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                body = b'{"models": [{"name": "fake-coder:7b"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fast_heartbeat(monkeypatch):
    monkeypatch.setattr(accounting, "HEARTBEAT_SECONDS", 0.2)


def passing_sandbox(monkeypatch, record=None):
    def sandbox(workspace, image, **kwargs):
        if record is not None:
            record.append(time.time())
        return {"passed": True, "exit_code": 0, "profile": "python", "output": "==== 1 passed in 0.01s ===="}
    monkeypatch.setattr("brain.service.run_tests", sandbox)


def make_brain(tmp_path, model, **options):
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    return Brain(repository.parent, tmp_path / "data", model, "img", **options)


def kinds(events, event_type=None):
    return [(event["phase"], event["status"]) for event in events if event_type in (None, event["event_type"])]


# 1-2. Progress before completion; heartbeats during a slow model call ---------------------------

class ObservingModel:
    """Checks, from inside the model call, what the journal already holds."""

    def __init__(self, brain_holder):
        self.holder, self.seen = brain_holder, None

    async def propose(self, root, goal, memories, repository_context=None, **kwargs):
        brain = self.holder[0]
        self.seen = brain.telemetry.journal(limit=1000)
        return json.dumps(PROPOSAL)

    async def review(self, goal, diff):
        return {"approved": True, "reason": "fine"}


def test_progress_is_persisted_before_the_task_finishes(tmp_path):
    holder = []
    model = ObservingModel(holder)
    brain = make_brain(tmp_path, model)
    holder.append(brain)

    async def flow():
        return await brain.create(brain.submit("demo", "Set x to 2", launch=False))
    asyncio.run(flow())
    stages = kinds(model.seen, "stage")
    assert ("goal", "COMPLETED") in stages
    assert ("project_validation", "COMPLETED") in stages and ("indexing", "COMPLETED") in stages
    assert ("proposal", "RUNNING") in stages and ("proposal", "COMPLETED") not in stages  # still running
    assert [event["seq"] for event in model.seen] == sorted(event["seq"] for event in model.seen)


def test_slow_model_requests_send_heartbeats_and_report_tokens(tmp_path, fast_heartbeat):
    server = FakeOllama(delay=1.0)
    try:
        brain = make_brain(tmp_path, OllamaModel(server.url, "fake-coder:7b"))
        stream = io.StringIO()
        view = LiveView(brain.telemetry, "plain", stream=stream, heartbeat=0.2)
        with view:
            asyncio.run(brain.create(brain.submit("demo", "Set x to 2", launch=False)))
            time.sleep(0.3)
            mid = view.status()
        events = brain.telemetry.journal(limit=1000)
        beats = [event for event in events if event["event_type"] == "heartbeat"]
        assert len(beats) >= 2 and all(event["agent"] == "implementer" for event in beats)
        request = next(event for event in events if event["event_type"] == "model_request")
        response = next(event for event in events if event["event_type"] == "model_response")
        assert request["model"] == "fake-coder:7b" and response["duration"] >= 1.0
        inference = next(event for event in events if event["event_type"] == "inference")
        assert "1234 in" in inference["summary"] and "56 out" in inference["summary"]
        output = stream.getvalue()
        assert "Implementer — Generating the proposed changes" in output
        assert "explanation (model-provided): Return the sum" in output and "files: main.py" in output
        assert "Waiting for approval" in output
        assert mid is None  # nothing in flight after completion
    finally:
        server.close()


def test_status_line_distinguishes_an_active_request_from_a_stalled_one(tmp_path):
    from brain.telemetry import Telemetry
    telemetry = Telemetry(tmp_path / "t.sqlite3")
    view = LiveView(telemetry, "plain", stream=io.StringIO(), heartbeat=1)
    now = time.time()
    view.emit({"seq": 1, "event_type": "model_request", "at": now - 30, "agent": "implementer",
               "model": "qwen2.5-coder:7b", "phase": "requirements", "status": "RUNNING", "data": {"role": "implementer"},
               "summary": ""})
    view.emit({"seq": 2, "event_type": "heartbeat", "at": now - 0.5, "agent": "implementer", "model": "qwen2.5-coder:7b",
               "phase": "requirements", "status": "RUNNING", "data": {"role": "implementer"}, "summary": ""})
    active = view.status()
    assert "working on requirement and test generation" in active and "00:30 elapsed" in active
    assert "stalled" not in active
    view.active[("implementer", "qwen2.5-coder:7b", "implementer")]["beat"] = now - 60
    assert "no heartbeat: the request may be stalled" in view.status()


# 3. Supervisor attribution --------------------------------------------------------------------

def test_supervisor_handoffs_are_attributed_and_never_assumed(tmp_path, monkeypatch):
    passing_sandbox(monkeypatch)
    plan = {"plan": "Change main.py so x is 2; keep the test.", "steps": ["edit main.py"], "risks": []}
    claude = FakeSupervisor("claude", reply=plan)
    policy = SupervisionPolicy([claude], SupervisorLedger(tmp_path / "ledger.sqlite3"), plan_budget=1, daily_limit=5)
    model = ObservingModel([None])
    brain = make_brain(tmp_path, model, supervision=policy)
    model.holder[0] = brain
    asyncio.run(brain.create(brain.submit("demo", "Set x to 2", launch=False, premium_plan=True)))
    events = brain.telemetry.journal(limit=1000)
    selected = [event for event in events if event["event_type"] == "supervisor_selected"]
    assert selected and "claude selected for plan" in selected[0]["summary"] and "left today" in selected[0]["summary"]
    decision = next(event for event in events if event["event_type"] == "decision" and event["agent"] == "supervisor")
    assert decision["phase"] == "planning" and "claude: Change main.py" in decision["summary"]
    assert decision["data"]["source"] == "model-provided"
    assert ("planning", "COMPLETED") in kinds(events, "stage")
    # Without premium planning nothing is attributed to a supervisor, even when one is configured.
    other = make_brain(tmp_path / "b", ObservingModel([None]), supervision=policy)
    other.model.holder[0] = other
    asyncio.run(other.create(other.submit("demo", "Set x to 2", launch=False)))
    assert not [event for event in other.telemetry.journal(limit=1000) if event["agent"] == "supervisor"]


# 4. No duplicates after retry or resume ---------------------------------------------------------

def test_milestones_are_not_duplicated_by_replays(tmp_path):
    brain = make_brain(tmp_path, ObservingModel([None]))
    brain.model.holder[0] = brain
    task = brain.submit("demo", "Set x to 2", launch=False)
    task = asyncio.run(brain.create(task))
    for _ in range(3):  # a replayed approval request or goal event (crash, resume) is ignored
        brain.journal_event(task, "approval", phase="approval", status="WAITING_APPROVAL",
                            dedupe=f"{task['id']}:approval:{task['digest']}")
        brain.journal_event(task, "stage", phase="goal", status="COMPLETED", dedupe=f"{task['id']}:goal")
    events = brain.telemetry.journal([task["id"]], limit=1000)
    assert sum(1 for event in events if event["event_type"] == "approval") == 1
    assert sum(1 for event in events if event["phase"] == "goal") == 1
    assert len({event["event_id"] for event in events}) == len(events)


# 5. Inspection never interferes ----------------------------------------------------------------

def test_activity_trace_and_watch_are_read_only_while_a_task_runs(tmp_path, monkeypatch):
    from brain.local import cli
    from brain.local import config as settings
    from brain.local.paths import Layout
    monkeypatch.setattr(os, "environ", dict(os.environ))
    layout = Layout(tmp_path / "home").ensure()
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "m"}})
    repo = make_repository(tmp_path / "projects" / "demo", use_git=True)
    context = cli.Context(layout, repo)
    brain = context.brain
    task = brain.submit("demo", "Long task", launch=False)
    task["status"] = "running"
    runner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        task["owner"] = {"pid": runner.pid, "host": brain.owner["host"]}
        brain.store.save(task)
        with brain.stage(task, "proposal", agent="implementer"):
            pass
        env = {**os.environ, "CODINGBRAIN_HOME": str(layout.home)}
        for command in (["activity"], ["activity", "--json"], ["trace", task["id"][:8]], ["snapshots", task["id"][:8]]):
            done = subprocess.run([sys.executable, "-m", "brain.local", *command], capture_output=True, text=True,
                                  env=env, cwd=str(repo), timeout=120)
            assert done.returncode == 0, (command, done.stderr)
        assert "running" in subprocess.run([sys.executable, "-m", "brain.local", "activity"], capture_output=True,
                                           text=True, env=env, cwd=str(repo), timeout=120).stdout
        assert brain.store.get(task["id"])["status"] == "running"
        assert not [event for event in brain.telemetry.journal([task["id"]], limit=1000)
                    if event["event_type"] == "task_event" and "interrupted" in event["summary"]]
    finally:
        runner.kill()
        runner.wait()


# 6. Parallel assignments --------------------------------------------------------------------------

def test_parallel_assignments_are_tracked_per_task(tmp_path):
    brain = make_brain(tmp_path, ObservingModel([None]))
    brain.model.holder[0] = brain
    group = {"id": "g" * 32, "kind": "orchestration", "goal": "two things", "trace_id": "t", "status": "active"}
    first = brain.submit("demo", "Set x to 2", parent_id=group["id"], name="a", launch=False)
    second = brain.submit("demo", "Set x to 3", parent_id=group["id"], name="b", launch=False)

    async def both():
        await asyncio.gather(brain.create(first), brain.create(second))
    asyncio.run(both())
    events = brain.telemetry.journal([first["id"], second["id"]], limit=1000)
    assert {event["parent_task_id"] for event in events} == {group["id"]}
    report = trace(brain.telemetry, group, [brain.store.get(first["id"]), brain.store.get(second["id"])])
    assert {item["task"] for item in report["stages"]} == {first["id"][:8], second["id"][:8]}


# 7-8. Snapshots -------------------------------------------------------------------------------------

def run_to_passed(tmp_path, monkeypatch, model=None, **options):
    passing_sandbox(monkeypatch)
    model = model or ObservingModel([None])
    brain = make_brain(tmp_path, model, **options)
    if hasattr(model, "holder"):
        model.holder[0] = brain

    async def flow():
        task = await brain.create(brain.submit("demo", "Set x to 2", launch=False))
        return await brain.execute(task["id"], task["digest"])
    return brain, asyncio.run(flow())


def test_snapshots_capture_each_boundary_and_compare(tmp_path, monkeypatch):
    brain, task = run_to_passed(tmp_path, monkeypatch)
    labels = [item["label"] for item in brain.snapshots.list(task["id"])]
    assert labels == ["initial", "after_proposal", "before_changes", "after_changes", "before_tests", "after_tests",
                      "before_acceptance"]
    snapshots = {item["label"]: item["id"] for item in brain.snapshots.list(task["id"])}
    initial, tested = brain.snapshots.show(snapshots["initial"]), brain.snapshots.show(snapshots["after_tests"])
    assert initial["manifest"] == [] and tested["manifest"] == [{"path": "main.py", "change": "modified",
                                                                 "sha256": tested["manifest"][0]["sha256"]}]
    assert tested["test_evidence"]["passed"] and tested["resume"]["command"].startswith("codingbrain resume")
    assert "+x = 2" in brain.snapshots.get(tested["diff"]).decode()
    comparison = brain.snapshots.diff(snapshots["initial"], snapshots["after_tests"])
    assert comparison["files"][0]["path"] == "main.py" and comparison["state"]["label"]["b"] == "after_tests"
    objects = list((tmp_path / "data" / "snapshots" / "objects").rglob("*"))
    assert len([path for path in objects if path.is_file()]) == 2  # one file body + one diff: deduplicated
    report = trace(brain.telemetry, task, snapshots=brain.snapshots.list(task["id"]))
    text = render_trace(report)
    assert "Sandbox execution" in text and "passed (exit 0): 1 passed in 0.01s" in text and "after_tests" in text


def test_restoring_a_snapshot_needs_approval_and_never_touches_the_working_tree(tmp_path, monkeypatch):
    from brain.local import cli
    from brain.local import config as settings
    from brain.local.paths import Layout
    monkeypatch.setattr(os, "environ", dict(os.environ))
    passing_sandbox(monkeypatch)
    layout = Layout(tmp_path / "home").ensure()
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "m"}})
    repo = make_repository(tmp_path / "projects" / "demo", use_git=True)
    context = cli.Context(layout, repo)
    context._brain = Brain(repo.parent, context.data, ObservingModel([None]), "img")
    context._brain.model.holder[0] = context._brain

    async def flow():
        task = await context._brain.create(context._brain.submit("demo", "Set x to 2", launch=False))
        return await context._brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    snapshot = next(item for item in context._brain.snapshots.list(task["id"]) if item["label"] == "after_tests")
    before = (repo / "main.py").read_text()
    env = {**os.environ, "CODINGBRAIN_HOME": str(layout.home)}
    refused = subprocess.run([sys.executable, "-m", "brain.local", "snapshot", "restore", snapshot["id"]],
                             capture_output=True, text=True, env=env, cwd=str(repo), stdin=subprocess.DEVNULL)
    assert refused.returncode == 1 and "Nothing restored" in refused.stdout
    done = subprocess.run([sys.executable, "-m", "brain.local", "snapshot", "restore", snapshot["id"], "--yes"],
                          capture_output=True, text=True, env=env, cwd=str(repo))
    assert done.returncode == 0, done.stderr
    branch = f"codingbrain/restore-{snapshot['id'][:9]}"
    shown = subprocess.run(["git", "-C", str(repo), "show", f"{branch}:main.py"], capture_output=True, text=True)
    assert shown.stdout == "x = 2\n" and (repo / "main.py").read_text() == before
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


# 9. Visual evidence --------------------------------------------------------------------------------

def test_visual_screenshots_are_linked_to_the_task(tmp_path, monkeypatch):
    from tests import multimodal_fixtures as fx
    shots = [fx.ui_image(tmp_path / "desktop.png"), fx.ui_image(tmp_path / "mobile.png", label="Pay")]

    class Verifier:
        async def verify(self, task, workspace):
            return {"status": "passed", "blocking": 0, "digest": "ok", "statement": "No blocking findings",
                    "findings": [], "viewports": ["desktop", "mobile"], "screenshots": [str(path) for path in shots]}
    passing_sandbox(monkeypatch)
    model = ObservingModel([None])
    brain = make_brain(tmp_path, model)
    model.holder[0] = brain
    brain.visual_verifier = lambda task: Verifier()

    async def flow():
        task = await brain.create(brain.submit("demo", "Match the design", launch=False, visual={"enabled": True}))
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    snapshot = next(item for item in brain.snapshots.list(task["id"]) if item["label"] == "after_visual")
    artifacts = brain.snapshots.show(snapshot["id"])["artifacts"]
    assert [(item["viewport"], item["kind"]) for item in artifacts] == [("desktop", "screenshot"), ("mobile", "screenshot")]
    assert brain.snapshots.get(artifacts[1]["sha256"]) == shots[1].read_bytes()
    events = brain.telemetry.journal([task["id"]], limit=1000)
    assert ("visual_verification", "COMPLETED") in kinds(events, "stage")


# 10. Redaction ----------------------------------------------------------------------------------------

def test_secrets_are_redacted_from_journal_and_snapshots(tmp_path, monkeypatch):
    secret = "sk-live-AbCdEf0123456789ZyXwVuTs"

    class LeakyModel(ObservingModel):
        async def propose(self, root, goal, memories, repository_context=None, **kwargs):
            return json.dumps({"plan": f"Use the key {secret} and password: hunter2",
                               "changes": [{"path": "main.py", "content": "x = 2\n"}]})
    brain, task = run_to_passed(tmp_path, monkeypatch, model=LeakyModel([None]))
    journal = json.dumps(brain.telemetry.journal(limit=10000))
    snapshots = json.dumps([brain.snapshots.show(item["id"]) for item in brain.snapshots.list(task["id"])])
    for text in (journal, snapshots):
        assert secret not in text and "hunter2" not in text and "[redacted]" in text
    stored = persisted(tmp_path / "data" / "snapshots")  # the objects and the index, not only show()
    assert secret.encode() not in stored and b"hunter2" not in stored


# 11-13. Terminal output: legacy code pages, no TTY, visible failures ------------------------------------

def test_output_on_a_legacy_windows_code_page_and_without_a_tty(tmp_path):
    from brain.telemetry import Telemetry
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    assert glyphs(stream)["COMPLETED"] == "+"
    telemetry = Telemetry(tmp_path / "t.sqlite3")
    view = LiveView(telemetry, "plain", stream=stream)
    view.emit({"seq": 1, "event_type": "stage", "at": time.time(), "agent": "implementer", "model": "qwen2.5-coder:7b",
               "phase": "proposal", "status": "RUNNING", "summary": "Generating the proposed changes — naïve ✓",
               "data": {}})
    stream.flush()
    text = stream.buffer.getvalue().decode("cp1252")
    assert "> Implementer (qwen2.5-coder:7b)" in text  # no UnicodeEncodeError on cp1252


def test_failures_are_visible(tmp_path):
    class Broken:
        async def propose(self, *args, **kwargs):
            raise RuntimeError("model crashed")

        async def review(self, goal, diff):
            return {"approved": True, "reason": ""}
    brain = make_brain(tmp_path, Broken())
    with pytest.raises(RuntimeError):
        asyncio.run(brain.create(brain.submit("demo", "Set x to 2", launch=False)))
    events = brain.telemetry.journal(limit=1000)
    failed = [event for event in events if event["status"] == "FAILED"]
    assert failed and "model crashed" in failed[0]["summary"]
    assert "x" in [glyphs(io.StringIO())["FAILED"], "x"]
    lines = [describe(event) for event in events]
    assert any(line and "model crashed" in line for line in lines)
    blocked = [event for event in events if event["status"] == "BLOCKED"]
    assert blocked  # the task's blocked state is reported too


def test_cli_run_shows_live_activity_without_a_tty(tmp_path):
    """The real CLI, a real (fake) Ollama server over HTTP, output piped: plain lines in order."""
    server = FakeOllama(delay=0.6)
    try:
        home = tmp_path / "home"
        repo = make_repository(tmp_path / "projects" / "demo", use_git=True)
        env = {**os.environ, "CODINGBRAIN_HOME": str(home), "BRAIN_HEARTBEAT_SECONDS": "0.2",
               "PYTHONIOENCODING": "cp1252"}
        setup = subprocess.run([sys.executable, "-m", "brain.local", "setup", "--non-interactive", "--url", server.url,
                                "--model", "fake-coder:7b", "--no-enable-claude", "--no-enable-codex"],
                               capture_output=True, text=True, env=env, cwd=str(repo))
        assert setup.returncode == 0, setup.stderr
        config = json.loads((home / "config" / "config.json").read_text())
        config["autonomy"]["requirement_checks"] = False
        config["sandbox"]["python_image"] = "coding-brain-sandbox-missing:0"  # never start containers here
        (home / "config" / "config.json").write_text(json.dumps(config))
        run = subprocess.run([sys.executable, "-m", "brain.local", "run", "Set x to 2", "--yes"],
                             capture_output=True, env=env, cwd=str(repo), timeout=300, stdin=subprocess.DEVNULL)
        output = run.stdout.decode("cp1252")
        order = ["live activity", "Project and Git validation", "Repository indexing", "served by fake-coder:7b",
                 "Implementer (fake-coder:7b) explanation (model-provided): Return the sum", "Waiting for approval", "Code review",
                 "Sandbox execution"]
        positions = [output.find(marker) for marker in order]
        assert all(position >= 0 for position in positions), (positions, output[-3000:])
        assert positions == sorted(positions), output[-3000:]
        trace_output = subprocess.run([sys.executable, "-m", "brain.local", "trace"], capture_output=True, text=True,
                                      env=env, cwd=str(repo)).stdout
        import re
        usage = re.search(r"fake-coder:7b \(ollama\): (\d+) call\(s\), (\d+) in / (\d+) out; roles (.*)", trace_output)
        assert usage, trace_output  # tokens add up per call, however many calls (a sandbox error may add a repair)
        calls = int(usage.group(1))
        assert calls >= 2 and int(usage.group(2)) == 1234 * calls and int(usage.group(3)) == 56 * calls
        assert "implementer" in usage.group(4) and "reviewer" in usage.group(4)
        (tmp_path / "transcript.txt").write_text(output)
        if os.environ.get("CODINGBRAIN_TRANSCRIPTS"):  # CI keeps the captured terminal output as evidence
            folder = Path(os.environ["CODINGBRAIN_TRANSCRIPTS"])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"run-{sys.platform}.txt").write_text(output, encoding="utf-8")
            (folder / f"trace-{sys.platform}.txt").write_text(trace_output, encoding="utf-8")
    finally:
        server.close()


# Real model (CI) ------------------------------------------------------------------------------------------

REAL_CODER = os.environ.get("CODINGBRAIN_TEST_CODING_MODEL")


@pytest.mark.skipif(not REAL_CODER, reason="set CODINGBRAIN_TEST_CODING_MODEL to a pulled Ollama model")
def test_real_ollama_request_is_journaled_with_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(accounting, "HEARTBEAT_SECONDS", 2.0)
    url = os.environ.get("CODINGBRAIN_TEST_OLLAMA_URL", "http://localhost:11434")
    brain = make_brain(tmp_path, OllamaModel(url, REAL_CODER, max_tool_rounds=2, max_output_tokens=512))
    started = time.time()
    asyncio.run(brain.create(brain.submit("demo", "Set x to 2 in main.py", launch=False)))
    events = brain.telemetry.journal(limit=10000)
    inference = [event for event in events if event["event_type"] == "inference"]
    beats = [event for event in events if event["event_type"] == "heartbeat"]
    print("REAL ACTIVITY", REAL_CODER, f"{time.time() - started:.0f}s", len(beats), "heartbeats;",
          [event["summary"] for event in inference])
    print(render_trace(trace(brain.telemetry, brain.store.tasks()[0])))
    assert inference and all(event["model"] for event in inference)
    assert any("in," in event["summary"] or " in" in event["summary"] for event in inference)
    assert ("proposal", "COMPLETED") in kinds(events, "stage") or ("proposal", "FAILED") in kinds(events, "stage")


def test_docker_errors_are_sandbox_problems_not_repairs(tmp_path, monkeypatch):
    def broken_sandbox(workspace, image, **kwargs):
        return {"passed": False, "exit_code": 1, "profile": "python",
                "output": "docker: Error response from daemon: No such image: coding-brain-sandbox:0.1"}
    monkeypatch.setattr("brain.service.run_tests", broken_sandbox)
    model = ObservingModel([None])
    brain = make_brain(tmp_path, model)
    model.holder[0] = brain

    async def flow():
        task = await brain.create(brain.submit("demo", "Set x to 2", launch=False))
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    assert task["status"] == "failed" and not task.get("failure_log")  # no repair spent on a Docker error
    assert not [event for event in brain.telemetry.journal(limit=1000) if event["phase"] == "repair"]


# 14. Snapshot storage safety: links, junctions, secrets, sensitive tasks ------------------------------

def git_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for command in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(workspace), *command], check=True)
    (workspace / "main.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "base"], check=True)
    base = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    return workspace, base


def persisted(store_root: Path) -> bytes:
    """Every byte the snapshot store has written: objects, index and anything else."""
    return b"".join(path.read_bytes() for path in store_root.rglob("*") if path.is_file())


def git_status(workspace: Path) -> str:
    return subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "-uall"], capture_output=True,
                          text=True, check=True).stdout


def watch_reads(monkeypatch):
    """Record every path the code under test opens or reads."""
    opened = []
    real_open, real_read_bytes, real_read_text = os.open, Path.read_bytes, Path.read_text
    monkeypatch.setattr(os, "open", lambda path, *a, **k: opened.append(os.path.realpath(path)) or real_open(path, *a, **k))
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(os.path.realpath(self)) or real_read_bytes(self))
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: opened.append(os.path.realpath(self)) or
                        real_read_text(self, *a, **k))
    return opened


@pytest.mark.parametrize("git", [True, False], ids=["git", "baseline-copy"])
def test_snapshot_never_reads_a_link_to_a_file_or_folder_outside(tmp_path, monkeypatch, git):
    from brain.snapshots import SnapshotStore
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE-SENTINEL-7731")
    if git:
        workspace, base = git_workspace(tmp_path)
    else:  # a snapshot workspace: compared with the baseline copy next to it
        workspace, base = tmp_path / "task" / "workspace", None
        workspace.mkdir(parents=True)
        (tmp_path / "task" / "baseline").mkdir()
        (tmp_path / "task" / "baseline" / "main.py").write_text("x = 1\n")
        (workspace / "main.py").write_text("x = 1\n")
    try:
        os.symlink(outside / "secret.txt", workspace / "link.txt")
        os.symlink(outside, workspace / "linked-folder", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symbolic links needs a privilege on this system (junctions are tested separately)")
    (workspace / "main.py").write_text("x = 2\n")
    status = git_status(workspace) if git else None
    store = SnapshotStore(tmp_path / "store")
    opened = watch_reads(monkeypatch)
    snapshot = store.show(store.capture({"id": "t1", "base_commit": base}, workspace, "after_changes"))
    monkeypatch.undo()
    manifest = {item["path"]: item for item in snapshot["manifest"]}
    assert manifest["link.txt"]["excluded"] == "symbolic link or junction" and "sha256" not in manifest["link.txt"]
    assert not any(path.startswith("linked-folder/") and "sha256" in item for path, item in manifest.items())
    assert "sha256" in manifest["main.py"] and "+x = 2" in store.get(snapshot["diff"]).decode()
    assert not any(path.startswith(os.path.realpath(outside)) for path in opened)  # never opened, not just not kept
    assert b"OUTSIDE-SENTINEL" not in persisted(tmp_path / "store")
    # Refusing the read changed nothing in the user's workspace.
    assert os.readlink(workspace / "link.txt") == str(outside / "secret.txt")
    assert (outside / "secret.txt").read_text() == "OUTSIDE-SENTINEL-7731"
    if git:
        assert git_status(workspace) == status


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are a Windows feature")
def test_snapshot_windows_junction_in_the_workspace_is_never_followed(tmp_path, monkeypatch):
    """A directory junction (no Developer Mode needed) inside the task workspace that points outside
    must not be read through, on every Windows Python including 3.11 (no Path.is_junction): neither
    from Git's status nor from the baseline-copy walk."""
    from brain.snapshots import SnapshotStore
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("JUNCTION-SENTINEL-4419")
    workspace, base = git_workspace(tmp_path)
    copy = tmp_path / "task" / "workspace"
    copy.mkdir(parents=True)
    (tmp_path / "task" / "baseline").mkdir()
    for root in (workspace, copy):
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "vendor"), str(outside)],
                              capture_output=True, text=True)
        assert made.returncode == 0, made.stdout + made.stderr
        assert os.lstat(root / "vendor").st_file_attributes & 0x400  # it really is a reparse point
    status = git_status(workspace)
    store = SnapshotStore(tmp_path / "store")
    opened = watch_reads(monkeypatch)
    snapshots = [store.show(store.capture({"id": "t1", "base_commit": base}, workspace, "after_changes")),
                 store.show(store.capture({"id": "t2"}, copy, "after_changes"))]
    monkeypatch.undo()
    for snapshot in snapshots:
        assert not any("sha256" in item for item in snapshot["manifest"] if item["path"].startswith("vendor"))
    assert not any(path.lower().startswith(os.path.realpath(outside).lower()) for path in opened)
    assert b"JUNCTION-SENTINEL" not in persisted(tmp_path / "store")
    assert git_status(workspace) == status and (outside / "data.txt").read_text() == "JUNCTION-SENTINEL-4419"


def test_snapshot_stores_no_secret_file_secret_content_or_non_image_artifact(tmp_path):
    from brain.snapshots import SnapshotStore
    key, token = "sk-live-AbCdEf0123456789ZyXwVuTs", "plain-token-998877"
    workspace, base = git_workspace(tmp_path)
    (workspace / ".env").write_text(f"OPENAI_API_KEY={key}\n")
    (workspace / "credentials.json").write_text(f'{{"token": "{token}"}}')
    (workspace / "settings.py").write_text(f'API_KEY = "{key}"\n')
    (workspace / "app.yaml").write_text("database:\n  password: hunter2hunter2\n")
    (workspace / "auth.py").write_text('token = request.headers.get("Authorization")\n')  # ordinary code
    (workspace / "main.py").write_text("x = 2\n")
    leak, image = tmp_path / "leak.png", tmp_path / "desktop.png"
    leak.write_text(f"not really an image {key}")
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    store = SnapshotStore(tmp_path / "store")
    snapshot = store.show(store.capture({"id": "t1", "base_commit": base}, workspace, "after_changes", artifacts=[
        {"path": str(leak), "kind": "screenshot", "viewport": "mobile"},
        {"path": str(image), "kind": "screenshot", "viewport": "desktop"}]))
    manifest = {item["path"]: item for item in snapshot["manifest"]}
    assert manifest[".env"]["excluded"] == manifest["credentials.json"]["excluded"] == "secret-like file name"
    assert manifest["settings.py"]["excluded"] == manifest["app.yaml"]["excluded"] == "contains secret-like content"
    assert all("sha256" not in manifest[path] for path in (".env", "credentials.json", "settings.py", "app.yaml"))
    assert store.get(manifest["auth.py"]["sha256"]) == (workspace / "auth.py").read_bytes()
    diff = store.get(snapshot["diff"]).decode()
    assert "+x = 2" in diff and ".env" not in diff and "settings.py" not in diff
    artifacts = {item["viewport"]: item for item in snapshot["artifacts"]}
    assert artifacts["mobile"]["excluded"] == "not an image file" and "sha256" not in artifacts["mobile"]
    assert store.get(artifacts["desktop"]["sha256"]) == image.read_bytes()
    stored = persisted(tmp_path / "store")
    assert key.encode() not in stored and token.encode() not in stored and b"OPENAI_API_KEY" not in stored
    assert b"hunter2hunter2" not in stored


def test_sensitive_task_snapshots_keep_paths_only(tmp_path):
    from brain.snapshots import SnapshotStore
    workspace, base = git_workspace(tmp_path)
    (workspace / "main.py").write_text("x = 2  # SENSITIVE-BODY\n")
    image = tmp_path / "desktop.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    store = SnapshotStore(tmp_path / "store")
    snapshot = store.show(store.capture({"id": "t1", "base_commit": base, "attachments_sensitive": True}, workspace,
                                        "after_changes", artifacts=[{"path": str(image), "kind": "screenshot"}]))
    assert snapshot["manifest"] == [{"path": "main.py", "change": "modified",
                                     "excluded": "sensitive task: content is not kept"}]
    assert snapshot["diff"] is None and "sha256" not in snapshot["artifacts"][0]
    assert not [path for path in (tmp_path / "store" / "objects").rglob("*") if path.is_file()]
    assert b"SENSITIVE-BODY" not in persisted(tmp_path / "store")


def test_restore_writes_stored_files_and_leaves_excluded_ones_at_baseline(tmp_path):
    from brain.snapshots import SnapshotStore
    workspace, base = git_workspace(tmp_path)
    (workspace / "main.py").write_text("x = 2\n")
    (workspace / "settings.py").write_text('API_KEY = "sk-live-AbCdEf0123456789ZyXwVuTs"\n')
    store = SnapshotStore(tmp_path / "store")
    snapshot_id = store.capture({"id": "t1", "base_commit": base}, workspace, "after_changes")
    status = git_status(workspace)
    commit = store.restore(snapshot_id, workspace, "codingbrain/restore-test")
    files = subprocess.run(["git", "-C", str(workspace), "ls-tree", "--name-only", commit], capture_output=True,
                           text=True, check=True).stdout.split()
    shown = subprocess.run(["git", "-C", str(workspace), "show", f"{commit}:main.py"], capture_output=True,
                           text=True, check=True).stdout
    assert shown == "x = 2\n" and "settings.py" not in files  # never written as a redacted copy
    assert git_status(workspace) == status  # the working tree is untouched
