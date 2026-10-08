import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from brain import sandbox
from brain.anthropic_model import AnthropicModel, FALLBACK_BETA
from brain.factory import build_brain_from_env
from brain.intelligence import build_index, relevant_context
from tests.test_brain import create_direct, drain, git, git_brain, brain  # noqa: F401


def text(value):
    return SimpleNamespace(type="text", text=value)


def reply(content, stop="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5), stop_details=None)


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def create(self, **kwargs):
        self.sent.append(kwargs)
        return self.replies.pop(0)


def fake_client(replies):
    messages = FakeMessages(replies)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_anthropic_tool_loop_uses_registry_and_fallbacks(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n")
    client, messages = fake_client([
        reply([text("Reading."), SimpleNamespace(type="tool_use", id="call_1", name="read_file",
                                                 input={"path": "main.py"})], stop="tool_use"),
        reply([text('```json\n{"plan": "done", "changes": []}\n```')]),
    ])
    model = AnthropicModel(client=client)
    result = asyncio.run(model.propose(tmp_path, "inspect", [], {"symbols": []}))
    assert json.loads(result) == {"plan": "done", "changes": []}
    first, second = messages.sent
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert first["betas"] == [FALLBACK_BETA]
    assert {tool["name"] for tool in first["tools"]} == {"list_files", "read_file", "search"}
    results = second["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "call_1" and results[0]["content"] == "x = 1\n"
    assert not results[0]["is_error"]
    assert model.usage[-1]["output_tokens"] == 5


def test_anthropic_denied_tool_is_reported_as_error(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n")
    client, messages = fake_client([
        reply([SimpleNamespace(type="tool_use", id="c", name="read_file",
                               input={"path": "../escape.py"})], stop="tool_use"),
        reply([text('{"plan": "p", "changes": []}')]),
    ])
    asyncio.run(AnthropicModel(client=client, fallbacks=False).propose(tmp_path, "g", []))
    assert "fallbacks" not in messages.sent[0]
    result = messages.sent[1]["messages"][-1]["content"][0]
    assert result["is_error"] and result["content"].startswith("Tool denied")


def test_anthropic_review_decompose_and_refusal():
    client, _ = fake_client([
        reply([text('{"approved": true, "reason": "ok"}')]),
        reply([text('Plan: {"assignments": [{"name": "a", "goal": "g", "depends_on": []}]}')]),
        SimpleNamespace(content=[], stop_reason="refusal", model="m", usage=None,
                        stop_details=SimpleNamespace(category="cyber")),
    ])
    model = AnthropicModel(client=client)
    assert asyncio.run(model.review("goal", "diff"))["approved"] is True
    assert asyncio.run(model.decompose("goal"))["assignments"][0]["name"] == "a"
    with pytest.raises(ValueError, match="declined.*cyber"):
        asyncio.run(model.review("goal", "diff"))


def test_factory_selects_anthropic_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("BRAIN_MODEL", raising=False)
    monkeypatch.setenv("BRAIN_REPOSITORIES", str(tmp_path / "repos"))
    monkeypatch.setenv("BRAIN_DATA", str(tmp_path / "data"))
    brain = build_brain_from_env()
    assert isinstance(brain.reviewer, AnthropicModel)
    assert brain.model.strong.name == "claude-opus-5-5"
    monkeypatch.setenv("BRAIN_PROVIDER", "other")
    with pytest.raises(RuntimeError, match="BRAIN_PROVIDER"):
        build_brain_from_env()


class Process:
    def __init__(self, *args, **kwargs):
        self.killed = False

    def wait(self, timeout=None):
        if self.killed:
            return -9
        raise subprocess.TimeoutExpired("docker", timeout)

    def kill(self):
        self.killed = True


def test_supervised_sandbox_stops_on_cancellation_and_timeout(tmp_path, monkeypatch):
    removed = []
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: removed.append(command))
    result = sandbox.run_tests(tmp_path, "image", should_cancel=lambda: True)
    assert result["cancelled"] and not result["passed"]
    assert removed[-1][1:3] == ["rm", "-f"]
    monkeypatch.setattr(sandbox, "TIMEOUT", 0)
    result = sandbox.run_tests(tmp_path, "image", should_cancel=lambda: False)
    assert result["output"] == "Sandbox timed out" and len(removed) == 2


def test_cancellation_during_tests_stops_task(brain, monkeypatch):
    def run(workspace, image, should_cancel):
        task_id = workspace.parent.name
        brain.cancel(task_id)
        assert should_cancel()
        return {"passed": False, "exit_code": None, "cancelled": True, "output": "stopped"}
    monkeypatch.setattr("brain.service.run_tests", run)

    async def flow():
        task = await create_direct(brain)
        with pytest.raises(asyncio.CancelledError):
            await brain.execute(task["id"], task["digest"])
        assert brain.store.get(task["id"])["status"] == "cancelled"
    asyncio.run(flow())


def test_call_graph_context(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def rotate_token(user):\n    return sign(user)\n\n"
        "def login(user):\n    return rotate_token(user)\n")
    (tmp_path / "app.ts").write_text("function boot() { return api.rotateToken(); }\n")
    index = build_index(tmp_path)
    assert {"path": "auth.py", "caller": "login", "callee": "rotate_token", "line": 5} in index["calls"]
    assert {"path": "app.ts", "caller": "boot", "callee": "rotateToken", "line": 1} in index["calls"]
    graph = relevant_context(index, "rotate_token expiry")["call_graph"]
    assert [call["caller"] for call in graph["callers"]] == ["login"]
    assert [call["callee"] for call in graph["callees"]] == ["sign"]


def test_cleanup_pins_commit_and_removes_worktree(git_brain, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a, **k: {"passed": True, "exit_code": 0, "output": "passed"})
    repository = git_brain.repositories / "demo"

    async def flow():
        task = await create_direct(git_brain)
        with pytest.raises(ValueError, match="finished"):
            git_brain.cleanup(task["id"])
        task = await git_brain.execute(task["id"], task["digest"])
        task = await git_brain.accept(task["id"], "Accepted")
        return task
    task = asyncio.run(flow())
    cleaned = git_brain.cleanup(task["id"])
    assert cleaned["workspace_removed"] and not git_brain.workspace(task["id"]).exists()
    assert git(repository, "rev-parse", cleaned["retained_ref"]) == task["commit"]
    assert str(git_brain.workspace(task["id"])) not in git(repository, "worktree", "list")


def test_orchestration_cleanup_and_prune(git_brain, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a, **k: {"passed": True, "exit_code": 0, "output": "passed"})
    repository = git_brain.repositories / "demo"

    async def flow():
        group = git_brain.delegate("demo", "Sequential changes")
        await drain(git_brain)
        for index in range(2):
            child = git_brain.orchestration(group["id"])["children"][index]["task"]
            await git_brain.execute(child["id"], child["digest"])
            await git_brain.accept(child["id"], "Accepted")
            await drain(git_brain)
        return git_brain.orchestration(group["id"])
    state = asyncio.run(flow())
    assert state["status"] == "completed"
    assert git_brain.prune(older_than_days=1) == []
    assert git_brain.prune(older_than_days=0) == [state["id"]]
    group = git_brain.store.get(state["id"])
    assert not Path(state["integration_workspace"]).exists()
    assert git(repository, "rev-parse", group["retained_ref"]) == state["integration_head"]
    assert all(git_brain.store.get(child["id"])["workspace_removed"] for child in state["children"])
    assert git(repository, "worktree", "list").count("\n") == 0
