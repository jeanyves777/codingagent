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
        self.runs = {}  # run id -> (status, conclusion) for background runs
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
                status, conclusion = self.runs.get(int(command[3]), ("completed", self.conclusion))
                return json.dumps({"status": status, "conclusion": conclusion})
            if verb == ["pr", "view"]:
                return json.dumps({"state": "OPEN", "isDraft": True, "mergeable": self.mergeable,
                                   "headRefOid": self.head, "statusCheckRollup": self.checks})
            if verb == ["pr", "create"]:
                return "https://github.com/owner/repo/pull/12\n"
            return ""
        monkeypatch.setattr(remote_verify, "_run", run)


def verifier(root, **extra):
    """Blocking mode (wait: true) unless a test asks for the default background mode."""
    return remote_verify.GitHubVerifier(root, {"repository": "owner/repo", "trusted_ref": "chat-context", "wait": True,
                                               **extra})


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
    task = {"id": "abcdef12", "pull_request": {"url": "https://github.com/owner/repo/pull/12"}}
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


def test_background_mode_does_not_wait_for_github(project, monkeypatch):
    root, workspace, _ = project
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b  # v2\n", encoding="utf-8")
    gh = FakeGitHub(monkeypatch)
    result = verifier(root, wait=False)(workspace, None, label="abcdef12", extra={"tests/test_req.py": "X = 1\n"})
    assert result["passed"] and result["deferred"] and result["run"]["id"] == 7
    assert result["run"]["extra"] == ["tests/test_req.py"]
    assert not any(call[1:3] == ["run", "view"] for call in gh.calls)  # never polled: nothing blocks


def accepted_head(workspace, root):
    """The accepted commit: the workspace's files as a real commit (what the PR head holds)."""
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "-m", "accepted")
    return git(workspace, "rev-parse", "HEAD")


def test_runs_count_only_for_the_code_the_pull_request_holds(project, monkeypatch):
    root, workspace, _ = project
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b  # v3\n", encoding="utf-8")
    commit, _, _ = remote_verify.snapshot_commit(workspace, "m", {"tests/test_req.py": "X = 1\n"})
    head = accepted_head(workspace, root)
    url = "https://github.com/owner/repo/pull/12"
    run = {"id": 41, "commit": commit, "url": "u", "extra": ["tests/test_req.py"]}
    gh = FakeGitHub(monkeypatch, checks=[], head=head)
    gh.runs[41] = ("in_progress", None)
    state = remote_verify.pull_request_checks(root, {"repository": "owner/repo"}, url, [run])
    assert state["pending"] == 1 and not state["green"]  # still running: no merge question yet
    gh.runs[41] = ("completed", "success")
    assert remote_verify.pull_request_checks(root, {"repository": "owner/repo"}, url, [run])["green"]
    gh.runs[41] = ("completed", "failure")
    gh.log = "x\ty\t2026-10-12T00:00:00Z FAILED tests/test_app.py::test_add"
    state = remote_verify.pull_request_checks(root, {"repository": "owner/repo"}, url, [run])
    assert state["failed"] == 1 and "FAILED tests/test_app.py::test_add" in state["failure_log"]
    # A run of other code never counts for this head.
    stale = {**run, "extra": []}
    gh.runs[41] = ("completed", "success")
    assert not remote_verify.pull_request_checks(root, {"repository": "owner/repo"}, url, [stale])["green"]
    with pytest.raises(remote_verify.RemoteUnavailable):
        remote_verify.merge_pull_request(root, {"repository": "owner/repo"}, url, head, [stale])
    remote_verify.merge_pull_request(root, {"repository": "owner/repo"}, url, head, [run])


class Store(dict):
    def get(self, task_id):
        return self[task_id]

    def save(self, task):
        self[task["id"]] = task


def test_fix_it_repairs_the_same_pull_request_fast_forward(project, monkeypatch):
    root, workspace, origin = project
    git(root, "push", "-q", "origin", "main:refs/heads/codingbrain/add-thing")
    head = git(root, "rev-parse", "main")
    store = Store()
    store.save({"id": "orig0001", "goal": "Add a thing",
                "pull_request": {"url": "https://github.com/owner/repo/pull/12", "branch": "codingbrain/add-thing"}})

    class Context:
        config = {"verification": {"projects": {str(root.resolve()): {"enabled": True, "repository": "owner/repo"}}}}

        class brain:
            pass

        @staticmethod
        def tasks():
            return list(store.values())
    Context.root, Context.brain.store = root, store
    gh = FakeGitHub(monkeypatch, head=head, checks=[{"name": "tests", "conclusion": "FAILURE",
                                                    "detailsUrl": "https://github.com/owner/repo/actions/runs/99/job/1"}])
    gh.log = "x\ty\t2026-10-12T00:00:00Z FAILED tests/test_app.py::test_add"
    fix = pullrequests.prepare_fix(Context, store, "orig0001")
    assert fix["base_commit"] == head and fix["fixes"] == "orig0001"  # planned from the PR's own head
    assert "pull/12" in fix["goal"] and "FAILED tests/test_app.py::test_add" in fix["goal"]

    (workspace / "app.py").write_text("def add(a, b):\n    return a + b  # fixed\n", encoding="utf-8")
    commit = accepted_head(workspace, root)
    task = {"id": "fix00002", "goal": fix["goal"], "fixes": "orig0001", "branch": "codingbrain/local", "commit": commit}
    record = pullrequests.publish(Context, task)
    assert record == {"url": "https://github.com/owner/repo/pull/12", "branch": "codingbrain/add-thing",
                      "fix_of": "orig0001"}
    assert git(origin, "rev-parse", "codingbrain/add-thing") == commit  # same PR, fast-forward
    assert store["orig0001"]["pull_request"]["superseded_by"] == "fix00002"
    assert not any(call[1:3] == ["pr", "create"] for call in gh.calls)  # no second pull request
    with pytest.raises(ValueError, match="newer fix"):
        pullrequests.prepare_fix(Context, store, "orig0001")
    store.save(task)
    assert [item["task_id"] for item in pullrequests.open_reviews(Context, store)] == ["fix00002"]
