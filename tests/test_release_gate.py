"""The release gate (scripts/release_gate.py) refuses every candidate that is not fully verified."""
import importlib.util
import re
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("release_gate", Path(__file__).resolve().parents[1] / "scripts/release_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SHA = "d7c3ca4e8f5b70f2f08209f37905bb78a624a5ac"
HEAD = "cb099f96e9f24149b7c96893bdde9f705412e004"
GREEN = [{"name": name, "conclusion": "success", "app": {"slug": "github-actions"}, "completed_at": "2026-10-09T22:04:00Z"}
         for name in ("verify (windows-2022, Python 3.12, pwsh)", "verify (windows-2025, Python 3.12, powershell)",
                      "real Docker sandbox (ubuntu)")] + [
    {"name": "release", "conclusion": "skipped", "app": {"slug": "github-actions"}}]
PROTECTED = {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}]}


def fake_api(**overrides):
    responses = {f"commits/{SHA}/check-runs?per_page=100": {"check_runs": []},
                 f"commits/{SHA}/pulls": [{"merged_at": "x", "merge_commit_sha": SHA, "head": {"sha": HEAD}}],
                 f"commits/{HEAD}/check-runs?per_page=100": {"check_runs": GREEN},
                 "git/ref/tags/v0.9.1": None, "environments/production": PROTECTED}
    responses.update(overrides)
    return lambda path: responses.get(path)


def run(api=None, version="0.9.1", sha=SHA, trees=None, pyproject='version = "0.9.1"', changelog="## 0.9.1\n",
        on_main=True, latest="v0.9.0"):
    trees = trees or {SHA: "tree1", HEAD: "tree1"}
    files = {"pyproject.toml": pyproject, "CHANGELOG.md": changelog}
    return gate.check(sha, version, "o/r", api or fake_api(), tree_of=lambda commit: trees[commit],
                      show=lambda commit, path: files[path], ancestor=lambda commit: on_main, latest=latest)


def test_merge_commit_with_identical_tree_to_a_green_pr_head_is_releasable():
    result = run()
    assert result["tested_commit"] == HEAD and result["checks"]["real Docker sandbox (ubuntu)"] == "success"


def test_commit_with_its_own_green_ci_is_releasable():
    result = run(api=fake_api(**{f"commits/{SHA}/check-runs?per_page=100": {"check_runs": GREEN}}))
    assert result["tested_commit"] == SHA


@pytest.mark.parametrize("change,message", [
    ({"sha": "d7c3ca4"}, "full 40-character"),
    ({"version": "latest"}, "not a release version"),
    ({"on_main": False}, "not on main"),
    ({"pyproject": 'version = "0.9.0"'}, "says 0.9.0"),
    ({"changelog": "## 0.9.0\n"}, "no '## 0.9.1' section"),
    ({"latest": "v0.10.0"}, "not newer"),
    ({"latest": "v0.9.1"}, "not newer"),
    ({"trees": {SHA: "tree1", HEAD: "other"}}, "no CI results"),
])
def test_refusals(change, message):
    with pytest.raises(gate.Refused, match=message):
        run(**change)


def test_existing_tag_is_refused():
    with pytest.raises(gate.Refused, match="already exists"):
        run(api=fake_api(**{"git/ref/tags/v0.9.1": {"object": {"sha": SHA}}}))


@pytest.mark.parametrize("runs,message", [
    ([dict(GREEN[0], conclusion="failure")] + GREEN[1:], "failure"),
    ([dict(GREEN[0], conclusion=None, status="in_progress")] + GREEN[1:], "in_progress"),
    (GREEN[:2] + GREEN[3:], "real Docker sandbox"),  # sandbox check missing
    (GREEN[2:], "verify ("),  # no Windows verify
    ([dict(run, app={"slug": "some-other-app"}) for run in GREEN], "no CI results"),  # only GitHub Actions counts
])
def test_ci_must_be_complete_and_green(runs, message):
    with pytest.raises(gate.Refused, match=re.escape(message)):
        run(api=fake_api(**{f"commits/{HEAD}/check-runs?per_page=100": {"check_runs": runs}}))


def test_a_rerun_that_passed_replaces_an_earlier_failure():
    rerun = [dict(GREEN[0], conclusion="failure", completed_at="2026-10-09T21:00:00Z")] + GREEN
    assert run(api=fake_api(**{f"commits/{HEAD}/check-runs?per_page=100": {"check_runs": rerun}}))


@pytest.mark.parametrize("environment", [None, {"protection_rules": []},
                                         {"protection_rules": [{"type": "wait_timer", "wait_timer": 5}]},
                                         {"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}])
def test_production_must_require_a_reviewer(environment):
    with pytest.raises(gate.Refused, match="require a reviewer"):
        run(api=fake_api(**{"environments/production": environment}))


def test_workflow_never_interpolates_inputs_into_shell():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text()
    for line in text.splitlines():
        if "${{ inputs." in line:
            stripped = line.strip()
            assert stripped.startswith(("SHA:", "VERSION:", "ref:")), line  # env or checkout input only
    assert "environment: production" in text and "release_gate.py" in text
