import hmac
import asyncio
import json
import os
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from .evaluation import jsonl, report
from .factory import build_brain_from_env


class TaskRequest(BaseModel):
    repository: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=8000)


class Approval(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Acceptance(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)
    kind: Literal["episodic", "semantic", "procedural"] = "episodic"


def create_app(brain=None, token=None):
    token = token or os.environ.get("BRAIN_API_TOKEN")
    if not token or len(token) < 32:
        raise RuntimeError("Set BRAIN_API_TOKEN to a random token of at least 32 characters")
    if brain is None:
        brain = build_brain_from_env()
    auth = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(auth)):
        if credentials is None or not hmac.compare_digest(credentials.credentials, token):
            raise HTTPException(401, "Invalid bearer token")

    app = FastAPI(title="Coding Brain", version="0.6.0", dependencies=[Depends(authorize)],
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(KeyError)
    async def missing(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": "Task not found"})

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.6.0", "semantic_memory": brain.memory is not None,
                "brains": {role: getattr(model, "name", None) for role, model in {
                    "implementer": getattr(brain.model, "strong", brain.model),
                    "fast": getattr(brain.model, "fast", None),
                    "reviewer": brain.reviewer, "coordinator": brain.coordinator}.items()},
                "durable_queue": brain.queue is not None,
                "tool_approvals": brain.approvals is not None}

    @app.post("/tasks")
    async def create(request: TaskRequest):
        return brain.submit(request.repository, request.goal)

    @app.post("/orchestrations")
    async def delegate(request: TaskRequest):
        return brain.delegate(request.repository, request.goal)

    @app.get("/orchestrations/{group_id}")
    def orchestration(group_id: str):
        return brain.orchestration(group_id)

    @app.get("/tasks/{task_id}")
    def task(task_id: str):
        return brain.store.get(task_id)

    @app.get("/tasks/{task_id}/events")
    async def events(task_id: str, after: int = 0, follow: bool = False):
        async def stream():
            cursor = max(0, after)
            while True:
                task = brain.store.get(task_id)
                available = task.get("events", [])
                while cursor < len(available):
                    yield "data: " + json.dumps({"index": cursor, **available[cursor]}) + "\n\n"
                    cursor += 1
                if not follow or task["status"] in {
                    "accepted", "completed", "failed", "blocked", "cancelled", "integration_conflict"
                }:
                    break
                await asyncio.sleep(0.5)
        brain.store.get(task_id)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/tasks/{task_id}/execute")
    async def execute(task_id: str, approval: Approval):
        task = brain.store.get(task_id)
        if task["status"] != "proposed" or task["digest"] != approval.digest:
            raise ValueError("Approval requires the current proposed digest")
        brain.schedule(task_id, "execute_task", {"digest": approval.digest})
        return {"id": task_id, "status": "execution_queued"}

    @app.get("/workers")
    async def workers():
        brain.reconcile_queue()
        brain.dispatch_events()
        return {"active_task_ids": list(brain.jobs),
                "durable_jobs": brain.queue.jobs() if brain.queue else []}

    @app.get("/events")
    def global_events(after: int = 0, limit: int = 100, task_id: str | None = None,
                      kind: str | None = None):
        return {"events": brain.telemetry.events(after, limit, task_id, kind)}

    @app.get("/traces/{trace_id}")
    def trace(trace_id: str):
        return brain.telemetry.trace(trace_id)

    @app.get("/tool-approvals")
    def tool_approvals(task_id: str | None = None, status: str | None = None):
        if not brain.approvals:
            return {"approvals": []}
        return {"approvals": brain.approvals.list(task_id, status)}

    @app.post("/tool-approvals/{request_id}/approve")
    async def approve_tool(request_id: str):
        return brain.decide_tool_approval(request_id, True)

    @app.post("/tool-approvals/{request_id}/deny")
    async def deny_tool(request_id: str):
        return brain.decide_tool_approval(request_id, False)

    @app.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        return brain.cancel(task_id)

    @app.post("/tasks/{task_id}/retry")
    async def retry(task_id: str):
        return brain.retry(task_id)

    @app.post("/tasks/{task_id}/cleanup")
    def cleanup(task_id: str):
        return brain.cleanup(task_id)

    @app.post("/maintenance/prune")
    def prune(older_than_days: float = 7):
        if older_than_days < 0:
            raise ValueError("older_than_days must not be negative")
        return {"removed": brain.prune(older_than_days)}

    @app.post("/tasks/{task_id}/accept")
    async def accept(task_id: str, acceptance: Acceptance):
        return await brain.accept(task_id, acceptance.summary, acceptance.kind)

    @app.post("/tasks/{task_id}/memory/sync")
    async def sync_memory(task_id: str):
        return await brain.sync_memory(task_id)

    @app.get("/memories/{repository}")
    def memories(repository: str, query: str = ""):
        return brain.store.memories(repository, query)

    @app.get("/semantic-memories/{repository}")
    async def semantic_memories(repository: str, query: str, limit: int = 5,
                                kind: Literal["episodic", "semantic", "procedural"] | None = None):
        return await brain.semantic_memories(repository, query, limit, [kind] if kind else None)

    @app.get("/repositories/{repository}/context")
    def context(repository: str, query: str = ""):
        return brain.repository_context(repository, query)

    @app.get("/evaluation")
    def evaluation():
        return report(brain.store.tasks())

    @app.get("/learning/export")
    def learning_export():
        content = jsonl(brain.store.tasks())
        return StreamingResponse(iter([content]), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": "attachment; filename=verified-learning.jsonl"})

    @app.get("/models/routes")
    def routes():
        return {"routes": getattr(brain.model, "routes", []),
                "performance": getattr(brain.model, "performance_report", lambda: {})(),
                "implementer_usage": getattr(getattr(brain.model, "strong", None), "usage", []) +
                                     getattr(getattr(brain.model, "fast", None), "usage", []),
                "reviewer_usage": getattr(brain.reviewer, "usage", []),
                "coordinator_usage": getattr(brain.coordinator, "usage", []),
                "failover": {role: getattr(model, "served", []) for role, model in {
                    "implementer": getattr(brain.model, "strong", None),
                    "fast": getattr(brain.model, "fast", None),
                    "reviewer": brain.reviewer, "coordinator": brain.coordinator}.items()
                    if hasattr(model, "served")}}

    @app.get("/mcp/tools")
    async def mcp_tools():
        gateway = getattr(getattr(brain.model, "fast", None), "mcp_gateway", None)
        return {"tools": await gateway.schemas() if gateway else []}

    return app
