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


def editor_client(tmp_path, project):
    workspace = Workspace(home=tmp_path, executable="not-a-real-binary")
    workspace.choose(str(project))
    return TestClient(create_app(workspace)), {"X-CodingBrain-Token": workspace.secret}, workspace


def test_review_b2_a_save_never_lands_in_another_project(tmp_path, project):
    other = tmp_path / "other"
    other.mkdir()
    (other / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")  # byte-identical file
    client, headers, workspace = editor_client(tmp_path, project)
    opened = client.get("/api/file", headers=headers, params={"path": "calc.py"}).json()
    assert opened["project"] == str(project.resolve())
    workspace.choose(str(other))  # the user switched projects with the tab still open
    stale = client.post("/api/file", headers=headers, json={"path": "calc.py", "content": "BAD\n",
                                                           "sha256": opened["sha256"], "project": opened["project"]})
    assert stale.status_code == 409 and "no longer selected" in stale.json()["detail"]
    assert (other / "calc.py").read_text(encoding="utf-8").startswith("def add")  # untouched


@pytest.mark.parametrize("path", [".GIT/config", ".Git/HEAD", "NODE_MODULES/x.js", "calc.py::$DATA", "calc.py.",
                                  "calc.py ", "PROGRA~1/x.txt", "NUL", "com1.txt", ".ENV", "Server.KEY"])
def test_review_windows_path_aliases_are_refused(tmp_path, project, path):
    client, headers, _ = editor_client(tmp_path, project)
    assert client.get("/api/file", headers=headers, params={"path": path}).status_code == 400
    assert client.post("/api/file", headers=headers, json={"path": path, "content": "", "sha256": "0" * 64}).status_code == 400


def test_review_save_keeps_bom_and_windows_line_endings(tmp_path, project):
    script = project / "tool.ps1"
    script.write_bytes(b"\xef\xbb\xbf" + "Write-Host 'café'\r\nexit 0\r\n".encode("utf-8"))
    client, headers, _ = editor_client(tmp_path, project)
    opened = client.get("/api/file", headers=headers, params={"path": "tool.ps1"}).json()
    assert opened["bom"] and opened["eol"] == "\r\n" and not opened["content"].startswith("﻿")
    edited = opened["content"].replace("\r\n", "\n").replace("exit 0", "exit 1")  # what an editor may hand back
    assert client.post("/api/file", headers=headers, json={"path": "tool.ps1", "content": edited, "sha256": opened["sha256"],
                                                          "project": opened["project"]}).status_code == 200
    data = script.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b"exit 1\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_review_b1_open_in_vs_code_never_runs_a_project_file(tmp_path, project, monkeypatch):
    import os as _os
    import subprocess as _subprocess
    from desktop_ui import server as server_module
    (project / "run.bat").write_text("echo pwned\n")
    client, headers, _ = editor_client(tmp_path, project)
    started, launched = [], []
    monkeypatch.setattr(server_module.shutil, "which", lambda name: None)  # VS Code not installed
    monkeypatch.setattr(_os, "startfile", lambda *a, **k: started.append(a), raising=False)
    monkeypatch.setattr(_subprocess, "Popen", lambda args, **k: launched.append(args))
    reply = client.post("/api/file/open-external", headers=headers, json={"path": "run.bat"})
    assert not started  # the default handler would RUN a .bat
    if _os.name == "nt":
        assert reply.status_code == 200 and reply.json()["opened"] == "explorer"
        assert launched and launched[0][0] == "explorer.exe" and launched[0][1] == "/select,"
    else:
        assert reply.status_code == 400


def test_review_b3_a_removed_sql_comment_is_not_a_file_header():
    from desktop_ui.diffview import parse
    diff = ("diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n select 1;\n--- old comment\n"
            " select 2;\n@@ -10,2 +9,2 @@\n a\n-x\n+y\n")
    files = parse(diff)
    assert [item["old"] for item in files] == ["q.sql"]
    assert files[0]["hunks"][0]["lines"] == [(" ", "select 1;"), ("-", "-- old comment"), (" ", "select 2;")]
    assert files[0]["hunks"][1]["lines"] == [(" ", "a"), ("-", "x"), ("+", "y")]
    assert all(hunk["complete"] for hunk in files[0]["hunks"])


def test_review_b3_cut_off_diffs_are_never_labelled_full_file(project):
    from desktop_ui.diffview import views
    cut = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
    calc = views(cut, project)[0]
    assert calc["exact"] is False and "incomplete" in calc["note"]
    whole = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return b + a\n"
    assert views(whole, project)[0]["exact"] is True
    assert "cut at 100,000" in views(whole, project, truncated=True)[-1]["note"]


@pytest.mark.parametrize("url,ok", [("https://example.com/flight?x=1", True), ("http://example.com", True),
                                    ("javascript:alert(1)", False), ("file:///C:/Windows/System32/calc.exe", False),
                                    ("https://exa\nmple.com", False), ("C:\Windows\notepad.exe", False)])
def test_display_links_open_only_web_addresses(tmp_path, monkeypatch, url, ok):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda target, new=0: opened.append(target))
    workspace = Workspace(home=tmp_path)
    client = TestClient(create_app(workspace))
    reply = client.post("/api/open-url", headers={"X-CodingBrain-Token": workspace.secret}, json={"url": url})
    assert (reply.status_code == 200) is ok and (opened == [url]) is ok
