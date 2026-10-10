"""Acceptance against the real published v0.9.0 engine (the owner's installed version): set
CODINGBRAIN_LEGACY_ENGINE to its `codingbrain` launcher. CI installs the v0.9.0 release wheel on
Windows and runs this; elsewhere it is skipped."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_ui.server import Workspace, create_app

LEGACY = os.environ.get("CODINGBRAIN_LEGACY_ENGINE")
pytestmark = pytest.mark.skipif(not LEGACY, reason="set CODINGBRAIN_LEGACY_ENGINE to a v0.9.0 codingbrain launcher")


def test_v090_new_project_question_never_reaches_a_missing_module(tmp_path, monkeypatch):
    monkeypatch.setenv("CODINGBRAIN_HOME", str(tmp_path / "cb"))
    workspace = Workspace(home=tmp_path, executable=LEGACY)
    http = TestClient(create_app(workspace))
    headers = {"X-CodingBrain-Token": workspace.secret}
    status = http.get("/api/engine/status?refresh=true", headers=headers).json()
    assert status["engine"]["available"] and status["engine"]["version"].startswith("0.9.0")
    assert status["chat_available"] is False and not status["engine"]["typed_api"]
    for mode, message in (("new", "CHECK IS ANY PROJECT EXIST"), ("chat", "hello"), ("run", "fix the bug")):
        response = http.post("/api/start", json={"message": message, "mode": mode}, headers=headers)
        assert response.status_code == 409, (mode, response.text)
        assert "Traceback" not in response.text and "brain.local.create" not in response.text
    assert not workspace.jobs  # no job, no engineering task, no greeting
    assert not (tmp_path / "Projects").exists()  # nothing created
