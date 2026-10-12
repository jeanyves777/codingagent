"""Pull requests validate on GitHub in the background: the desktop's watcher never blocks the window
or the active job, offers Merge only for a pull request the engine reported, and passes exactly the
head the user was shown (the engine re-checks that every check is green before merging)."""
import time

import pytest
from fastapi.testclient import TestClient

from desktop_ui.server import Workspace, create_app

GREEN = {"task_id": "abcdef12", "state": "OPEN", "ask_merge": True, "failed": 0, "pending": 0, "head": "a" * 40,
         "url": "https://github.com/o/r/pull/1", "question": "All 2 checks are green on https://github.com/o/r/pull/1. Merge it?"}


class Engine:
    info = {"features": ["tasks.pull_requests"]}

    def __init__(self, delay=0.0):
        self.calls, self.delay = [], delay

    def call(self, op, params=None, on_event=None, timeout=None):
        self.calls.append((op, params))
        if op == "projects.register":
            return {"id": "proj1"}
        if op == "pull_requests.list":
            time.sleep(self.delay)  # GitHub is slow; the window must not wait for it
            return {"enabled": True, "pull_requests": [GREEN]}
        if op == "tasks.merge":
            return {"merged": True, "url": GREEN["url"]}
        raise AssertionError(op)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    engine = Engine(delay=0.3)
    monkeypatch.setattr(Workspace, "engine_client", lambda self: engine)
    workspace = Workspace(home=tmp_path, executable="unused")
    workspace.project = tmp_path
    http = TestClient(create_app(workspace))
    return http, {"X-CodingBrain-Token": workspace.secret}, workspace, engine


def settle(workspace):
    deadline = time.monotonic() + 5
    while workspace._reviews_running and time.monotonic() < deadline:
        time.sleep(.02)


def test_the_watcher_never_blocks_and_reports_green_pull_requests(desktop):
    http, headers, workspace, _ = desktop
    started = time.monotonic()
    first = http.get("/api/pull-requests", headers=headers).json()
    assert time.monotonic() - started < 0.25 and first["pull_requests"] == []  # answered from the cache
    settle(workspace)
    found = http.get("/api/pull-requests", headers=headers).json()
    assert found["enabled"] and found["pull_requests"][0]["ask_merge"]


def test_merge_passes_the_shown_head_and_only_for_known_pull_requests(desktop):
    http, headers, workspace, engine = desktop
    http.get("/api/pull-requests", headers=headers)
    settle(workspace)
    unknown = http.post("/api/pull-requests/merge", json={"task_id": "ffffff99", "head": "a" * 40}, headers=headers)
    assert unknown.status_code == 409
    bad = http.post("/api/pull-requests/merge", json={"task_id": "abcdef12", "head": "main"}, headers=headers)
    assert bad.status_code == 422
    assert http.post("/api/pull-requests/merge", json={"task_id": "abcdef12", "head": "a" * 40},
                     headers=headers).json()["merged"]
    assert ("tasks.merge", {"project_id": "proj1", "task_id": "abcdef12", "head": "a" * 40}) in engine.calls


def test_older_engines_are_not_asked(desktop):
    http, headers, workspace, engine = desktop
    engine.info = {"features": []}
    http.get("/api/pull-requests", headers=headers)
    settle(workspace)
    assert [op for op, _ in engine.calls] == []
