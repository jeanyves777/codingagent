import asyncio
import copy
import json
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from brain.api import create_app
from brain.capabilities import Capability, CapabilityRegistry
from brain.evaluation import jsonl, learning_examples, report
from brain.intelligence import build_index, relevant_context
from brain.memory import OllamaEmbedder, SemanticMemory, SQLiteVectorMemory
from brain.model import OllamaModel
from brain.repository import safe_path, snapshot
from brain.sandbox import run_tests
from brain.sandbox import profile as sandbox_profile
from brain.routing import RoutedModel
from brain.service import Assignment, Brain, Delegation, validate_graph


class Model:
    def __init__(self, answers=None, graph=None):
        self.answers = answers or {}
        self.graph = graph or {
            "assignments": [
                {"name": "first", "goal": "first change", "depends_on": []},
                {"name": "second", "goal": "second change", "depends_on": ["first"]},
            ]
        }
        self.observed = []

    async def propose(self, root, goal, memories, repository_context=None):
        current = (root / "main.py").read_text()
        self.observed.append({"goal": goal, "current": current, "context": repository_context})
        content = self.answers.get(goal)
        if content is None:
            content = "x = 3\n" if "second" in goal else "x = 2\n"
        return json.dumps({"plan": "Make requested change",
                           "changes": [{"path": "main.py", "content": content}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "Matches goal"}

    async def decompose(self, goal):
        return self.graph


def git(repository: Path, *arguments):
    result = subprocess.run(["git", "-C", str(repository), *arguments],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_repository(path: Path, use_git=False):
    path.mkdir(parents=True)
    (path / "main.py").write_text("import os\n\nclass Engine:\n    def run(self):\n        return 1\n\nx = 1\n")
    (path / "test_main.py").write_text("def test_ok():\n    assert True\n")
    if use_git:
        git(path, "init")
        git(path, "config", "user.name", "Test")
        git(path, "config", "user.email", "test@example.com")
        git(path, "add", ".")
        git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def brain(tmp_path):
    repository = make_repository(tmp_path / "repos" / "demo")
    return Brain(repository.parent, tmp_path / "data", Model(), "test-image")


@pytest.fixture
def git_brain(tmp_path):
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    return Brain(repository.parent, tmp_path / "data", Model(), "test-image")


async def create_direct(brain, goal="Fix x"):
    task = brain.submit("demo", goal, launch=False)
    return await brain.create(task)


async def drain(brain):
    while brain.jobs:
        await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.parametrize("name", [
    "../escape.py", "/tmp/a.py", ".env", ".git/config", "a/../../b.py", "secret.pem",
    "coding-brain.json",
])
def test_path_denied(tmp_path, name):
    with pytest.raises(ValueError):
        safe_path(tmp_path, name)


def test_symlink_denied(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        safe_path(tmp_path, "link/x.py")


def test_snapshot_excludes_credentials_dependencies_and_git(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("main.py", ".env", "credentials.json"):
        (source / name).write_text("data")
    for directory in ("node_modules", ".git"):
        (source / directory).mkdir()
        (source / directory / "a.py").write_text("data")
    snapshot(source, tmp_path / "snapshot")
    assert sorted(p.name for p in (tmp_path / "snapshot").iterdir()) == ["main.py"]


def test_tree_sitter_index_python_and_typescript(tmp_path):
    (tmp_path / "a.py").write_text("import os\nclass Worker:\n    def run(self):\n        pass\n")
    (tmp_path / "b.ts").write_text("import {x} from './x';\ninterface Job { id: string }\nfunction go() {}\n")
    index = build_index(tmp_path)
    assert {item["name"] for item in index["symbols"]} >= {"Worker", "run", "Job", "go"}
    assert len(index["dependencies"]) == 2
    assert relevant_context(index, "Worker run")["symbols"][0]["path"] == "a.py"


def test_graph_validation():
    valid = Delegation(assignments=[
        Assignment(name="a", goal="a"), Assignment(name="b", goal="b", depends_on=["a"])
    ])
    validate_graph(valid)
    with pytest.raises(ValueError, match="cycle"):
        validate_graph(Delegation(assignments=[
            Assignment(name="a", goal="a", depends_on=["b"]),
            Assignment(name="b", goal="b", depends_on=["a"]),
        ]))
    with pytest.raises(ValueError, match="unique"):
        validate_graph(Delegation(assignments=[
            Assignment(name="a", goal="a"), Assignment(name="a", goal="b"),
        ]))


def test_approval_memory_and_original_unchanged(brain, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a: {"passed": True, "exit_code": 0, "output": "passed"})

    async def flow():
        task = await create_direct(brain)
        assert task["status"] == "proposed"
        with pytest.raises(ValueError):
            await brain.execute(task["id"], "0" * 64)
        task = await brain.execute(task["id"], task["digest"])
        assert task["status"] == "passed"
        await brain.accept(task["id"], "Tested fix")
        memory = brain.store.memories("demo")[0]
        assert memory["verification"] == "tests_passed_and_human_accepted"
        assert (brain.repositories / "demo/main.py").read_text().endswith("x = 1\n")
        assert brain.workspace(task["id"]).joinpath("main.py").read_text() == "x = 2\n"
    asyncio.run(flow())


def test_no_tests_fails_closed(brain, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a: {"passed": False, "exit_code": 5, "output": "no tests"})

    async def flow():
        task = await create_direct(brain)
        task = await brain.execute(task["id"], task["digest"])
        assert task["status"] == "failed"
        with pytest.raises(ValueError):
            await brain.accept(task["id"], "not verified")
    asyncio.run(flow())


def test_worker_limit(brain):
    active = maximum = 0
    brain.slots = asyncio.Semaphore(2)

    async def flow():
        nonlocal active, maximum
        async def operation():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
        for number in range(5):
            brain.store.save({"id": str(number), "status": "queued", "events": []})
            brain.launch(str(number), operation)
        await drain(brain)
        assert maximum == 2
    asyncio.run(flow())


def test_dependency_inherits_accepted_commit(git_brain, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a: {"passed": True, "exit_code": 0, "output": "passed"})

    async def flow():
        group = git_brain.delegate("demo", "Sequential changes")
        await drain(git_brain)
        state = git_brain.orchestration(group["id"])
        first, second = [child["task"] for child in state["children"]]
        assert first["status"] == "proposed" and second["status"] == "waiting"
        first = await git_brain.execute(first["id"], first["digest"])
        await git_brain.accept(first["id"], "Accepted first")
        await drain(git_brain)
        state = git_brain.orchestration(group["id"])
        second = state["children"][1]["task"]
        assert second["status"] == "proposed"
        observed = [item for item in git_brain.model.observed if item["goal"] == "second change"][0]
        assert observed["current"] == "x = 2\n"
        second = await git_brain.execute(second["id"], second["digest"])
        await git_brain.accept(second["id"], "Accepted second")
        state = git_brain.orchestration(group["id"])
        assert state["status"] == "completed"
        integration = Path(state["integration_workspace"])
        assert (integration / "main.py").read_text() == "x = 3\n"
        assert (git_brain.repositories / "demo/main.py").read_text().endswith("x = 1\n")
    asyncio.run(flow())


def test_dependency_failure_blocks_downstream(git_brain, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a: {"passed": False, "exit_code": 5, "output": "no tests"})

    async def flow():
        group = git_brain.delegate("demo", "Sequential changes")
        await drain(git_brain)
        first = git_brain.orchestration(group["id"])["children"][0]["task"]
        await git_brain.execute(first["id"], first["digest"])
        state = git_brain.orchestration(group["id"])
        assert state["status"] == "attention_required"
        assert state["children"][1]["task"]["status"] == "blocked"
    asyncio.run(flow())


def test_parallel_conflict_is_reported(git_brain, monkeypatch):
    git_brain.model.graph = {"assignments": [
        {"name": "left", "goal": "left", "depends_on": []},
        {"name": "right", "goal": "right", "depends_on": []},
    ]}
    git_brain.model.answers = {"left": "x = 'left'\n", "right": "x = 'right'\n"}
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a: {"passed": True, "exit_code": 0, "output": "passed"})

    async def flow():
        group = git_brain.delegate("demo", "Parallel conflict")
        await drain(git_brain)
        children = git_brain.orchestration(group["id"])["children"]
        for child in children:
            task = child["task"]
            await git_brain.execute(task["id"], task["digest"])
        await git_brain.accept(children[0]["id"], "left")
        result = await git_brain.accept(children[1]["id"], "right")
        assert result["status"] == "integration_conflict"
        assert git_brain.orchestration(group["id"])["status"] == "integration_conflict"
    asyncio.run(flow())


def test_cancel_and_retry(brain):
    async def flow():
        task = await create_direct(brain)
        cancelled = brain.cancel(task["id"])
        assert cancelled["status"] == "cancelled"
        retried = brain.retry(task["id"])
        assert retried["status"] == "queued"
        await drain(brain)
        assert brain.store.get(task["id"])["status"] == "proposed"
    asyncio.run(flow())


def test_non_git_dependency_orchestration_is_rejected(brain):
    with pytest.raises(ValueError, match="Git repository"):
        brain.delegate("demo", "multiple tasks")


def test_git_tracked_changes_are_not_ignored(git_brain):
    (git_brain.repositories / "demo/main.py").write_text("local change\n")

    async def flow():
        task = git_brain.submit("demo", "change", launch=False)
        with pytest.raises(ValueError, match="tracked local changes"):
            await git_brain.create(task)
        assert git_brain.store.get(task["id"])["status"] == "blocked"
    asyncio.run(flow())


def test_restart_marks_active_work_blocked(brain):
    brain.store.save({"id": "old", "kind": "task", "status": "testing", "events": []})
    restarted = Brain(brain.repositories, brain.data, Model(), "test-image")
    assert restarted.store.get("old")["status"] == "blocked"


def test_api_auth_events_and_context(brain):
    task = {"id": "task", "kind": "task", "status": "proposed", "events": [
        {"time": "now", "kind": "proposal", "detail": "ready"}
    ]}
    brain.store.save(task)
    brain.store.save_index("demo", build_index(brain.repositories / "demo"))
    headers = {"Authorization": "Bearer " + "t" * 32}
    with TestClient(create_app(brain, "t" * 32)) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers=headers).json()["version"] == "0.5.0"
        events = client.get("/tasks/task/events", headers=headers)
        assert '"kind": "proposal"' in events.text
        context = client.get("/repositories/demo/context?query=Engine", headers=headers).json()
        assert context["symbols"][0]["name"] == "Engine"
        assert client.get("/evaluation", headers=headers).status_code == 200
        assert client.get("/learning/export", headers=headers).text == ""


def test_sandbox_flags_and_timeout_cleanup(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 120)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", run)
    result = run_tests(tmp_path, "test-image")
    assert not result["passed"]
    assert "--network=none" in calls[0] and "--cap-drop=ALL" in calls[0]
    assert "readonly" in calls[0][calls[0].index("--mount") + 1]
    assert calls[1][1:3] == ["rm", "-f"]


def test_ollama_tool_loop(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("x = 1\n")
    sent = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            if len(sent) == 1:
                return {"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "main.py"}}}]}}
            return {"message": {"role": "assistant", "content": '{"plan":"done","changes":[]}' }}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            sent.append(copy.deepcopy(json))
            return Response()
    monkeypatch.setattr("brain.model.httpx.AsyncClient", Client)
    result = asyncio.run(OllamaModel("http://localhost:11434", "model").propose(
        tmp_path, "inspect", [], {"symbols": []}
    ))
    assert json.loads(result)["changes"] == []
    assert sent[1]["messages"][-1]["content"] == "x = 1\n"


class Embedder:
    dimensions = 3
    async def embed(self, texts):
        return [[float("auth" in text.lower()), float("cache" in text.lower()), 1.0]
                for text in texts]


def test_verified_semantic_memory_and_repository_scope(tmp_path):
    memory = SemanticMemory(Embedder(), SQLiteVectorMemory(tmp_path / "memory.sqlite3", 3))

    async def flow():
        with pytest.raises(ValueError, match="verified"):
            await memory.remember("bad", "demo", "episodic", "auth fix", {}, verified=False)
        await memory.remember("one", "demo", "episodic", "authentication token fix", {"passed": True}, True)
        await memory.remember("two", "demo", "procedural", "cache invalidation", {"passed": True}, True)
        await memory.remember("three", "other", "episodic", "authentication failure", {}, True)
        result = await memory.search("demo", "auth token", limit=2)
        assert result[0]["task_id"] == "one" and all(item["task_id"] != "three" for item in result)
        procedural = await memory.search("demo", "cache", kinds=["procedural"])
        assert [item["task_id"] for item in procedural] == ["two"]
    asyncio.run(flow())


def test_ollama_embedding_contract(monkeypatch):
    sent = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"embeddings": [[1, 0, 0, 0, 0, 0, 0, 0],
                                                [0, 1, 0, 0, 0, 0, 0, 0]]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            sent.append(json)
            return Response()
    monkeypatch.setattr("brain.memory.httpx.AsyncClient", Client)
    vectors = asyncio.run(OllamaEmbedder("http://local", "embed", 8).embed(["a", "b"]))
    assert vectors[0][:3] == [1.0, 0.0, 0.0]
    assert sent[0]["truncate"] is False and sent[0]["dimensions"] == 8


def test_capability_registry_denies_mutation_without_approval():
    registry = CapabilityRegistry()
    registry.register(Capability("write", "write", {"type": "object", "properties": {}, "required": []},
                                    lambda args: "done", mutating=True))
    with pytest.raises(PermissionError):
        registry.invoke("write", {})
    assert registry.invoke("write", {}, approved=True) == "done"
    with pytest.raises(ValueError, match="Unknown"):
        registry.invoke("missing", {})


def test_model_router_selects_fast_and_strong():
    class Routed:
        def __init__(self, name): self.name = name
        async def propose(self, *args): return self.name
        async def decompose(self, goal): return {"model": self.name}
        async def review(self, goal, diff): return {"model": self.name}
    router = RoutedModel(Routed("fast"), Routed("strong"), threshold=2)

    async def flow():
        assert await router.propose(None, "small", [], {"symbols": []}) == "fast"
        context = {"symbols": [{}] * 20, "dependencies": []}
        assert await router.propose(None, "large", [], context) == "strong"
        assert (await router.review("goal", "diff"))["model"] == "strong"
    asyncio.run(flow())


def test_node_sandbox_profile_and_command_policy(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert sandbox_profile(tmp_path)["name"] == "node"
    (tmp_path / "coding-brain.json").write_text(json.dumps({
        "test_profile": "node", "test_command": ["npm", "run", "test:unit"]
    }))
    assert sandbox_profile(tmp_path)["command"][-1] == "test:unit"
    (tmp_path / "coding-brain.json").write_text(json.dumps({
        "test_profile": "node", "test_command": ["sh", "-c", "anything"]
    }))
    with pytest.raises(ValueError, match="not allowed"):
        sandbox_profile(tmp_path)


def test_node_runner_selects_node_image(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", run)
    result = run_tests(tmp_path, {"python": "py-image", "node": "node-image"})
    assert result["passed"] and result["profile"] == "node"
    assert "node-image" in commands[0] and commands[0][-4:] == ["npm", "test", "--", "--runInBand"]


def test_evaluation_and_learning_export_require_verified_acceptance():
    accepted = {"id": "a", "kind": "task", "repository": "demo", "goal": "fix",
                "status": "accepted", "proposal": {"plan": "p", "changes": []}, "diff": "d",
                "review": {"approved": True}, "test_evidence": {"passed": True},
                "events": [{"kind": "reviewer"}, {"kind": "tester"},
                           {"kind": "accepted", "detail": "verified"}]}
    rejected = {"id": "b", "kind": "task", "repository": "demo", "goal": "bad",
                "status": "failed", "test_evidence": {"passed": False},
                "events": [{"kind": "tester"}]}
    metrics = report([accepted, rejected])
    assert metrics["acceptance_rate"] == 0.5 and metrics["test_attempts"] == 2
    assert [item["task_id"] for item in learning_examples([accepted, rejected])] == ["a"]
    assert json.loads(jsonl([accepted, rejected]))["acceptance_summary"] == "verified"


def test_semantic_memory_failure_does_not_undo_acceptance(brain, monkeypatch):
    class BrokenMemory:
        async def remember(self, *args, **kwargs): raise RuntimeError("offline")
        async def search(self, *args, **kwargs): raise RuntimeError("offline")
    brain.memory = BrokenMemory()
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a: {"passed": True, "exit_code": 0, "output": "passed"})

    async def flow():
        task = await create_direct(brain)
        task = await brain.execute(task["id"], task["digest"])
        task = await brain.accept(task["id"], "verified")
        assert task["status"] == "accepted"
        assert any(event["kind"] == "memory_deferred" for event in task["events"])
    asyncio.run(flow())
