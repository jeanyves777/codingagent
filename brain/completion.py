"""Completion verification: requirement checks written from the goal before implementation.

Visible tests can be too thin to show that a goal is met (the pilot had six runs where the agent
believed it succeeded but acceptance tests failed). Before implementing, the free model writes
checks for the behavior the goal states, from the goal and the visible repository only; it never
sees hidden or evaluation tests. The checks are written in the language of the project's test
profile (pytest for Python, node:test for Node), run explicitly with that language's runner in an
isolated copy of the workspace after the visible tests pass, and are never committed, so they
cannot change the deliverable. A run counts as verification only with evidence that the checks
actually executed and passed: zero collected, skipped or missing checks are never success.
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
PROMPTS = {
    "python": REQUIREMENT_PROMPT,
    "node": REQUIREMENT_PROMPT.replace(
        "Write pytest tests", "Write tests for Node's built-in test runner as an ES module (import test from "
        "'node:test' and assert from 'node:assert/strict'; one test(...) call per check; no skip, todo or only)").replace(
        "Put them in exactly one new file named {name}.", "Put them in exactly one new file named {name}, "
        "either in the project root or in the folder of the existing tests (for example test/{name}); "
        "import the code with paths relative to where you put that file.").replace(
        "import the code the way the existing tests do, ", ""),
}
SUFFIXES = {"python": ".py", "node": ".test.mjs"}
# Python checks sit in the project root; Node checks may also sit next to the project's tests
# (a model writing for test/ imports '../src/x.js'). Directory names only: no '.', '..' or links.
NAME = re.compile(r"^(?:(?:[A-Za-z0-9_-]+/){0,3}test_requirements_[a-f0-9]{8}\.test\.mjs|"
                  r"test_requirements_[a-f0-9]{8}\.py)$")
NODE_TEST_CALL = re.compile(r"^\s*(?:test|it)\s*\(", re.M)
NODE_TEST_IMPORT = re.compile(r"""(?:from|require\()\s*['"]node:test['"]""")


def requirement_language(workspace: Path) -> str | None:
    """The language requirement checks are written in: the project's sandbox test profile.
    None when the profile is invalid or has no requirement-check runner."""
    from .sandbox import profile
    try:
        name = profile(workspace)["name"]
    except (ValueError, OSError):
        return None
    return name if name in SUFFIXES else None


def checks_language(path: str) -> str | None:
    """The language of a requirement checks file, from its fixed name."""
    if not isinstance(path, str) or not NAME.match(path):
        return None
    return "node" if path.endswith(SUFFIXES["node"]) else "python"


def requirement_file_name(task_id: str, language: str = "python") -> str:
    return f"test_requirements_{task_id[:8]}{SUFFIXES[language]}"


def requirement_goal(task: dict) -> str:
    """The checks prompt, in the language recorded on the task (task["requirement_language"])."""
    language = task.get("requirement_language", "python")
    return PROMPTS[language].format(name=requirement_file_name(task["id"], language), goal=task["goal"])


def pytest_tests(tree: ast.AST) -> list[str]:
    """Test functions pytest collects by default: module-level test* functions and test* methods
    of module-level Test* classes."""
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names += [item.name for item in node.body
                      if isinstance(item, ast.FunctionDef) and item.name.startswith("test")]
    return names


def accept_requirement_tests(task: dict, raw: str) -> dict | None:
    """Keep only one new requirement file, in the task's requirement language, with at least one test."""
    language = task.get("requirement_language", "python")
    try:
        proposal = json.loads(raw)
        changes = proposal.get("changes") or []
    except (ValueError, AttributeError):
        return None
    if language not in SUFFIXES:
        return None
    expected = requirement_file_name(task["id"], language)
    chosen = [change for change in changes if isinstance(change, dict) and isinstance(change.get("path"), str)
              and NAME.match(change["path"]) and change["path"].rsplit("/", 1)[-1] == expected]
    if len(chosen) != 1:
        return None
    path = chosen[0]["path"]
    content = str(chosen[0].get("content", ""))
    from .validators import FENCED
    fenced = FENCED.match(content)  # small models often wrap the whole file in one Markdown fence
    if fenced:
        content = fenced.group(1) + "\n"
    if len(content) > 20_000:
        return None
    if language == "node":
        # No JavaScript parser here: a check that does not load fails closed when it runs.
        count = len(NODE_TEST_CALL.findall(content))
        if not count or not NODE_TEST_IMPORT.search(content):
            return None
        return {"path": path, "content": content, "tests": count, "language": "node"}
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    tests = pytest_tests(tree)
    if not tests:
        return None
    # Small models often use pytest without importing it; add the import deterministically.
    if "pytest." in content and not re.search(r"^\s*import pytest\b", content, re.M):
        content = "import pytest\n" + content
    # Checks that reference undefined names would fail on their own defect, not the work's.
    from .validators import static_issues
    if any(item.category == "Undefined name" for item in static_issues(expected, content, lambda name: None)):
        return None
    return {"path": expected, "content": content, "tests": len(tests), "language": "python"}


def run_requirement_checks(workspace: Path, checks: dict, run_tests, image, should_cancel) -> dict:
    """Run the requirement checks, explicitly and only them, with their language's runner in a
    throwaway copy of the workspace. The checks come from the task record and overwrite any file of
    the same name, so the work under test can neither edit nor replace them."""
    with tempfile.TemporaryDirectory(prefix="coding-brain-requirements-") as scratch:
        copy = Path(scratch) / "workspace"
        shutil.copytree(workspace, copy, ignore=shutil.ignore_patterns(".git"))
        target = copy / checks["path"]
        # every existing part of the path must be a real folder inside the copy, and the file itself
        # must not be a link or a folder: the work under test cannot redirect where the checks go
        parts = [copy.joinpath(*Path(checks["path"]).parts[:index]) for index in range(1, len(Path(checks["path"]).parts))]
        if target.is_symlink() or target.is_dir() or any(part.is_symlink() or (part.exists() and not part.is_dir())
                                                          for part in parts):
            return {"passed": False, "exit_code": None, "output": "The requirement checks path is taken by the work"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(checks["content"], encoding="utf-8")
        for path in [copy, *copy.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        return run_tests(copy, image, should_cancel=should_cancel, checks=checks["path"])


PYTEST_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")
TAP_COUNT = re.compile(r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)\s*$", re.M)


def execution_gap(evidence: dict, checks: dict) -> str | None:
    """Why a passing run does not prove the requirement checks ran and passed, or None when it does:
    the runner must report at least as many executed, passing checks as were written, and none
    failed, skipped, deselected, cancelled or left to do."""
    output, expected = str(evidence.get("output") or ""), int(checks.get("tests") or 0)
    if evidence.get("exit_code") != 0 or not evidence.get("passed"):
        return f"the check run did not succeed (exit {evidence.get('exit_code')})"
    if expected < 1:
        return "no requirement checks were written"
    language = checks.get("language") or checks_language(checks.get("path", ""))
    if language == "python":
        if "no tests ran" in output:
            return "pytest ran no tests"
        counts = {}
        for number, kind in PYTEST_COUNT.findall(output):
            counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(number)
        weakened = {kind: count for kind, count in counts.items() if kind != "passed" and count}
        if weakened:
            return "pytest reported " + ", ".join(f"{count} {kind}" for kind, count in weakened.items())
        ran = counts.get("passed", 0)
    elif language == "node":
        if re.search(rf"^\s*(?:not )?ok \d+ - {_file_entry(checks)}\s*$", output, re.M):
            return "the checks file produced no tests of its own"  # node --test reports a file without tests
        counts = {kind: int(number) for kind, number in TAP_COUNT.findall(output)}
        if "tests" not in counts or "pass" not in counts:
            return "the node test runner reported no test summary"
        weakened = {kind: counts.get(kind, 0) for kind in ("fail", "cancelled", "skipped", "todo") if counts.get(kind)}
        if weakened:
            return "node --test reported " + ", ".join(f"{count} {kind}" for kind, count in weakened.items())
        if counts["pass"] != counts["tests"]:
            return f"node --test passed {counts['pass']} of {counts['tests']} tests"
        ran = counts["pass"]
    else:
        return "requirement checks in this language have no runner"
    if ran < expected:
        return f"{ran} of {expected} requirement check(s) ran"
    return None


def load_failure(evidence: dict, checks: dict) -> bool:
    """A Node checks file that failed as a whole (it did not load: syntax, missing import, a
    top-level error) is the check's own defect, like a pytest collection error."""
    return (checks.get("language") or checks_language(checks.get("path", ""))) == "node" and bool(
        re.search(rf"^\s*not ok \d+ - {_file_entry(checks)}\s*$", str(evidence.get("output") or ""), re.M))


def _file_entry(checks: dict) -> str:
    """node --test names a file-level entry by the path it was given (or just its name)."""
    path = str(checks.get("path", ""))
    return "(?:" + "|".join(sorted({re.escape(path), re.escape(Path(path).name)})) + ")"


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
    if (checks.get("language") or checks_language(checks.get("path", ""))) == "node":
        return node_failures(output)
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


TAP_FAILED = re.compile(r"^(\s*)not ok \d+ - (.+?)(?: # .*)?$", re.M)


def node_failures(output: str) -> dict:
    """Failing node:test checks from TAP output. Interface validation is pytest-specific, so no
    Node check is classed invalid: a failing one drives at most one repair before the result is
    reported unverified."""
    failures = {}
    for found in TAP_FAILED.finditer(output):
        indent, name = found.group(1), found.group(2).strip()
        block = output[found.end():found.end() + 3000].split("\n" + indent + "  ...")[0]
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        detail = ""
        for index, line in enumerate(lines):
            if line.startswith("error:"):
                value = line[len("error:"):].strip()
                detail = lines[index + 1] if value in ("|-", "|", ">-") and index + 1 < len(lines) else value
                break
        if re.search(r"subtests? failed", detail):
            continue  # a suite whose own checks are reported separately
        failures[name[:200]] = re.sub(r"0x[0-9a-f]+", "0x?", detail.strip("'"))[:300]
    return {"invalid": {}, "failures": failures, "summary": "\n".join(
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
    return {**checks, "content": kept, "tests": len(pytest_tests(ast.parse(kept)))}
