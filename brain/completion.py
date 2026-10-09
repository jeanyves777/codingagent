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


# Validating a failing check before it may drive a repair -------------------------------------
# A failing model-written check is evidence against the implementation only when it fails on
# behavior. When it fails at the boundary between the check and the code (a name the check never
# defined, or an interface the goal never named), the check itself is the likely defect.
BLOCK = re.compile(r"^_{2,} (\S+?) _{2,}$", re.M)
ORIGIN = re.compile(r"^(\S+\.py):\d+: (\w+)$", re.M)
INTERFACE = [
    (re.compile(r"NameError: name '(\w+)' is not defined"), "uses a name it never defines or imports"),
    (re.compile(r"ImportError: cannot import name '(\w+)'"), "imports a name"),
    (re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'"), "imports a module"),
    (re.compile(r"TypeError: .*unexpected keyword argument '(\w+)'"), "passes a keyword argument"),
    (re.compile(r"AttributeError: .*has no attribute '(\w+)'"), "uses an attribute"),
    (re.compile(r"TypeError: .*(?:required positional argument|positional arguments? but)"), "calls with a signature"),
]


def failing_blocks(output: str) -> dict[str, str]:
    """Failure blocks from pytest's FAILURES section, keyed by test function name."""
    section = output.split("= FAILURES =", 1)[-1] if "= FAILURES =" in output else output
    marks = list(BLOCK.finditer(section))
    blocks = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(section)
        name = mark.group(1).split("[")[0].split(".")[-1]
        if name.startswith("test"):
            blocks[name] = section[mark.end():end].split("short test summary info")[0]
    return blocks


def assess_failures(output: str, checks: dict, goal: str) -> dict:
    """Split failing checks into invalid ones (their own defect, or an interface the goal does not
    state) and requirement failures (behavior), each with a stable signature."""
    goal_words = set(re.findall(r"[A-Za-z_]\w*", goal))
    invalid, failures = {}, {}
    for name, block in failing_blocks(output).items():
        errors = [line[1:].strip() for line in block.splitlines() if line.startswith("E ")]
        origins = ORIGIN.findall(block)
        in_check = bool(origins) and Path(origins[-1][0]).name == checks["path"]
        reason = None
        for pattern, what in INTERFACE:
            found = next((pattern.search(line) for line in errors if pattern.search(line)), None)
            if not found:
                continue
            named = found.group(1) if found.groups() else None
            if what.startswith("uses a name it never"):
                reason = f"the check {what}: {named}"
            elif in_check and (named is None or named.split(".")[-1] not in goal_words):
                reason = f"the check {what} the goal does not state" + (f": {named}" if named else "")
            break
        if reason:
            invalid[name] = reason
        else:
            detail = next((line for line in errors if "Error" in line or "assert" in line), errors[0] if errors else "")
            failures[name] = re.sub(r"0x[0-9a-f]+", "0x?", detail)[:300]
    return {"invalid": invalid, "failures": failures, "summary": "\n".join(
        f"{name}: {detail}" for name, detail in failures.items())[:3000]}


def without_tests(checks: dict, names: set[str]) -> dict:
    """The checks file with the named test functions removed (methods included)."""
    content = checks["content"]
    tree = ast.parse(content)
    lines = content.splitlines(keepends=True)
    spans = [((node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1, node.end_lineno)
             for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    kept = "".join(lines)
    remaining = [node for node in ast.walk(ast.parse(kept))
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")]
    return {**checks, "content": kept, "tests": len(remaining)}
