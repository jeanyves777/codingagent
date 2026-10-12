"""Verify a task's changes on GitHub Actions instead of the local sandbox (opt-in per project).

The governing rule: change -> verify remotely -> open a pull request -> the human merges.

- Nothing is pushed before the user approved the plan (this runs at the test step).
- The task's workspace is snapshotted into a commit with a temporary Git index, so the worktree, its
  index and HEAD are never touched, then pushed only to refs/heads/codingbrain/verify-<task>.
- Changes to .github/ are never pushed: CI stays human-controlled.
- The workflow is started from a trusted ref (the default branch, or one the owner configured): the
  workflow definition never comes from the agent's branch, only the code under test does. The
  workflow itself has read-only permissions and no secrets.
- A quick local syntax check of changed Python files runs first, so obvious mistakes cost no CI run.
The result has the same shape as brain.sandbox.run_tests, so the repair loop works unchanged.

By default the GitHub run is not waited for (settings "wait": false): GitHub validation never blocks
the rest of the day's work. The task records the run, the user can accept (a branch and a draft PR,
never a merge), and the run plus the PR's own checks are watched in the background; merging is offered
only once all of them are green. "wait": true keeps the blocking mode (the repair loop sees the result).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .github import parse_remote

WORKFLOW = "codingbrain-verify.yml"
POLL_SECONDS = 15
SAFE_TESTS = re.compile(r"^[\w./:\[\]-]+( [\w./:\[\]-]+)*$")


class RemoteUnavailable(RuntimeError):
    pass


def _gh() -> str:
    found = shutil.which("gh")
    if found:
        return found
    default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if default.is_file():
        return str(default)
    raise RemoteUnavailable("GitHub CLI (gh) is not installed")


def _run(command: list[str], cwd: Path | None = None, env: dict | None = None, timeout: float = 120,
         input: str | None = None) -> str:
    proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", input=input)
    if proc.returncode:
        raise RemoteUnavailable(f"{' '.join(command[:3])} failed: {(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc.stdout


def repo_slug(root: Path) -> str | None:
    """owner/name of the origin remote when it is on GitHub."""
    try:
        url = _run(["git", "-C", str(root), "remote", "get-url", "origin"], timeout=20).strip()
    except (RemoteUnavailable, OSError, subprocess.TimeoutExpired):
        return None
    try:
        return "/".join(parse_remote(url))
    except ValueError:
        return None


def snapshot_commit(workspace: Path, message: str, extra: dict | None = None) -> tuple[str, str, list[str]]:
    """(commit, base, changed paths) for the workspace's current files plus `extra` {path: text}
    (requirement checks), without touching its index, worktree or HEAD."""
    base = _run(["git", "-C", str(workspace), "rev-parse", "HEAD"], timeout=30).strip()
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index"),
               "GIT_AUTHOR_NAME": "Coding Brain", "GIT_AUTHOR_EMAIL": "codingbrain@localhost",
               "GIT_COMMITTER_NAME": "Coding Brain", "GIT_COMMITTER_EMAIL": "codingbrain@localhost"}
        _run(["git", "-C", str(workspace), "read-tree", base], env=env, timeout=60)
        _run(["git", "-C", str(workspace), "add", "-A"], env=env, timeout=120)
        for relative, content in (extra or {}).items():
            blob = _run(["git", "-C", str(workspace), "hash-object", "-w", "--stdin"], timeout=60, input=content).strip()
            _run(["git", "-C", str(workspace), "update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}"],
                 env=env, timeout=60)
        tree = _run(["git", "-C", str(workspace), "write-tree"], env=env, timeout=60).strip()
        commit = _run(["git", "-C", str(workspace), "commit-tree", tree, "-p", base, "-m", message],
                      env=env, timeout=60).strip()
    changed = [line for line in _run(["git", "-C", str(workspace), "diff", "--name-only", base, commit],
                                     timeout=60).splitlines() if line.strip()]
    return commit, base, changed


def local_syntax_errors(workspace: Path, changed: list[str]) -> str:
    """Syntax errors in changed Python files (no dependencies needed; seconds, not a CI run)."""
    problems = []
    for relative in changed:
        path = workspace / relative
        if path.suffix == ".py" and path.is_file():
            try:
                compile(path.read_bytes(), relative, "exec", dont_inherit=True)  # checks only; writes nothing
            except SyntaxError as error:
                problems.append(f"{relative}:{error.lineno}: {error.msg}")
            except (OSError, ValueError):
                continue
    return "\n".join(problems)


class GitHubVerifier:
    """A drop-in for brain.sandbox.run_tests that runs the project's tests on GitHub Actions."""

    def __init__(self, root: Path, settings: dict, log=lambda text: None):
        self.root, self.settings, self.log = Path(root), settings, log
        self.slug = settings.get("repository") or repo_slug(self.root)
        self.trusted_ref = settings.get("trusted_ref") or "main"
        self.tests = str(settings.get("tests") or "").strip()
        self.timeout = int(settings.get("timeout_minutes") or 40) * 60
        self.wait = bool(settings.get("wait", False))
        if self.tests and not SAFE_TESTS.match(self.tests):
            raise ValueError("verification tests must be test paths or pytest node ids separated by spaces")

    def _result(self, passed, exit_code, output, **extra):
        return {"passed": passed, "exit_code": exit_code, "profile": "github", "output": output[:16000], **extra}

    def __call__(self, workspace, images=None, should_cancel=None, only=None, extra=None, label=None):
        workspace = Path(workspace)
        label = re.sub(r"[^\w.-]+", "-", label or workspace.parent.name)[:60] or "task"
        should_cancel = should_cancel or (lambda: False)
        try:
            if not self.slug:
                raise RemoteUnavailable("the project has no GitHub 'origin' remote")
            commit, base, changed = snapshot_commit(workspace, f"Coding Brain verification of {label}", extra)
            if any(path.startswith(".github/") for path in changed):
                return self._result(False, None, "Not verified: the change touches .github/. Workflow files stay "
                                    "human-controlled; Coding Brain never pushes them.", infrastructure=True)
            errors = local_syntax_errors(workspace, changed)
            if errors:  # caught locally in seconds: a normal test failure the repair loop can fix
                return self._result(False, 1, "Syntax errors (checked locally before any CI run):\n" + errors)
            branch = f"codingbrain/verify-{label}"
            self.log(f"Pushing {commit[:10]} to {branch}")
            _run(["git", "-C", str(workspace), "push", "--force", "--quiet", "origin", f"{commit}:refs/heads/{branch}"],
                 timeout=300)
            request = uuid.uuid4().hex[:12]
            args = [_gh(), "workflow", "run", WORKFLOW, "--repo", self.slug, "--ref", self.trusted_ref,
                    "-f", f"ref={commit}", "-f", f"request_id={request}"]
            nodes = " ".join(only or [])
            tests = nodes if nodes and SAFE_TESTS.match(nodes) else self.tests
            if tests:
                args += ["-f", f"tests={tests}"]
            _run(args, timeout=60)
            run_id = self._find_run(request)
            url = f"https://github.com/{self.slug}/actions/runs/{run_id}"
            self.log(f"Verifying on GitHub: {url}")
            if not self.wait:
                return self._result(True, 0, f"Local checks passed. The full tests run on GitHub in the background "
                                    f"({url}); nothing is merged until they and every pull request check are green.",
                                    url=url, deferred=True,
                                    run={"id": run_id, "commit": commit, "url": url, "extra": sorted(extra or {})})
            return self._wait(run_id, should_cancel)
        except RemoteUnavailable as error:
            return self._result(False, None, f"GitHub verification unavailable: {error}. Nothing was tested; check "
                                "the GitHub remote, 'gh auth status' and the codingbrain-verify workflow.",
                                infrastructure=True)
        except subprocess.TimeoutExpired as error:
            return self._result(False, None, f"GitHub verification did not answer in time ({error.cmd[:2]})",
                                infrastructure=True)

    def _find_run(self, request: str) -> int:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            runs = json.loads(_run([_gh(), "run", "list", "--repo", self.slug, "--workflow", WORKFLOW, "--limit", "20",
                                    "--json", "databaseId,displayTitle"], timeout=60) or "[]")
            for run in runs:
                if request in run.get("displayTitle", ""):
                    return int(run["databaseId"])
            time.sleep(4)
        raise RemoteUnavailable("the verification run did not appear on GitHub within 2 minutes")

    def _wait(self, run_id: int, should_cancel) -> dict:
        url = f"https://github.com/{self.slug}/actions/runs/{run_id}"
        deadline = time.monotonic() + self.timeout
        while True:
            if should_cancel():
                try:
                    _run([_gh(), "run", "cancel", str(run_id), "--repo", self.slug], timeout=60)
                except (RemoteUnavailable, subprocess.TimeoutExpired):
                    pass  # already finished, or GitHub unreachable: nothing more to stop
                return self._result(False, None, "Verification stopped by cancellation", cancelled=True, url=url)
            if time.monotonic() > deadline:
                return self._result(False, None, f"GitHub verification timed out after {self.timeout // 60} min: {url}",
                                    url=url)
            state = run_state(self.slug, run_id)
            if state.get("status") == "completed":
                break
            time.sleep(POLL_SECONDS)
        conclusion = state.get("conclusion")
        if conclusion == "success":
            return self._result(True, 0, f"Tests passed on GitHub Actions: {url}", url=url)
        if conclusion == "cancelled":
            return self._result(False, None, f"The verification run was cancelled on GitHub: {url}", url=url,
                                infrastructure=True)
        return self._result(False, 1, f"Tests failed on GitHub Actions ({url}):\n" + failure_log(self.slug, run_id),
                            url=url)


def run_state(slug: str, run_id: int) -> dict:
    return json.loads(_run([_gh(), "run", "view", str(run_id), "--repo", slug, "--json", "status,conclusion"],
                           timeout=60))


def failure_log(slug: str, run_id: int, lines: int = 200) -> str:
    try:
        log = _run([_gh(), "run", "view", str(run_id), "--repo", slug, "--log-failed"], timeout=120)
    except RemoteUnavailable as error:
        log = str(error)
    return "\n".join(re.sub(r"^\S+\t\S+\t\S+Z ", "", line) for line in log.splitlines()[-lines:])


def covers(root: Path, run: dict, head: str) -> bool:
    """Whether a verification run tested the PR head's files: identical, apart from the run's own extra
    files (requirement checks). A run of other code never counts for this head."""
    try:
        diff = _run(["git", "-C", str(root), "diff", "--name-only", head, run.get("commit", "")], timeout=60)
    except (RemoteUnavailable, subprocess.TimeoutExpired, OSError):
        return False
    return set(filter(None, diff.splitlines())) <= set(run.get("extra") or [])


def publish_pull_request(root: Path, settings: dict, branch: str, commit: str, title: str, body: str) -> str:
    """After the user accepted: push the accepted branch and open a draft PR. Merging stays with the human."""
    slug = settings.get("repository") or repo_slug(Path(root))
    if not slug:
        raise RemoteUnavailable("the project has no GitHub 'origin' remote")
    base = settings.get("pr_base") or settings.get("trusted_ref") or "main"
    _run(["git", "-C", str(root), "push", "--quiet", "origin", f"{commit}:refs/heads/{branch}"], timeout=300)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        body_file = handle.name
    try:
        out = _run([_gh(), "pr", "create", "--repo", slug, "--draft", "--base", base, "--head", branch,
                    "--title", title[:200], "--body-file", body_file], timeout=120)
    finally:
        os.unlink(body_file)
    match = re.search(r"https://github\.com/\S+/pull/\d+", out)
    return match.group(0) if match else out.strip()[:300]


def pull_request_checks(root: Path, settings: dict, url: str, runs=()) -> dict:
    """The PR's state, its checks and the task's verification runs that tested exactly the PR head's
    files: {"state", "draft", "mergeable", "head", "checks", "pending", "failed", "failure_log", "green"}.
    Green needs at least one check or run, none pending or failed, an open and mergeable PR."""
    slug = settings.get("repository") or repo_slug(Path(root))
    info = json.loads(_run([_gh(), "pr", "view", url, "--repo", slug, "--json",
                            "state,isDraft,mergeable,headRefOid,statusCheckRollup"], timeout=60))
    checks = []
    for check in info.get("statusCheckRollup") or []:
        status = (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        checks.append({"name": check.get("name") or check.get("context") or "check", "status": status})
    head, failure = info.get("headRefOid"), ""
    for run in runs or ():
        if not run.get("id") or not head or not covers(Path(root), run, head):
            continue  # it tested other files than the PR now holds
        found = run_state(slug, run["id"])
        status = "PENDING" if found.get("status") != "completed" else (found.get("conclusion") or "").upper()
        checks.append({"name": "Coding Brain verify", "status": status, "url": run.get("url")})
        if status not in {"PENDING", "SUCCESS"} and not failure:
            failure = f"{run.get('url')}\n" + failure_log(slug, run["id"], 120)
    pending = [c for c in checks if c["status"] in {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING"}]
    failed = [c for c in checks if c not in pending and c["status"] not in {"SUCCESS", "NEUTRAL", "SKIPPED"}]
    return {"state": info.get("state"), "draft": info.get("isDraft"), "mergeable": info.get("mergeable"),
            "head": head, "checks": checks, "pending": len(pending), "failed": len(failed),
            "failure_log": failure[-6000:],
            "green": bool(checks) and not pending and not failed and info.get("mergeable") == "MERGEABLE"
            and info.get("state") == "OPEN"}


def merge_pull_request(root: Path, settings: dict, url: str, confirmed_head: str, runs=()) -> str:
    """Merge only what the user confirmed: every check green, mergeable, and the head unchanged since
    the user was asked (a new push means a new question). Never with --admin: branch protection holds."""
    if not confirmed_head:
        raise RemoteUnavailable("merging needs the user's confirmation of a specific head commit")
    state = pull_request_checks(root, settings, url, runs)
    if not state["green"]:
        raise RemoteUnavailable(f"not merging: {state['failed']} failed, {state['pending']} pending check(s), "
                                f"mergeable={state['mergeable']}, state={state['state']}")
    if state["head"] != confirmed_head:
        raise RemoteUnavailable("not merging: the pull request changed after you were asked; ask again")
    slug = settings.get("repository") or repo_slug(Path(root))
    if state["draft"]:
        _run([_gh(), "pr", "ready", url, "--repo", slug], timeout=60)
    method = {"merge": "--merge", "rebase": "--rebase"}.get(settings.get("merge_method"), "--squash")
    _run([_gh(), "pr", "merge", url, "--repo", slug, method, "--match-head-commit", confirmed_head], timeout=120)
    return url
