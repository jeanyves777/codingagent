"""The desktop's Backlog panel: read-only, from the installed engine's backlog.status. The window
shows the backlog and never works it (no run, no add, no approval of its own); an engine without
the operation is reported truthfully."""
import re

from desktop_ui.server import ASSETS, Workspace
from test_desktop_engine import InProcess, desktop, tmp_path  # noqa: F401  (fixtures)
from test_desktop_engine_status import TypedEngine, typed_workspace
from test_engine import world  # noqa: F401  (fixture)

READS = {"engine.info", "providers.list", "projects.list", "backlog.status"}


class Recording(InProcess):
    def __init__(self, engine):
        super().__init__(engine)
        self.ops = []

    def call(self, op, params=None, on_event=None, timeout=None):
        self.ops.append(op)
        return super().call(op, params, on_event, timeout)


def test_backlog_panel_shows_the_engine_status_and_only_reads(world, desktop, monkeypatch):
    http, headers, workspace = desktop
    engine, project = world["engine"], world["project"]
    first = engine.call("backlog.add", {"project_id": project["id"], "goal": "Create alpha.py", "priority": "P0"})
    engine.call("backlog.add", {"project_id": project["id"], "goal": "Use alpha", "depends_on": [first["id"]]})
    recording = Recording(engine)
    monkeypatch.setattr(Workspace, "engine_client", lambda self: recording)
    workspace._engine_status_cache = None

    report = http.get("/api/backlog", headers=headers).json()

    assert report["available"] and report["project_id"] == project["id"]
    assert report["status"] == engine.call("backlog.status", {"project_id": project["id"]})
    assert report["status"]["counts"] == {"ready": 1, "waiting": 1}
    assert [item["title"] for item in report["status"]["items"]] == ["Create alpha.py", "Use alpha"]
    assert set(recording.ops) <= READS and "backlog.status" in recording.ops
    assert workspace.jobs == {} and engine.context(project["id"])._brain is None
    assert http.post("/api/backlog", headers=headers).status_code == 405  # nothing to run or change here
    assert http.get("/api/backlog").status_code == 403


def test_a_project_the_engine_does_not_know_has_no_backlog(world, desktop, tmp_path):
    http, headers, workspace = desktop
    registered = sorted(world["layout"].projects.glob("*/project.json"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert http.post("/api/project", json={"path": str(other)}, headers=headers).status_code == 200
    report = http.get("/api/backlog", headers=headers).json()
    assert report["available"] and report["status"] is None and "no backlog" in report["detail"]
    assert sorted(world["layout"].projects.glob("*/project.json")) == registered  # viewing registers nothing


def test_without_a_project_or_the_operation_nothing_is_called(tmp_path, monkeypatch):
    old = TypedEngine()  # an API 1.0 engine from before backlog.status
    workspace = typed_workspace(tmp_path, monkeypatch, old)
    assert workspace.backlog() == {"available": False, "status": None,
                                   "detail": "Select a project to see its backlog."}
    workspace.project = tmp_path
    report = workspace.backlog()
    assert not report["available"] and "backlog.status missing" in report["detail"]
    assert set(old.calls) <= {"engine.info", "providers.list"}  # never asked for what it cannot answer


def test_the_panel_is_rendered_from_text_and_never_posts():
    script = (ASSETS / "app.js").read_text(encoding="utf-8")
    page = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="tab-backlog"' in page and 'id="backlog-panel"' in page
    section = script[script.index("function renderBacklog"):script.index("function approval(")]
    assert "innerHTML" not in section and "api('/api/backlog')" in section
    assert not re.search(r"api\('/api/backlog'\s*,", script)  # GET only
    assert "Waiting for you" in section and "ledger" in section.lower()
