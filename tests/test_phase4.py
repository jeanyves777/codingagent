import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from brain.benchmark import BenchmarkRunner, validate_suite
from brain.durable_queue import DurableQueue, QueueWorker
from brain.mcp_gateway import MCPGateway


def test_durable_queue_deduplicates_and_claims_atomically(tmp_path):
    first = DurableQueue(tmp_path / "queue.sqlite3")
    second = DurableQueue(tmp_path / "queue.sqlite3")
    job_id = first.enqueue("task", "create_task")
    assert second.enqueue("task", "create_task") == job_id
    claimed = first.claim("worker-a", 60)
    assert claimed["id"] == job_id
    assert second.claim("worker-b", 60) is None
    assert first.heartbeat(job_id, "worker-a", 60)
    first.finish(job_id, "worker-a")
    assert first.jobs()[0]["status"] == "completed"


def test_expired_queue_lease_fails_closed(tmp_path):
    queue = DurableQueue(tmp_path / "queue.sqlite3")
    job_id = queue.enqueue("task", "create_task")
    queue.claim("dead-worker", 30)
    with queue.connect() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job_id))
    assert queue.claim("new-worker", 30) is None
    assert queue.jobs()[0]["status"] == "failed"
    assert queue.jobs()[0]["last_error"] == "worker lease expired"


def test_queue_worker_records_handler_failure(tmp_path):
    queue = DurableQueue(tmp_path / "queue.sqlite3")
    queue.enqueue("task", "create_task")
    async def broken(job):
        raise RuntimeError("failure")
    assert asyncio.run(QueueWorker(queue, broken).run_once())
    assert queue.jobs()[0]["status"] == "failed"
    assert queue.jobs()[0]["last_error"] == "failure"


class Block:
    def model_dump(self, **kwargs):
        return {"type": "text", "text": "result"}


class FakeClient:
    calls = []
    def __init__(self, url, **kwargs):
        self.url = url
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name="search", description="Search", input_schema={"type": "object"}),
            SimpleNamespace(name="write", description="Write", input_schema={"type": "object"}),
        ])
    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content={"ok": True}, content=[Block()])


def test_mcp_gateway_exposes_read_only_tools_only():
    gateway = MCPGateway({"docs": {"url": "http://127.0.0.1:9000/mcp",
                                    "tools": {"search": "read_only", "write": "approval_required"}}},
                         client_factory=FakeClient)
    schemas = asyncio.run(gateway.schemas())
    assert [item["function"]["name"] for item in schemas] == ["mcp__docs__search",
                                                               "mcp__docs__write"]
    result = asyncio.run(gateway.invoke("mcp__docs__search", {"q": "auth"}))
    assert '"ok": true' in result
    with pytest.raises(PermissionError):
        asyncio.run(gateway.invoke("mcp__docs__write", {}))


@pytest.mark.parametrize("url", ["http://example.com/mcp", "ftp://localhost/mcp",
                                  "https://user:pass@example.com/mcp"])
def test_mcp_gateway_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        MCPGateway({"bad": {"url": url, "tools": {"x": "read_only"}}})


class BenchmarkBrain:
    def __init__(self, paths):
        self.paths = paths
    def submit(self, repository, goal, launch=False):
        return {"id": "task", "repository": repository, "goal": goal, "status": "waiting"}
    async def create(self, task):
        task.update({"status": "proposed", "digest": "d",
                     "proposal": {"changes": [{"path": path} for path in self.paths]}})
        return task
    async def execute(self, task_id, digest):
        return {"id": task_id, "status": "passed", "review": {"approved": True},
                "test_evidence": {"passed": True},
                "proposal": {"changes": [{"path": path} for path in self.paths]}}


def test_executable_benchmark_scores_paths_review_and_tests():
    suite = {"name": "suite", "cases": [{"name": "case", "repository": "demo",
             "goal": "fix", "expected_paths": ["a.py"], "allowed_paths": ["a.py", "test_a.py"]}]}
    result = asyncio.run(BenchmarkRunner(BenchmarkBrain(["a.py", "test_a.py"])).suite(suite, True))
    assert result["score"] == 1.0 and result["cases"][0]["status"] == "passed"
    with pytest.raises(ValueError):
        validate_suite({"name": "empty", "cases": []})
