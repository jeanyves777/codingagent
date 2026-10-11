"""Editor save rules and the side-by-side diff helper (no browser)."""
import hashlib

import pytest
from fastapi.testclient import TestClient

from desktop_ui.diffview import views
from desktop_ui.server import Workspace, create_app

DIFF = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
        "-    return a + b\n+    total = a + b\n+    return total\n"
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-A=1\n+A=2\n")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / ".env").write_text("A=1\nSECRET=very\n", encoding="utf-8")
    return root


def test_diff_views_apply_hunks_to_the_full_file(project):
    files = {item["path"]: item for item in views(DIFF, project, allowed=lambda path: not path.startswith(".env"))}
    assert files["calc.py"]["exact"] and files["calc.py"]["modified"] == "def add(a, b):\n    total = a + b\n    return total\n"
    assert files["new.py"] == {"path": "new.py", "status": "added", "original": "", "modified": "x = 1", "exact": True}
    # A secret file is never read from disk: only the diff's own lines are shown.
    assert files[".env"]["exact"] is False and "SECRET" not in files[".env"]["original"]


def test_diff_views_fall_back_to_changed_regions_when_the_file_differs(project):
    (project / "calc.py").write_text("something else\n", encoding="utf-8")
    calc = next(item for item in views(DIFF, project) if item["path"] == "calc.py")
    assert calc["exact"] is False and "return a + b" in calc["original"] and "total = a + b" in calc["modified"]


def test_save_requires_the_opened_version_and_writes_atomically(tmp_path, project):
    workspace = Workspace(home=tmp_path, executable="not-a-real-binary")
    workspace.choose(str(project))
    client = TestClient(create_app(workspace))
    headers = {"X-CodingBrain-Token": workspace.secret}
    opened = client.get("/api/file", headers=headers, params={"path": "calc.py"}).json()
    assert opened["sha256"] == hashlib.sha256((project / "calc.py").read_bytes()).hexdigest()
    saved = client.post("/api/file", headers=headers, json={"path": "calc.py", "content": "x = 2\n", "sha256": opened["sha256"]})
    assert saved.status_code == 200 and (project / "calc.py").read_text() == "x = 2\n"
    stale = client.post("/api/file", headers=headers, json={"path": "calc.py", "content": "x = 3\n", "sha256": opened["sha256"]})
    assert stale.status_code == 409 and (project / "calc.py").read_text() == "x = 2\n"  # never overwritten
    assert not list(project.glob(".*.tmp"))  # no staging file left behind
    secret = client.post("/api/file", headers=headers, json={"path": ".env", "content": "", "sha256": "0" * 64})
    assert secret.status_code == 400
    outside = client.post("/api/file", headers=headers, json={"path": "../x.py", "content": "", "sha256": "0" * 64})
    assert outside.status_code == 400
    assert client.post("/api/file", json={"path": "calc.py", "content": "", "sha256": "0" * 64}).status_code == 403


def test_bundled_editor_files_are_served_only_from_the_vendor_folder(tmp_path):
    client = TestClient(create_app(Workspace(home=tmp_path)))
    # Encoded so the HTTP client does not normalize the dots away before the request is sent.
    assert client.get("/vendor/%2e%2e/server.py").status_code == 404
    assert client.get("/vendor/monaco/%2e%2e/%2e%2e/server.py").status_code == 404
    assert client.get("/vendor/..%5c..%5cserver.py").status_code == 404
