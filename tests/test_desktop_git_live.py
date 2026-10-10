"""Real Git repository regression tests for the read-only live desktop source-control panel."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from desktop_ui.git_inspection import MAX_CHANGES, git_snapshot, _changes
from desktop_ui.server import Workspace, create_app


def git(path: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *arguments],
                            capture_output=True, text=True, timeout=12, check=True)
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "local@example.test")
    git(root, "config", "user.name", "Local Test")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    git(root, "add", "--", "README.md")
    git(root, "commit", "-qm", "Initial repository")
    return root


def test_branch_and_history_are_present_and_read_only(repository: Path):
    original = git(repository, "rev-parse", "HEAD")
    result = git_snapshot(repository)
    assert result["available"] is True and result["branch"] == "main"
    assert result["head"] == original
    assert "main" in result["branches"]
    assert len(result["commits"]) == 1
    assert result["commits"][0]["subject"] == "Initial repository"
    assert result["commits"][0]["sha"] == original
    assert result["changes"] == []
    assert all(value == 0 for value in result["counts"].values())
    assert git(repository, "status", "--porcelain") == ""
    assert git(repository, "rev-parse", "HEAD") == original


def test_live_status_updates_after_edit_stage_commit_and_checkout(repository: Path):
    (repository / "README.md").write_text("changed\n", encoding="utf-8")
    (repository / "new file.txt").write_text("new\n", encoding="utf-8")
    modified = git_snapshot(repository)
    assert modified["counts"]["modified"] == 1
    assert modified["counts"]["untracked"] == 1
    assert {x["path"] for x in modified["changes"]} == {"README.md", "new file.txt"}

    git(repository, "add", "--", "README.md")
    staged = git_snapshot(repository)
    assert staged["counts"]["staged"] == 1
    assert staged["counts"]["modified"] == 0
    assert staged["counts"]["untracked"] == 1

    git(repository, "switch", "-q", "-c", "feature/live")
    git(repository, "commit", "-qm", "Update working tree")
    new_head = git(repository, "rev-parse", "HEAD")
    current = git_snapshot(repository)
    assert current["branch"] == "feature/live"
    assert current["head"] == new_head
    assert "main" in current["branches"] and "feature/live" in current["branches"]
    assert [c["subject"] for c in current["commits"]][:2] == [
        "Update working tree", "Initial repository"
    ]
    assert current["counts"]["untracked"] == 1
    # A read-only snapshot must never stage or delete the remaining untracked file.
    assert (repository / "new file.txt").exists()
    assert "new file.txt" in git(repository, "status", "--porcelain")


def test_unborn_and_detached_branches_are_explicit(repository: Path, tmp_path: Path):
    git(repository, "checkout", "-q", "--detach", "HEAD")
    detached = git_snapshot(repository)
    assert detached["available"] and detached["detached"]
    assert detached["branch"] == "Detached HEAD"
    assert detached["head"] == git(repository, "rev-parse", "HEAD")

    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "fresh")
    snapshot = git_snapshot(empty)
    assert snapshot["available"] is True
    assert snapshot["unborn"] and snapshot["head"] is None
    assert snapshot["commits"] == []


def test_rename_porcelain_and_bounded_change_list(repository: Path):
    git(repository, "mv", "README.md", "renamed readme.md")
    result = git_snapshot(repository)
    assert result["counts"]["staged"] == 1
    assert len(result["changes"]) == 1
    assert result["changes"][0]["path"] == "renamed readme.md"
    assert result["changes"][0]["status"].startswith("R")

    for i in range(MAX_CHANGES + 4):
        (repository / f"new-{i:03}.txt").write_text(str(i))
    result = git_snapshot(repository)
    assert result["changes_truncated"] and len(result["changes"]) == MAX_CHANGES
    assert result["counts"]["untracked"] == MAX_CHANGES + 4


def test_no_project_and_non_repository_report_expected_state(tmp_path: Path):
    assert not git_snapshot(None)["available"]
    other = tmp_path / "folder"
    other.mkdir()
    info = git_snapshot(other)
    assert info["available"] is False and "not a Git repository" in info["reason"]


def test_authenticated_endpoint_only_reads_selected_workspace(repository: Path, tmp_path: Path):
    workspace = Workspace(home=tmp_path)
    client = TestClient(create_app(workspace))
    assert client.get("/api/git/snapshot").status_code == 403
    headers = {"X-Codingbrain-Token": workspace.secret}
    assert client.get("/api/git/snapshot", headers=headers).json()["available"] is False
    workspace.choose(str(repository))
    result = client.get("/api/git/snapshot", headers=headers)
    assert result.status_code == 200
    snapshot = result.json()
    assert snapshot["branch"] == "main"
    assert len(snapshot["commits"]) == 1
    # Never accept a path query parameter to inspect arbitrary repos.
    assert client.get("/api/git/snapshot", params={"path": str(tmp_path)}, headers=headers).json()["branch"] == "main"


def test_porcelain_parser_does_not_require_printable_only_filenames():
    changes, counts = _changes("?? new\tname.txt\0 M a\nname.py\0")
    assert changes[0]["path"] == "new\tname.txt"
    assert changes[1]["path"] == "a\nname.py"
    assert counts["untracked"] == 1 and counts["modified"] == 1


@pytest.mark.parametrize("setting, value", [
    ("core.fsmonitor", "touch {sentinel}; false"),
    ("filter.planted.clean", "touch {sentinel}; cat"),
    ("filter.planted.process", "touch {sentinel}"),
])
def test_repository_configured_commands_never_run(repository: Path, tmp_path: Path, setting, value):
    """A folder received from someone else can carry a .git/config naming programs that `git status`
    would run. Selecting the project (and the 3-second polling) must never start them."""
    import sys
    if sys.platform == "win32":
        pytest.skip("the planted commands are POSIX shell; the setting check itself is platform-independent")
    sentinel = tmp_path / "PWNED"
    git(repository, "config", "--local", setting, value.format(sentinel=sentinel))
    (repository / ".gitattributes").write_text("* filter=planted\n")
    (repository / "README.md").write_text("changed so status would re-check it\n")
    snapshot = git_snapshot(repository)
    assert not sentinel.exists()
    assert snapshot["available"] and snapshot["branches"] and snapshot["changes"] == []
    assert "programs" in snapshot["changes_paused"]


def test_user_level_filters_do_not_pause_the_view(repository: Path, tmp_path: Path, monkeypatch):
    """Filters the user installed themselves (Git LFS, in the global config) are theirs to run."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[filter \"lfs\"]\n\tprocess = git-lfs filter-process\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    (repository / "README.md").write_text("changed\n")
    snapshot = git_snapshot(repository)
    assert snapshot["changes_paused"] is None and snapshot["counts"]["modified"] == 1
