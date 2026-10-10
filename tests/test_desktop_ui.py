"""Security and UI bridge smoke tests. No real AI model or project mutation."""
from pathlib import Path
import os
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from desktop_ui.server import Workspace, StartRequest, create_app


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "sample-project"
    root.mkdir()
    (root / "hello.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_hello.py").write_text("def test_hello(): assert True\n")
    (root / ".env").write_text("SECRET_TOKEN=must-not-leak")
    (root / ".env.development").write_text("ANOTHER_SECRET=hidden")
    (root / "tls.key").write_text("SECRET_PRIVATE_KEY")
    (root / "big.txt").write_text("x" * 129000)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "hello.py"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=ui@example.test", "-c", "user.name=UI Test", "commit", "-qm", "init"], check=True)
    return root


@pytest.fixture
def client(tmp_path):
    w = Workspace(home=tmp_path, executable="not-a-real-binary")
    app = create_app(w)
    return TestClient(app), w


def headers(w):
    return {"X-CodingBrain-Token": w.secret}


def test_server_bootstrap_starts_without_core(tmp_path):
    client, w = TestClient(create_app(Workspace(home=tmp_path))), None
    assert client.get("/").status_code == 200
    assert client.get("/app.css").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert "Coding Brain" in client.get("/").text


def test_mutating_endpoints_need_token(client):
    api, w = client
    assert api.get("/api/state").status_code == 403
    assert api.post("/api/project", json={"path": "/"}).status_code == 403
    assert api.post("/api/start", json={"message": "hello", "mode": "chat"}).status_code == 403
    assert api.get("/api/state", headers=headers(w)).status_code == 200


def test_select_and_browse_files(client, sandbox):
    api, w = client
    result = api.post("/api/project", headers=headers(w), json={"path": str(sandbox)})
    assert result.status_code == 200
    assert result.json()["name"] == "sample-project"
    tree = api.get("/api/tree", headers=headers(w)).json()["entries"]
    assert [x["name"] for x in tree] == ["tests", "big.txt", "hello.py"]
    hello = api.get("/api/file", headers=headers(w), params={"path":"hello.py"})
    assert "return 'hi'" in hello.json()["content"]
    assert api.get("/api/file", headers=headers(w), params={"path":".env"}).status_code == 400
    assert api.get("/api/file", headers=headers(w), params={"path":".env.development"}).status_code == 400
    assert api.get("/api/file", headers=headers(w), params={"path":"tls.key"}).status_code == 400
    assert api.get("/api/file", headers=headers(w), params={"path":"big.txt"}).status_code == 400
    assert api.get("/api/file", headers=headers(w), params={"path":"../hello.py"}).status_code == 400
    assert api.get("/api/file", headers=headers(w), params={"path":str(sandbox / "hello.py")}).status_code == 400
    assert api.get("/api/tree", headers=headers(w), params={"path":".."}).status_code == 400


def test_symlink_is_never_followed(client, sandbox, tmp_path):
    api, w = client
    target = tmp_path / "private.txt"
    target.write_text("SECRET")
    try:
        (sandbox / "link.txt").symlink_to(target)
    except OSError:
        pytest.skip("Symlinks require elevation on this machine")
    api.post("/api/project", headers=headers(w), json={"path":str(sandbox)})
    assert api.get("/api/file", headers=headers(w), params={"path":"link.txt"}).status_code == 400
    assert not any(x["name"] == "link.txt" for x in api.get("/api/tree", headers=headers(w)).json()["entries"])


def test_run_is_explicit_and_cannot_run_outside_git(client):
    api, w = client
    result = api.post("/api/start", headers=headers(w), json={"mode":"run","message":"create tests"})
    assert result.status_code == 409
    assert not w.jobs


def test_chat_never_falls_back_to_engineering_task(client):
    api, w = client
    w.use_core = False  # exercise unavailable CLI, not an installed Brain
    result = api.post("/api/start", headers=headers(w), json={"mode":"chat","message":"hello"})
    assert result.status_code == 200
    job = w.jobs[result.json()["id"]]
    assert job.command == ["not-a-real-binary", "chat", "hello"]
    for _ in range(50):
        if job.status == "failed":
            break
        time.sleep(.02)
    assert job.status == "failed"   # No installed CLI, and critically no 'run'.
    assert all('run' not in event['message'] for event in job.dump())


def test_acceptance_requires_actual_prompt(client):
    api, w = client
    result = api.post("/api/start", headers=headers(w), json={"mode":"chat","message":"hello"})
    job_id = result.json()["id"]
    assert api.post(f"/api/jobs/{job_id}/decision", headers=headers(w), json={"allow":True}).status_code == 409


def test_two_sessions_not_allowed_simultaneously(tmp_path):
    # Verify deterministic in-memory coordination without launching outside commands.
    w = Workspace(home=tmp_path, executable=sys.executable)
    w.use_core = False  # deliberately use the command-array fallback
    fake = w.start(StartRequest(mode="chat", message="hello"))
    if fake.status in {"starting", "running", "approval_required"}:
        with pytest.raises(ValueError):
            w.start(StartRequest(mode="chat", message="another"))


def test_static_contains_no_remote_urls():
    html = (Path(__file__).parents[1] / "desktop_ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert "app.js" in html


def test_run_uses_argument_array_not_shell(client, sandbox):
    api, w = client
    api.post("/api/project", headers=headers(w), json={"path":str(sandbox)})
    w.use_core = False  # Deliberate legacy argument-array test; production requires typed service.
    prompt = "fix code && echo NEVER_EXECUTE"
    created = api.post("/api/start", headers=headers(w), json={"mode":"run","message":prompt})
    assert created.status_code == 200
    job = w.jobs[created.json()["id"]]
    assert job.command[-1] == prompt
    assert job.command[1] == "run"
