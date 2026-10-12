"""GitHub verification governance: tests run on GitHub Actions for opted-in projects, the agent's
commit goes only to codingbrain/verify-*, .github/ is never pushed, the worktree is never touched,
and merging needs the user's confirmation of a specific head with every check green."""
import json
import subprocess
from pathlib import Path

import pytest

from brain import remote_verify
from brain.local import config as settings
from brain.local import pullrequests


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "main")
    workspace = tmp_path / "data" / "tasks" / "abcdef1234" / "workspace"
    workspace.parent.mkdir(parents=True)
    git(root, "worktree", "add", "-q", "--detach", str(workspace), "HEAD")
    return root, workspace, origin


class FakeGitHub:
    """Stands in for the gh CLI; git commands run for real."""

    def __init__(self, monkeypatch, conclusion="success", log="", checks=None, head="h" * 40, mergeable="MERGEABLE"):
        self.calls, self.conclusion, self.log = [], conclusion, log
        self.checks = checks if checks is not None else [{"name": "tests", "conclusion": "SUCCESS"}]
        self.head, self.mergeable, self.request = head, mergeable, None
        real = remote_verify._run
        monkeypatch.setattr(remote_verify, "_gh", lambda: "gh")
        monkeypatch.setattr(remote_verify.time, "sleep", lambda seconds: None)

        def run(command, **options):
            if command[0] != "gh":
                return real(command, **options)
            self.calls.append(command)
            verb = command[1:3]
            if verb == ["workflow", "run"]:
                self.request = next(arg.split("=", 1)[1] for arg in command if arg.startswith("request_id="))
                return ""
            if verb == ["run", "list"]:
                return json.dumps([{"databaseId": 7, "displayTitle": f"Coding Brain verify {self.request}"}])
            if verb == ["run", "view"] and "--log-failed" in command:
                return self.log
            if verb == ["run", "view"]:
                return json.dumps({"status": "completed", "conclusion": self.conclusion})
            if verb == ["pr", "view"]:
                return json.dumps({"state": "OPEN", "isDraft": True, "mergeable": self.mergeable,
                                   "headRefOid": self.head, "statusCheckRollup": self.checks})
            if verb == ["pr", "create"]:
                return "https://github.com/owner/repo/pull/12\n"
            return ""
        monkeypatch.setattr(remote_verify, "_run", run)


def verifier(root, **extra):
    return remote_verify.GitHubVerifier(root, {"repository": "owner/repo", "trusted_ref": "chat-context", **extra})


def test_snapshot_leaves_worktree_index_and_head_untouched(project):
    root, workspace, _ = project
    (workspace / "app.py").write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
    (workspace / "new.py").write_text("X = 1\n", encoding="utf-8")
    head, status = git(workspace, "rev-parse", "HEAD"), git(workspace, "status", "--porcelain")
    commit, base, changed = remote_verify.snapshot_commit(workspace, "m", {"tests/test_req.py": "def test_x():\n    pass\n"})
    assert base == head and set(changed) == {"app.py", "new.py", "tests/test_req.py"}
    assert git(workspace, "rev-parse", "HEAD") == head and git(workspace, "status", "--porcelain") == status
    assert not (workspace / "tests").exists()  # requirement checks travel only in the commit
    assert git(workspace, "show", f"{commit}:tests/test_req.py").startswith("def test_x")


def test_passing_run_pushes_only_the_verify_branch_from_the_trusted_ref(project, monkeypatch):
    root, workspace, origin = project
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b  # same\n", encoding="utf-8")
    gh = FakeGitHub(monkeypatch)
    result = verifier(root, tests="tests/test_app.py")(workspace, None, label="abcdef12")
    assert result["passed"] and result["profile"] == "github" and result["url"].endswith("/actions/runs/7")
    assert git(origin, "for-each-ref", "--format=%(refname)").split() == [
        "refs/heads/codingbrain/verify-abcdef12", "refs/heads/main"]
    dispatch = next(call for call in gh.calls if call[1:3] == ["workflow", "run"])
    assert dispatch[dispatch.index("--ref") + 1] == "chat-context"  # the workflow never comes from the agent branch
    assert f"ref={git(origin, 'rev-parse', 'codingbrain/verify-abcdef12')}" in dispatch
    assert "tests=tests/test_app.py" in dispatch


def test_failing_run_returns_the_log_for_the_repair_loop(project, monkeypatch):
    root, workspace, _ = project
    (workspace / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    FakeGitHub(monkeypatch, conclusion="failure",
               log="tests\tTests\t2026-10-12T10:00:00.0000000Z FAILED tests/test_app.py::test_add - assert -1 == 3")
    result = verifier(root)(workspace, None)
    assert result["passed"] is False and result["exit_code"] == 1 and not result.get("infrastructure")
    assert "FAILED tests/test_app.py::test_add - assert -1 == 3" in result["output"]
    assert "2026-10-12T" not in result["output"]


def test_github_folder_changes_are_never_pushed(project, monkeypatch):
    root, workspace, origin = project
    (workspace / ".github" / "workflows").mkdir(parents=True)
    (workspace / ".github" / "workflows" / "x.yml").write_text("on: push\n", encoding="utf-8")
    gh = FakeGitHub(monkeypatch)
    result = verifier(root)(workspace, None)
    assert result["passed"] is False and result.get("infrastructure") and ".github/" in result["output"]
    assert not gh.calls and git(origin, "for-each-ref", "--format=%(refname)") == "refs/heads/main"


def test_syntax_errors_are_caught_locally_before_any_ci_run(project, monkeypatch):
    root, workspace, origin = project
    (workspace / "app.py").write_text("def add(a, b)\n    return a + b\n", encoding="utf-8")
    gh = FakeGitHub(monkeypatch)
    result = verifier(root)(workspace, None)
    assert result["exit_code"] == 1 and "app.py:1" in result["output"] and not gh.calls
    assert git(origin, "for-each-ref", "--format=%(refname)") == "refs/heads/main"


def test_unsafe_test_selection_is_refused():
    with pytest.raises(ValueError):
        remote_verify.GitHubVerifier(Path("."), {"repository": "o/r", "tests": "tests; curl evil"})


def test_merge_needs_green_checks_and_the_confirmed_head(project, monkeypatch):
    root, _, _ = project
    options = {"repository": "owner/repo"}
    url = "https://github.com/owner/repo/pull/12"
    FakeGitHub(monkeypatch, checks=[{"name": "tests", "status": "IN_PROGRESS"}])
    with pytest.raises(remote_verify.RemoteUnavailable, match="pending"):
        remote_verify.merge_pull_request(root, options, url, "h" * 40)
    FakeGitHub(monkeypatch, checks=[{"name": "tests", "conclusion": "FAILURE"}])
    assert remote_verify.pull_request_checks(root, options, url)["green"] is False
    gh = FakeGitHub(monkeypatch)
    with pytest.raises(remote_verify.RemoteUnavailable, match="changed after you were asked"):
        remote_verify.merge_pull_request(root, options, url, "0" * 40)
    with pytest.raises(remote_verify.RemoteUnavailable):
        remote_verify.merge_pull_request(root, options, url, "")
    remote_verify.merge_pull_request(root, options, url, "h" * 40)
    merge = next(call for call in gh.calls if call[1:3] == ["pr", "merge"])
    assert "--match-head-commit" in merge and "h" * 40 in merge and "--admin" not in merge
    assert any(call[1:3] == ["pr", "ready"] for call in gh.calls)  # the draft is marked ready only now


def test_no_checks_is_not_green(project, monkeypatch):
    root, _, _ = project
    FakeGitHub(monkeypatch, checks=[])
    assert remote_verify.pull_request_checks(root, {"repository": "o/r"}, "u")["green"] is False


def test_verification_is_per_project_and_never_leaks(tmp_path):
    config = settings._merge(settings.DEFAULTS, {})
    on, off = tmp_path / "on", tmp_path / "off"
    config["verification"]["projects"][str(on.resolve())] = {"enabled": True, "repository": "o/r"}
    assert settings.verification_for(config, on)["repository"] == "o/r"
    assert settings.verification_for(config, off) is None


def test_pull_request_status_asks_to_merge_only_when_green(project, monkeypatch):
    root, _, _ = project

    class Context:
        config = {"verification": {"projects": {str(root.resolve()): {"enabled": True, "repository": "owner/repo"}}}}
    Context.root = root
    task = {"pull_request": {"url": "https://github.com/owner/repo/pull/12"}}
    FakeGitHub(monkeypatch)
    state = pullrequests.status(Context, task)
    assert state["ask_merge"] and "Merge it?" in state["question"] and state["head"] == "h" * 40
    FakeGitHub(monkeypatch, checks=[{"name": "tests", "status": "QUEUED"}])
    assert pullrequests.status(Context, task)["question"] is None


def test_service_uses_the_verifier_only_when_opted_in(tmp_path):
    from brain.sandbox import run_tests
    from brain.service import Brain
    brain = Brain(tmp_path / "repos", tmp_path / "data", None, "img")
    assert brain.tester({"repository": "x", "id": "1"}) is run_tests
