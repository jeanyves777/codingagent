import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from brain.api import create_app
from brain.approvals import ApprovalRequired, ToolApprovalStore
from brain.mcp_gateway import MCPGateway
from brain.routing import RoutedModel, RoutingPerformance
from brain.service import Brain
from brain.telemetry import Telemetry


class Block:
    def model_dump(self, **kwargs):
        return {"type": "text", "text": "done"}


class FakeClient:
    calls = []

    def __init__(self, url, **kwargs):
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(
            name="write", description="Write", input_schema={"type": "object"}
        )])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content={"saved": True}, content=[Block()])


def gateway(tmp_path):
    approvals = ToolApprovalStore(tmp_path / "approvals.sqlite3")
    return approvals, MCPGateway({"ops": {"url": "http://127.0.0.1:9000/mcp",
        "tools": {"write": "approval_required"}}}, client_factory=FakeClient,
        approvals=approvals)


def test_approved_mcp_tool_executes_once_and_replays_result(tmp_path):
    approvals, mcp = gateway(tmp_path)
    FakeClient.calls = []
    with pytest.raises(ApprovalRequired) as pause:
        asyncio.run(mcp.invoke("mcp__ops__write", {"value": 1}, "task"))
    request_id = pause.value.request["id"]
    approvals.decide(request_id, True)
    first = asyncio.run(mcp.invoke("mcp__ops__write", {"value": 1}, "task"))
    second = asyncio.run(mcp.invoke("mcp__ops__write", {"value": 1}, "task"))
    assert first == second and len(FakeClient.calls) == 1
    assert approvals.get(request_id)["status"] == "completed"


class ApprovalModel:
    def __init__(self, mcp):
        self.mcp = mcp

    async def propose(self, root, goal, memories, repository_context=None, task_id=None):
        await self.mcp.invoke("mcp__ops__write", {"value": goal}, task_id)
        return json.dumps({"plan": "approved change",
                           "changes": [{"path": "main.py", "content": "x = 2\n"}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}

    async def decompose(self, goal):
        return {"assignments": [{"name": "one", "goal": goal, "depends_on": []}]}


async def drain(brain):
    while brain.jobs:
        await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
        await asyncio.sleep(0)


def test_brain_pauses_and_resumes_after_tool_approval(tmp_path):
    repository = tmp_path / "repositories" / "demo"
    repository.mkdir(parents=True)
    (repository / "main.py").write_text("x = 1\n")
    approvals, mcp = gateway(tmp_path)
    brain = Brain(repository.parent, tmp_path / "data", ApprovalModel(mcp), "image",
                  approvals=approvals)

    async def flow():
        task = brain.submit("demo", "change", launch=False)
        task = await brain.create(task)
        assert task["status"] == "awaiting_tool_approval"
        request_id = task["pending_approval_id"]
        brain.decide_tool_approval(request_id, True)
        await drain(brain)
        task = brain.store.get(task["id"])
        assert task["status"] == "proposed"
        assert approvals.get(request_id)["status"] == "completed"
    asyncio.run(flow())


def test_telemetry_tracks_events_spans_and_consumer_cursor(tmp_path):
    telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
    event_id = telemetry.publish("task", "trace", "created", "ready")
    with telemetry.span("trace", "worker.create", "task", {"worker": "one"}):
        with telemetry.span("trace", "model.propose", "task"):
            pass
    assert telemetry.pending("workflow")[0]["id"] == event_id
    telemetry.acknowledge("workflow", event_id)
    assert telemetry.pending("workflow") == []
    trace = telemetry.trace("trace")
    assert [span["status"] for span in trace["spans"]] == ["ok", "ok"]
    assert trace["spans"][1]["parent_id"] == trace["spans"][0]["id"]
    assert trace["events"][0]["kind"] == "created"


class NamedModel:
    def __init__(self, name):
        self.name = name

    async def propose(self, *args, **kwargs):
        return self.name

    async def decompose(self, goal):
        return {}

    async def review(self, goal, diff):
        return {}


def test_adaptive_router_uses_verified_performance(tmp_path):
    performance = RoutingPerformance(tmp_path / "routing.sqlite3")
    for number in range(6):
        performance.record(f"fast-{number}", "fast", True)
        performance.record(f"strong-{number}", "strong", False)
    router = RoutedModel(NamedModel("fast"), NamedModel("strong"), threshold=1,
                         performance=performance)
    result = asyncio.run(router.propose(None, "complex", [], {"symbols": [{}] * 10}, "task"))
    assert result == "fast"
    assert router.routes[-1]["strategy"] == "adaptive_fast"
    assert performance.stats()["fast"]["success_rate"] == 1.0


def test_event_workflow_dispatches_parent_progress(tmp_path):
    repository = tmp_path / "repositories" / "demo"
    repository.mkdir(parents=True)
    model = NamedModel("model")
    brain = Brain(repository.parent, tmp_path / "data", model, "image")
    parent = {"id": "parent", "kind": "orchestration", "status": "active", "events": [],
              "trace_id": "trace", "children": []}
    child = {"id": "child", "kind": "task", "status": "accepted", "events": [],
             "trace_id": "trace", "parent_id": "parent"}
    brain.store.save(parent)
    brain.store.save(child)
    brain.event(child, "accepted", "done")
    scheduled = []
    brain.schedule = lambda task_id, action, payload=None: scheduled.append((task_id, action))
    brain.dispatch_events()
    assert scheduled == [("parent", "advance_group")]


def test_event_and_trace_api_exposes_durable_observability(tmp_path):
    repository = tmp_path / "repositories" / "demo"
    repository.mkdir(parents=True)
    brain = Brain(repository.parent, tmp_path / "data", NamedModel("model"), "image")
    task = brain.submit("demo", "observe", launch=False)
    brain.event(task, "observed", "ready")
    headers = {"Authorization": "Bearer " + "t" * 32}
    with TestClient(create_app(brain, "t" * 32)) as client:
        events = client.get("/events", headers=headers,
                            params={"task_id": task["id"]}).json()["events"]
        trace = client.get("/traces/" + task["trace_id"], headers=headers).json()
    assert events[-1]["kind"] == "observed"
    assert trace["trace_id"] == task["trace_id"]
    assert any(span["name"] == "api.submit" for span in trace["spans"])
