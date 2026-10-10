"""The desktop driving the real engine API: the window's HTTP endpoints, the desktop core and the
engine (brain.local.engine) together. The model and sandbox are fakes; the engine, its approvals,
Git and the task store are real. The out-of-process client is tested against a real
`codingbrain api --stdio` process too."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_ui import core
from desktop_ui.engine_client import EngineClient, EngineError
from desktop_ui.server import Workspace, create_app

from test_engine import git, with_fake_brain, world  # noqa: F401  (fixtures)


class InProcess:
    """EngineClient's call signature over an in-process Engine, so the engine can use fakes."""

    def __init__(self, engine):
        self.engine = engine

    def call(self, op, params=None, on_event=None, timeout=None):
        from brain.local.engine import ApiError
        try:
            return self.engine.call(op, params or {}, emit=on_event)
        except ApiError as error:
            raise EngineError(error.code, str(error)) from error


@pytest.fixture
def tmp_path():
    """A short temporary root. Git on Windows refuses a $GIT_DIR close to MAX_PATH (260), and
    pytest's per-test folders under the runner's temp directory, plus the engine's data layout,
    reach it (CI: '$GIT_DIR' too big at about 230 characters). This is a real limit for very deep project
    folders on Windows too (documented in docs/engine-api.md); these tests are about the desktop
    and the engine working together, not about path length."""
    import shutil
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="cbd-")).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def desktop(world, monkeypatch):
    from desktop_ui import management
    client = InProcess(world["engine"])
    monkeypatch.setattr(core, "shared", lambda: client)
    monkeypatch.setattr(Workspace, "engine_client", lambda self: client)
    monkeypatch.setattr(management, "engine_capabilities", lambda cli: {
        "available": True, "version": "0.12.0", "conversation": True, "typed_api": True, "task_command": True,
        "detail": "probe"})
    monkeypatch.setattr(management, "local_ollama_models", lambda: {"running": False, "models": [], "detail": "off"})
    workspace = Workspace(home=world["home"], executable="unused")
    http = TestClient(create_app(workspace))
    headers = {"X-CodingBrain-Token": workspace.secret}
    assert http.post("/api/project", json={"path": str(world["root"])}, headers=headers).status_code == 200
    return http, headers, workspace


def wait_for(http, headers, job_id, condition, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = http.get(f"/api/jobs/{job_id}", headers=headers).json()
        if condition(state["job"]):
            return state
        time.sleep(.05)
    pytest.fail(f"job never reached the expected state: {state['job']}")


def messages(http, headers, job_id):
    return [event["message"] for event in http.get(f"/api/jobs/{job_id}", headers=headers).json()["events"]]


def test_run_task_through_the_engine_with_explicit_approvals(world, desktop, monkeypatch):
    http, headers, _ = desktop
    with_fake_brain(world, monkeypatch)
    job = http.post("/api/start", json={"message": "Fix x", "mode": "run"}, headers=headers).json()
    state = wait_for(http, headers, job["id"], lambda j: j["status"] == "approval_required")
    approval = state["job"]["approval"]
    assert approval["kind"] == "proposal" and approval["files"] == ["main.py"] and approval["digest"]
    assert git(world["root"], "status", "--porcelain") == ""  # nothing applied before approval
    assert any(event["kind"] == "stage" for event in state["events"])  # live engine events
    assert http.post(f"/api/jobs/{job['id']}/decision", json={"allow": True, "approval_id": "stale"},
                     headers=headers).status_code == 409
    assert http.post(f"/api/jobs/{job['id']}/decision", json={"allow": True, "approval_id": approval["id"]},
                     headers=headers).status_code == 200
    state = wait_for(http, headers, job["id"], lambda j: j["approval"] and j["approval"]["kind"] == "accept")
    assert state["job"]["approval"]["tests_passed"] is True
    http.post(f"/api/jobs/{job['id']}/decision", json={"allow": True, "approval_id": state["job"]["approval"]["id"]},
              headers=headers)
    wait_for(http, headers, job["id"], lambda j: j["status"] == "completed")
    assert any("Accepted on new branch codingbrain/" in text for text in messages(http, headers, job["id"]))
    assert git(world["root"], "branch", "--show-current") in {"main", "master"}
    assert (world["root"] / "main.py").read_text() == "x = 1\n"
    tasks = world["engine"].call("tasks.list", {"project_id": world["project"]["id"]})
    assert [task["status"] for task in tasks] == ["accepted"]  # the engine's own record, not the UI's


def test_declined_proposal_changes_nothing(world, desktop, monkeypatch):
    http, headers, _ = desktop
    with_fake_brain(world, monkeypatch)
    job = http.post("/api/start", json={"message": "Fix x", "mode": "run"}, headers=headers).json()
    state = wait_for(http, headers, job["id"], lambda j: j["status"] == "approval_required")
    http.post(f"/api/jobs/{job['id']}/decision", json={"allow": False, "approval_id": state["job"]["approval"]["id"]},
              headers=headers)
    wait_for(http, headers, job["id"], lambda j: j["status"] == "cancelled")
    tasks = world["engine"].call("tasks.list", {"project_id": world["project"]["id"]})
    assert [task["status"] for task in tasks] == ["cancelled"]
    assert git(world["root"], "status", "--porcelain") == ""


def test_stop_during_the_sandbox_cancels_through_the_engine(world, desktop, monkeypatch):
    http, headers, _ = desktop
    with_fake_brain(world, monkeypatch)
    started = threading.Event()

    def blocking_tests(workspace, image, should_cancel=None, only=None):
        started.set()
        for _ in range(400):
            if should_cancel and should_cancel():
                return {"passed": False, "cancelled": True, "exit_code": None, "profile": "python", "output": ""}
            time.sleep(.05)
        return {"passed": True, "exit_code": 0, "profile": "python", "output": "never stopped"}
    monkeypatch.setattr("brain.service.run_tests", blocking_tests)
    job = http.post("/api/start", json={"message": "Fix x", "mode": "run"}, headers=headers).json()
    state = wait_for(http, headers, job["id"], lambda j: j["status"] == "approval_required")
    http.post(f"/api/jobs/{job['id']}/decision", json={"allow": True, "approval_id": state["job"]["approval"]["id"]},
              headers=headers)
    assert started.wait(30)
    assert http.post(f"/api/jobs/{job['id']}/stop", headers=headers).status_code == 200
    wait_for(http, headers, job["id"], lambda j: j["status"] == "cancelled")
    tasks = world["engine"].call("tasks.list", {"project_id": world["project"]["id"]})
    assert [task["status"] for task in tasks] == ["cancelled"]
    assert git(world["root"], "status", "--porcelain") == ""


def test_chat_goes_through_the_engine_and_never_executes(world, desktop):
    http, headers, _ = desktop
    hello = http.post("/api/start", json={"message": "hello", "mode": "chat"}, headers=headers).json()
    wait_for(http, headers, hello["id"], lambda j: j["status"] == "completed")
    assert any("Hello! I'm Coding Brain" in text for text in messages(http, headers, hello["id"]))
    create = http.post("/api/start", json={"message": "Create a task management application", "mode": "chat"},
                       headers=headers).json()
    wait_for(http, headers, create["id"], lambda j: j["status"] == "completed")
    assert any("use New project to review and authorize" in text for text in messages(http, headers, create["id"]))
    assert not (world["home"] / "Projects" / "task-management").exists()
    assert world["engine"].call("tasks.list", {"project_id": world["project"]["id"]}) == []
    history = world["engine"].call("conversation.history", {"project_id": world["project"]["id"]})
    assert {"role": "user", "content": "hello"} in history  # one conversation, owned by the engine


def test_out_of_process_client_against_a_real_engine(world):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    client = EngineClient(cwd=str(world["home"]), env=env)
    try:
        info = client.call("engine.info", {}, None, 60)
        assert info["api_version"] == "1.0" and client.info["api_version"] == "1.0"
        assert "Hello! I'm Coding Brain" in client.call("conversation.send", {"message": "hello"}, None, 60)["reply"]
        with pytest.raises(EngineError) as error:  # there is no --yes
            client.call("conversation.send", {"message": "hi", "yes": True}, None, 60)
        assert error.value.code == "bad_params"
        assert [item["name"] for item in client.call("projects.list", {}, None, 60)] == ["calculator"]
        client.process.kill()  # the engine dies; the next call starts a new one from the persisted state
        client.process.wait(30)
        history = client.call("conversation.history", {}, None, 60)
        assert {"role": "user", "content": "hello"} in history
    finally:
        client.close()
