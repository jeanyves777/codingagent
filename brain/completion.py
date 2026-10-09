"""Completion verification: requirement checks written from the goal before implementation.

Visible tests can be too thin to show that a goal is met (the pilot had six runs where the agent
believed it succeeded but acceptance tests failed). Before implementing, the free model writes
pytest checks for the behavior the goal states, from the goal and the visible repository only;
it never sees hidden or evaluation tests. The checks run in an isolated copy of the workspace
after the visible tests pass and are never committed, so they cannot change the deliverable.
"""
import ast
import json
import re
import shutil
import tempfile
from pathlib import Path

REQUIREMENT_PROMPT = (
    "Do not implement anything. Write pytest tests that check the behavior the goal below states "
    "explicitly, so that the finished work can be verified. Put them in exactly one new file named "
    "{name}. Test only requirements written in the goal (examples, edge cases and error behavior it "
    "names), import the code the way the existing tests do, and do not test anything the goal does "
    "not ask for. The current code may fail these tests; that is expected.\n\nGoal:\n{goal}")
NAME = re.compile(r"^test_requirements_[a-f0-9]{8}\.py$")


def requirement_file_name(task_id: str) -> str:
    return f"test_requirements_{task_id[:8]}.py"


def requirement_goal(task: dict) -> str:
    return REQUIREMENT_PROMPT.format(name=requirement_file_name(task["id"]), goal=task["goal"])


def accept_requirement_tests(task: dict, raw: str) -> dict | None:
    """Keep only one new, syntactically valid pytest file with at least one test function."""
    try:
        proposal = json.loads(raw)
        changes = proposal.get("changes") or []
    except (ValueError, AttributeError):
        return None
    expected = requirement_file_name(task["id"])
    chosen = [change for change in changes if isinstance(change, dict) and change.get("path") == expected]
    if len(chosen) != 1 or not NAME.match(expected):
        return None
    content = str(chosen[0].get("content", ""))
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    tests = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test")]
    if not tests or len(content) > 20_000:
        return None
    # Small models often use pytest without importing it; add the import deterministically.
    if "pytest." in content and not re.search(r"^\s*import pytest\b", content, re.M):
        content = "import pytest\n" + content
    # Checks that reference undefined names would fail on their own defect, not the work's.
    from .validators import static_issues
    if any(item.category == "Undefined name" for item in static_issues(expected, content, lambda name: None)):
        return None
    return {"path": expected, "content": content, "tests": len(tests)}


def run_requirement_checks(workspace: Path, checks: dict, run_tests, image, should_cancel) -> dict:
    """Run the visible tests plus the requirement checks in a throwaway copy of the workspace."""
    with tempfile.TemporaryDirectory(prefix="coding-brain-requirements-") as scratch:
        copy = Path(scratch) / "workspace"
        shutil.copytree(workspace, copy, ignore=shutil.ignore_patterns(".git"))
        (copy / checks["path"]).write_text(checks["content"], encoding="utf-8")
        for path in [copy, *copy.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        return run_tests(copy, image, should_cancel=should_cancel)
