import asyncio
import copy
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
    pytest.importorskip("anthropic")
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


def test_openai_compatible_tool_loop(tmp_path, monkeypatch):
    from brain.openai_compatible import OpenAICompatibleModel
    (tmp_path / "main.py").write_text("x = 1\n")
    sent = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json, headers):
            sent.append({"url": url, "body": copy.deepcopy(json), "headers": headers})
            if len(sent) == 1:
                return Response({"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "t1", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "main.py"}'}}]}}]})
            return Response({"usage": {"prompt_tokens": 7, "completion_tokens": 3},
                             "choices": [{"finish_reason": "stop", "message": {
                                 "role": "assistant", "content": '{"plan": "ok", "changes": []}'}}]})
    monkeypatch.setattr("brain.openai_compatible.httpx.AsyncClient", Client)
    model = OpenAICompatibleModel("http://localhost:1234/v1/", "local", api_key="k")
    result = asyncio.run(model.propose(tmp_path, "inspect", []))
    assert json.loads(result)["plan"] == "ok"
    assert sent[0]["url"] == "http://localhost:1234/v1/chat/completions"
    assert sent[0]["headers"] == {"Authorization": "Bearer k"}
    assert sent[0]["body"]["tools"][0]["type"] == "function"
    tool_message = sent[1]["body"]["messages"][-1]
    assert tool_message == {"role": "tool", "tool_call_id": "t1", "content": "x = 1\n"}
    assert model.usage[-1]["output_tokens"] == 3


class Brain_:
    def __init__(self, name, error=None):
        self.name, self.error, self.calls, self.usage = name, error, 0, [{"model": name}]
    async def review(self, goal, diff):
        self.calls += 1
        if self.error:
            raise self.error
        return {"approved": True, "reason": self.name}


def test_failover_uses_next_brain_but_not_on_approval():
    from brain.approvals import ApprovalRequired
    from brain.brains import FailoverModel
    down, up = Brain_("down", ConnectionError("refused")), Brain_("up")
    chain = FailoverModel([down, up])
    assert asyncio.run(chain.review("g", "d"))["reason"] == "up"
    assert chain.served[-1] == {"method": "review", "model": "up", "failed_over": 1}
    assert chain.name == "down+up" and len(chain.usage) == 2
    from brain.brains import ImplementerUnavailable
    with pytest.raises(ImplementerUnavailable, match="down.*refused"):
        asyncio.run(FailoverModel([down]).review("g", "d"))
    with pytest.raises(ValueError, match="All brains failed.*bad.*broken"):
        asyncio.run(FailoverModel([Brain_("bad", ValueError("broken")), down]).review("g", "d"))
    pending = Brain_("pending", ApprovalRequired({"id": "r1"}))
    with pytest.raises(ApprovalRequired):
        asyncio.run(FailoverModel([pending, up]).review("g", "d"))
    assert up.calls == 1


def test_brains_config_validation_and_role_defaults(tmp_path):
    from brain.brains import load_brains, validate_url
    path = tmp_path / "brains.json"
    path.write_text(json.dumps({"brains": {
        "a": {"provider": "ollama", "model": "m1"},
        "b": {"provider": "openai", "url": "http://192.168.1.5:8080/v1", "model": "m2"}},
        "roles": {"implementer": ["a", "b"], "reviewer": ["b"]}}))
    config = load_brains(path)
    assert config["brains"]["a"]["url"] == "http://localhost:11434"
    assert config["roles"]["fast"] == ["a", "b"] and config["roles"]["coordinator"] == ["a", "b"]
    assert config["roles"]["reviewer"] == ["b"]
    for url, key in [("http://example.com/v1", False), ("http://192.168.1.5/v1", True),
                     ("https://user:pw@host/v1", False), ("ftp://localhost", False)]:
        with pytest.raises(ValueError):
            validate_url(url, key)
    assert validate_url("http://127.0.0.1:1234/v1", True)
    path.write_text(json.dumps({"brains": {"a": {"provider": "ollama", "model": "m"}},
                                "roles": {"reviewer": ["missing"]}}))
    with pytest.raises(ValueError, match="unknown brain"):
        load_brains(path)


def test_factory_builds_free_failover_roles(tmp_path, monkeypatch):
    from brain.brains import FailoverModel
    from brain.model import OllamaModel
    from brain.openai_compatible import OpenAICompatibleModel
    config = tmp_path / "brains.json"
    config.write_text(Path("brains.example.json").read_text())
    monkeypatch.setenv("BRAIN_BRAINS_CONFIG", str(config))
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("BRAIN_REPOSITORIES", str(tmp_path / "repos"))
    monkeypatch.setenv("BRAIN_DATA", str(tmp_path / "data"))
    brain = build_brain_from_env()
    implementer = brain.model.strong
    assert isinstance(implementer, FailoverModel)
    assert [type(model) for model in implementer.models] == [
        OllamaModel, OpenAICompatibleModel, OpenAICompatibleModel]
    assert implementer.models[2].api_key == "gsk-test"
    assert brain.reviewer.name == "LOADED_MODEL_ID+qwen2.5-coder:14b"
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        build_brain_from_env()


def test_default_provider_is_ollama(tmp_path, monkeypatch):
    from brain.model import OllamaModel
    for name in ("BRAIN_PROVIDER", "BRAIN_BRAINS_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRAIN_MODEL", "qwen2.5-coder:14b")
    monkeypatch.setenv("BRAIN_REPOSITORIES", str(tmp_path / "repos"))
    monkeypatch.setenv("BRAIN_DATA", str(tmp_path / "data"))
    brain = build_brain_from_env()
    assert isinstance(brain.model.strong, OllamaModel) and isinstance(brain.reviewer, OllamaModel)
    assert brain.model.strong.url == "http://localhost:11434"


def test_api_retry_and_cancel_schedule_on_event_loop(brain):
    from fastapi.testclient import TestClient
    from brain.api import create_app
    token = "t" * 32
    headers = {"Authorization": "Bearer " + token}
    task = brain.submit("demo", "Fix x", launch=False)
    task["status"] = "blocked"
    brain.store.save(task)
    with TestClient(create_app(brain, token)) as client:
        response = client.post(f"/tasks/{task['id']}/retry", headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "queued"
        response = client.post(f"/tasks/{task['id']}/cancel", headers=headers)
        assert response.status_code == 200


def test_ollama_prose_answer_is_finalized_with_structured_output(tmp_path, monkeypatch):
    from brain.model import OllamaModel, json_object
    (tmp_path / "main.py").write_text("x = 1\n")
    sent = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            sent.append(copy.deepcopy(json))
            if "format" in json:
                return Response({"message": {"content": '{"plan": "p", "changes": []}'}})
            return Response({"message": {"role": "assistant", "content": "Here is my plan in prose."}})
    monkeypatch.setattr("brain.model.httpx.AsyncClient", Client)
    result = asyncio.run(OllamaModel("http://localhost:11434", "m").propose(tmp_path, "g", []))
    assert json.loads(result) == {"plan": "p", "changes": []}
    assert sent[-1]["format"]["required"] == ["plan", "changes"] and "tools" not in sent[-1]
    assert json_object('note {not json} then {"plan": "a", "changes": []} {"x": 1}')["plan"] == "a"
