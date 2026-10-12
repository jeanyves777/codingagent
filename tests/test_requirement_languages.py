"""Requirement checks must run in the project's own language and count as verification only with
evidence that they executed (regression: in a Node project pytest checks were never collected by
`npm test`, yet the task was reported as verified).

Three levels:
- unit tests of the evidence rules and the runner selection (everywhere);
- end-to-end Brain runs through the real `sandbox.run_tests` code path where only `docker run` is
  replaced by running the same command on this machine (needs node and pytest here);
- the same scenarios in the real Docker sandbox (CODINGBRAIN_TEST_DOCKER=1, both images built).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brain import sandbox
from brain.service import Brain
from tests.test_brain import create_direct, git

HAS_NODE = shutil.which("node") is not None and shutil.which("npm") is not None
DOCKER = bool(os.environ.get("CODINGBRAIN_TEST_DOCKER"))
IMAGES = {"python": "coding-brain-sandbox:0.1", "node": "coding-brain-node-sandbox:0.1"}
needs_node = pytest.mark.skipif(not HAS_NODE, reason="node and npm are needed to run Node tests on this machine")
needs_docker = pytest.mark.skipif(not DOCKER, reason="set CODINGBRAIN_TEST_DOCKER=1 with both sandbox images built")

NODE_IMPL = "export function add(a, b) { return a + b; }\n"
NODE_VISIBLE = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                "import { add } from '../src/add.js';\ntest('add', () => assert.equal(add(2, 3), 5));\n")
NODE_HEAD = "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { add } from './src/add.js';\n"
NODE_PASS = NODE_HEAD + "test('add(-1, 1) is 0', () => assert.equal(add(-1, 1), 0));\n"
NODE_FAIL = NODE_HEAD + "test('add(2, 2) is 5', () => assert.equal(add(2, 2), 5));\n"
NODE_SKIP = NODE_HEAD + "test('add(-1, 1) is 0', { skip: true }, () => assert.equal(add(-1, 1), 0));\n"
NODE_NONE = NODE_HEAD + "if (false) {\n  test('never', () => {});\n}\n"
NODE_BROKEN = NODE_HEAD + "test('x', () => {\n"
PY_IMPL = "def add(a, b):\n    return a + b\n"
PY_PASS = "from calc import add\n\n\ndef test_negative():\n    assert add(-1, 1) == 0\n"
PY_FAIL = "from calc import add\n\n\ndef test_wrong():\n    assert add(2, 2) == 5\n"
# Any language: a check that would fail if it were ever executed.
FAILS_IF_RUN = {".py": "def test_goal():\n    assert False, 'requirement check executed'\n", ".mjs": NODE_FAIL}


# Host execution of the sandbox command ---------------------------------------------------------

def host_docker(monkeypatch, missing=()):
    """Run the container command of sandbox.run_tests on this machine instead of in Docker: same
    command, same mounted workspace, same exit code and output. `missing` executables behave as an
    image without them (Docker exits 127)."""
    real_run, real_popen = subprocess.run, subprocess.Popen
    calls = []

    def translate(command, kwargs):
        mount = command[command.index("--mount") + 1]
        workspace = re.search(r"source=(.+?),target=/code", mount).group(1)
        inner = command[next(index for index, arg in enumerate(command) if arg in IMAGES.values() or arg == "img") + 1:]
        calls.append(inner)
        if inner[0] in missing:
            kwargs["stdout"].write(f'docker: Error response from daemon: exec: "{inner[0]}": executable file not '
                                   'found in $PATH\n'.encode())
            return [sys.executable, "-c", "raise SystemExit(127)"], workspace
        return [sys.executable if inner[0] == "python" else shutil.which(inner[0]) or inner[0], *inner[1:]], workspace

    def options(kwargs, workspace):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        return {"cwd": workspace, "stdout": kwargs.get("stdout"), "stderr": subprocess.STDOUT, "env": env}

    def run(command, **kwargs):
        if command[:2] != ["docker", "run"]:
            return real_run(command, **kwargs)
        resolved, workspace = translate(command, kwargs)
        return real_run(resolved, timeout=120, **options(kwargs, workspace))

    def popen(command, **kwargs):
        if command[:2] != ["docker", "run"]:
            return real_popen(command, **kwargs)
        resolved, workspace = translate(command, kwargs)
        return real_popen(resolved, **options(kwargs, workspace))
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    return calls


# Projects and a stand-in model -------------------------------------------------------------------

def repository(root: Path, files: dict) -> Path:
    path = root / "repos" / "demo"
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content, encoding="utf-8")
    git(path, "init")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def node_project(root: Path, extra=None) -> Path:
    return repository(root, {"package.json": json.dumps({"name": "demo", "private": True, "type": "module",
                                                         "scripts": {"test": "node --test"}}),
                             "coding-brain.json": json.dumps({"test_profile": "node", "test_command": ["npm", "test"]}),
                             "src/add.js": "export function add(a, b) { return a - b; }\n",
                             "test/add.test.js": NODE_VISIBLE, **(extra or {})})


def python_project(root: Path, extra=None) -> Path:
    return repository(root, {"calc.py": "def add(a, b):\n    return a - b\n",
                             "tests/test_calc.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__)"
                                                   ".parents[1]))\nfrom calc import add\n\n\ndef test_add():\n"
                                                   "    assert add(2, 0) == 2\n", **(extra or {})})


class StandIn:
    """Writes the checks in whatever file the requirement prompt names, then the implementation."""
    def __init__(self, checks: dict, implementation: dict):
        self.checks, self.implementation, self.prompts = checks, implementation, []

    async def propose(self, root, goal, memories, repository_context=None, **kwargs):
        self.prompts.append(goal)
        if goal.startswith("Do not implement anything"):
            name = re.search(r"named (test_requirements_\w+(?:\.test)?\.\w+)", goal).group(1)
            content = self.checks.get(Path(name).suffix, self.checks.get("*", ""))
            return json.dumps({"plan": "checks", "changes": [{"path": name, "content": content}]})
        return json.dumps({"plan": "p", "changes": [{"path": path, "content": content}
                                                     for path, content in self.implementation.items()]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}


def execute(root: Path, repo: Path, model, image="img", goal="Fix add so it adds; add(-1, 1) must be 0"):
    brain = Brain(repo.parent, root / "data", model, image, requirement_checks=True, max_free_attempts=2)

    async def flow():
        task = await create_direct(brain, goal)
        return await brain.execute(task["id"], task["digest"])
    return asyncio.run(flow())


def kinds(task):
    return [event["kind"] for event in task["events"]]


# The regression: checks that are never executed must never verify the work ------------------------

@needs_node
def test_node_project_checks_that_would_fail_are_executed_not_reported_verified(tmp_path, monkeypatch):
    calls = host_docker(monkeypatch)
    model = StandIn(FAILS_IF_RUN, {"src/add.js": NODE_IMPL})
    task = execute(tmp_path, node_project(tmp_path), model)
    assert task["status"] == "passed"  # the visible tests pass
    assert task["completion_verified"] is not True, task["completion"]
    assert task["completion"]["status"] == "unverified" and "completion_verified" not in kinds(task)
    assert "requirement_checks_failed" in kinds(task)
    # Generated for Node and run with node --test, explicitly, in the Node image.
    assert task["requirement_audit"][0]["content"] == NODE_FAIL
    assert ["node", "--test", "--test-reporter=tap"] == calls[1][:3] and calls[1][3].endswith(".test.mjs")
    assert "add(2, 2) is 5" in task["failure_log"][0]["summary"]


@needs_node
def test_python_checks_outside_the_projects_testpaths_are_executed(tmp_path, monkeypatch):
    """The same defect in Python: `testpaths = tests` meant the project command never collected
    the requirement file at the root."""
    host_docker(monkeypatch)
    repo = python_project(tmp_path, {"pytest.ini": "[pytest]\ntestpaths = tests\n"})
    task = execute(tmp_path, repo, StandIn(FAILS_IF_RUN, {"calc.py": PY_IMPL}))
    assert task["status"] == "passed" and task["completion_verified"] is not True, task["completion"]
    assert task["completion"]["status"] == "unverified" and "requirement_checks_failed" in kinds(task)


# Real execution: Node, Python and mixed repositories --------------------------------------------

@needs_node
def test_node_checks_that_pass_verify_with_execution_evidence(tmp_path, monkeypatch):
    calls = host_docker(monkeypatch)
    task = execute(tmp_path, node_project(tmp_path), StandIn({".mjs": NODE_PASS}, {"src/add.js": NODE_IMPL}))
    assert task["status"] == "passed" and task["completion_verified"] is True
    assert task["completion"] == {"status": "verified", "detail": "1 requirement check(s) passed"}
    assert calls[1][:3] == ["node", "--test", "--test-reporter=tap"]
    assert "'node:test'" in task["requirement_tests"]["content"] and task["requirement_tests"]["language"] == "node"


@needs_node
@pytest.mark.parametrize("checks, reason", [(NODE_SKIP, "1 skipped"), (NODE_NONE, "no tests of its own")],
                         ids=["skipped", "none-collected"])
def test_node_checks_skipped_or_never_collected_are_unverified(tmp_path, monkeypatch, checks, reason):
    host_docker(monkeypatch)
    task = execute(tmp_path, node_project(tmp_path), StandIn({".mjs": checks}, {"src/add.js": NODE_IMPL}))
    assert task["status"] == "passed" and task["completion_verified"] is False
    assert task["completion"]["status"] == "unverified" and reason in task["completion"]["detail"]
    assert "completion_unverified" in kinds(task) and "completion_verified" not in kinds(task)


@needs_node
def test_node_checks_that_do_not_load_are_discarded_never_verified(tmp_path, monkeypatch):
    host_docker(monkeypatch)
    task = execute(tmp_path, node_project(tmp_path), StandIn({".mjs": NODE_BROKEN}, {"src/add.js": NODE_IMPL}))
    assert task["completion_verified"] is False and task["completion"]["status"] == "inconclusive"
    assert "requirement_checks_discarded" in kinds(task) and not task.get("failure_log")


@needs_node
def test_missing_node_runner_in_the_sandbox_fails_closed(tmp_path, monkeypatch):
    host_docker(monkeypatch, missing=("node",))
    task = execute(tmp_path, node_project(tmp_path), StandIn({".mjs": NODE_PASS}, {"src/add.js": NODE_IMPL}))
    assert task["completion_verified"] is False and task["completion"]["status"] == "unverified"
    assert "infrastructure" in task["completion"]["detail"] and "completion_unverified" in kinds(task)


@needs_node
def test_the_work_cannot_replace_the_checks(tmp_path, monkeypatch):
    """The implementation writes a passing file under the checks' name (Python), or cannot write
    one at all (.mjs is not a source type it may write): the stored checks still run."""
    host_docker(monkeypatch)
    from brain.completion import run_requirement_checks
    from brain.repository import safe_path
    with pytest.raises(ValueError):
        safe_path(tmp_path, "test_requirements_0123abcd.test.mjs")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "calc.py").write_text(PY_IMPL)
    (workspace / "test_requirements_0123abcd.py").write_text("def test_ok():\n    assert True\n")
    checks = {"path": "test_requirements_0123abcd.py", "content": PY_FAIL, "tests": 1, "language": "python"}
    result = run_requirement_checks(workspace, checks, sandbox.run_tests, "img", lambda: False)
    assert result["exit_code"] == 1 and "1 failed" in result["output"]


@needs_node
def test_python_checks_still_verify_and_still_catch_failures(tmp_path, monkeypatch):
    calls = host_docker(monkeypatch)
    task = execute(tmp_path, python_project(tmp_path), StandIn({".py": PY_PASS}, {"calc.py": PY_IMPL}))
    assert task["completion_verified"] is True and calls[1][:3] == ["python", "-m", "pytest"]
    assert calls[1][-1].startswith("test_requirements_") and calls[1][-1].endswith(".py")
    failing = execute(tmp_path / "f", python_project(tmp_path / "f"), StandIn({".py": PY_FAIL}, {"calc.py": PY_IMPL}))
    assert failing["completion_verified"] is False and "requirement_checks_failed" in kinds(failing)


@needs_node
def test_mixed_repositories_write_and_run_checks_in_the_profile_language(tmp_path, monkeypatch):
    """pyproject.toml + package.json: the Python profile, so pytest checks, run by pytest.
    package.json + Python files without pyproject.toml: the Node profile, so node:test checks run by
    node. Either way a failing check is executed and caught, never skipped."""
    calls = host_docker(monkeypatch)
    mixed_python = python_project(tmp_path / "a", {
        "pyproject.toml": "[project]\nname = 'demo'\nversion = '0'\n[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        "package.json": json.dumps({"scripts": {"test": "node --test"}}), "web/app.js": "export const x = 1;\n"})
    task = execute(tmp_path / "a", mixed_python, StandIn(FAILS_IF_RUN, {"calc.py": PY_IMPL}))
    assert task["requirement_audit"][0]["content"] == FAILS_IF_RUN[".py"]
    assert task["completion_verified"] is False and "requirement_checks_failed" in kinds(task)
    assert any(call[:3] == ["python", "-m", "pytest"] and call[-1].endswith(".py") for call in calls)
    calls.clear()
    mixed_node = node_project(tmp_path / "b", {"tool.py": "def f():\n    return 1\n",
                                               "test_tool.py": "from tool import f\n\n\ndef test_f():\n    assert f() == 1\n"})
    task = execute(tmp_path / "b", mixed_node, StandIn(FAILS_IF_RUN, {"src/add.js": NODE_IMPL}))
    assert task["requirement_audit"][0]["content"] == NODE_FAIL
    assert task["completion_verified"] is False and "requirement_checks_failed" in kinds(task)
    assert any(call[:2] == ["node", "--test"] and call[-1].endswith(".test.mjs") for call in calls)
    passing = execute(tmp_path / "c", node_project(tmp_path / "c", {"tool.py": "x = 1\n"}),
                      StandIn({".mjs": NODE_PASS}, {"src/add.js": NODE_IMPL}))
    assert passing["completion_verified"] is True


# The same in the real Docker sandbox --------------------------------------------------------------

@needs_docker
@pytest.mark.parametrize("project, checks, implementation, verified", [
    ("node", NODE_FAIL, {"src/add.js": NODE_IMPL}, False),
    ("node", NODE_PASS, {"src/add.js": NODE_IMPL}, True),
    ("node", NODE_SKIP, {"src/add.js": NODE_IMPL}, False),
    ("python", PY_FAIL, {"calc.py": PY_IMPL}, False),
    ("python", PY_PASS, {"calc.py": PY_IMPL}, True),
    ("mixed", PY_FAIL, {"calc.py": PY_IMPL}, False),
])
def test_requirement_checks_in_the_real_sandbox(tmp_path, project, checks, implementation, verified):
    if project == "node":
        repo = node_project(tmp_path)
    else:
        repo = python_project(tmp_path, {"pyproject.toml": "[project]\nname = 'demo'\nversion = '0'\n",
                                         "package.json": "{}"} if project == "mixed" else None)
    model = StandIn({".mjs": checks} if project == "node" else {".py": checks}, implementation)
    task = execute(tmp_path, repo, model, image=IMAGES)
    assert task["status"] == "passed", task.get("test_evidence")
    assert task["completion_verified"] is verified, task["completion"]
    assert ("completion_verified" in kinds(task)) is verified


@needs_docker
def test_an_image_without_node_fails_closed_in_the_real_sandbox(tmp_path):
    """Node checks given an image with no node executable (the Python image): never a pass."""
    from brain.completion import run_requirement_checks
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "add.js").write_text(NODE_IMPL)
    (workspace / "package.json").write_text(json.dumps({"type": "module"}))
    checks = {"path": "test_requirements_0123abcd.test.mjs", "content": NODE_PASS, "tests": 1, "language": "node"}
    result = run_requirement_checks(workspace, checks, sandbox.run_tests, {"node": IMAGES["python"]}, lambda: False)
    assert result["exit_code"] in (126, 127) and not result["passed"], result


# Unit tests (everywhere) ----------------------------------------------------------------------------

def test_execution_evidence_rules():
    from brain.completion import execution_gap
    py = {"path": "test_requirements_0123abcd.py", "tests": 2, "language": "python"}
    ok = {"passed": True, "exit_code": 0}
    assert execution_gap({**ok, "output": "..\n2 passed in 0.01s\n"}, py) is None
    assert execution_gap({**ok, "output": "3 passed in 0.01s"}, py) is None  # parametrized checks
    for output in ("1 passed in 0.01s", "no tests ran in 0.01s", "2 passed, 1 skipped", "2 passed, 1 deselected",
                   "2 passed, 1 xfailed", "2 passed, 1 xpassed", "", "ok"):
        assert execution_gap({**ok, "output": output}, py), output
    assert execution_gap({"passed": False, "exit_code": 1, "output": "2 passed"}, py)
    assert execution_gap({"passed": True, "exit_code": None, "output": "2 passed"}, py)
    node = {"path": "test_requirements_0123abcd.test.mjs", "tests": 1, "language": "node"}
    tap = "ok 1 - adds\n1..1\n# tests {t}\n# suites 0\n# pass {p}\n# fail 0\n# cancelled 0\n# skipped {s}\n# todo {d}\n"
    assert execution_gap({**ok, "output": tap.format(t=1, p=1, s=0, d=0)}, node) is None
    assert execution_gap({**ok, "output": tap.format(t=2, p=1, s=1, d=0)}, node)
    assert execution_gap({**ok, "output": tap.format(t=2, p=1, s=0, d=1)}, node)
    assert execution_gap({**ok, "output": tap.format(t=0, p=0, s=0, d=0)}, node)
    assert execution_gap({**ok, "output": "> node --test\n"}, node)  # no summary at all
    empty = "ok 1 - test_requirements_0123abcd.test.mjs\n" + tap.format(t=1, p=1, s=0, d=0).split("\n", 1)[1]
    assert "no tests of its own" in execution_gap({**ok, "output": empty}, node)
    assert execution_gap({**ok, "output": tap.format(t=1, p=1, s=0, d=0)}, {**node, "tests": 2})
    assert execution_gap({**ok, "output": "1 passed"}, {"path": "x.rb", "tests": 1})  # no runner: never success
    assert execution_gap({**ok, "output": "1 passed"}, {**py, "tests": 0})


def test_checks_are_generated_for_the_project_language(tmp_path):
    from brain.completion import accept_requirement_tests, requirement_goal, requirement_language
    task = {"id": "0123456789abcdef", "goal": "add must add", "requirement_language": "node"}
    assert "node:test" in requirement_goal(task) and "test_requirements_01234567.test.mjs" in requirement_goal(task)
    assert "pytest" not in requirement_goal(task)
    # a real model put the file next to the existing tests and imported '../src/x.js' from there
    assert "folder of the existing tests" in requirement_goal(task) and "relative to where" in requirement_goal(task)
    python_task = {"id": "0123456789abcdef", "goal": "add must add"}
    assert "pytest" in requirement_goal(python_task) and "test_requirements_01234567.py" in requirement_goal(python_task)
    assert "folder of the existing tests" not in requirement_goal(python_task)  # Python checks stay in the root
    node = {"changes": [{"path": "test_requirements_01234567.test.mjs", "content": NODE_PASS}]}
    accepted = accept_requirement_tests(task, json.dumps(node))
    assert accepted == {"path": "test_requirements_01234567.test.mjs", "content": NODE_PASS, "tests": 1, "language": "node"}
    # A pytest file for a Node project, or a Node file without node:test or without tests: refused.
    assert accept_requirement_tests(task, json.dumps({"changes": [{"path": "test_requirements_01234567.py",
                                                                   "content": PY_PASS}]})) is None
    for content in ("test('x', () => {});\n", NODE_HEAD + "// test('x')\n"):
        bad = {"changes": [{"path": "test_requirements_01234567.test.mjs", "content": content}]}
        assert accept_requirement_tests(task, json.dumps(bad)) is None
    assert accept_requirement_tests({**task, "requirement_language": "ruby"}, json.dumps(node)) is None
    # Only test functions pytest collects are counted, so the evidence check is exact.
    nested = "def helper():\n    def test_inner():\n        pass\n\n\nclass Helper:\n    def test_m(self):\n        pass\n" \
             "\n\nclass TestA:\n    def test_m(self):\n        pass\n\n\ndef test_top():\n    pass\n"
    python = accept_requirement_tests(python_task, json.dumps({"changes": [{"path": "test_requirements_01234567.py",
                                                                     "content": nested}]}))
    assert python["tests"] == 2 and python["language"] == "python"
    (tmp_path / "package.json").write_text("{}")
    assert requirement_language(tmp_path) == "node"
    (tmp_path / "pyproject.toml").write_text("")
    assert requirement_language(tmp_path) == "python"  # mixed: the sandbox profile decides
    (tmp_path / "coding-brain.json").write_text(json.dumps({"test_profile": "node", "test_command": ["npm", "test"]}))
    assert requirement_language(tmp_path) == "node"
    (tmp_path / "coding-brain.json").write_text(json.dumps({"test_profile": "ruby"}))
    assert requirement_language(tmp_path) is None


def test_sandbox_runs_requirement_checks_with_their_own_runner_and_image(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", run)
    images = {"python": "py-image", "node": "node-image"}
    result = sandbox.run_tests(tmp_path, images, checks="test_requirements_0123abcd.test.mjs")
    assert result["profile"] == "node" and "node-image" in commands[-1]
    assert commands[-1][-4:] == ["node", "--test", "--test-reporter=tap", "test_requirements_0123abcd.test.mjs"]
    result = sandbox.run_tests(tmp_path, images, checks="test_requirements_0123abcd.py")
    assert result["profile"] == "python" and "py-image" in commands[-1]
    assert commands[-1][-2:] == ["no:cacheprovider", "test_requirements_0123abcd.py"]
    count = len(commands)
    for bad in ("../x.py", "test_requirements_0123abcd.js", "conftest.py", ["x"], "test_requirements_0123abcd.py; sh"):
        refused = sandbox.run_tests(tmp_path, images, checks=bad)
        assert refused["passed"] is False and refused["exit_code"] is None
    assert sandbox.run_tests(tmp_path, images, checks="test_requirements_0123abcd.py",
                             only=["test_a.py::test_b"])["passed"] is False
    assert sandbox.run_tests(tmp_path, {"python": "py-image"}, checks="test_requirements_0123abcd.test.mjs")["passed"] is False
    assert len(commands) == count  # nothing was run for any refused request


def test_backlog_acceptance_in_a_node_project_fails_closed(tmp_path, monkeypatch):
    """Backlog acceptance tests are pytest node ids; the Node profile refuses to run them, so the
    item can never verify (it does not pass by running `npm test`)."""
    from brain.conductor import acceptance_passed
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("nothing may run"))
    nodes = ["tests/test_accept.py::test_goal"]
    result = sandbox.run_tests(tmp_path, {"python": "py-image", "node": "node-image"}, None, nodes)
    assert result["passed"] is False and result["exit_code"] is None and result["profile"] == "node"
    assert "pytest node ids only" in result["output"]
    assert acceptance_passed(result, nodes) is False
    assert acceptance_passed({"passed": True, "exit_code": 0, "output": "# tests 1\n# pass 1\n"}, nodes) is False


def test_check_runs_that_show_no_execution_are_unverified_not_verified(tmp_path, monkeypatch):
    """Without node or Docker: a run that exits 0 without executing the checks (as `npm test` did)."""
    from tests.test_brain import make_repository
    repo = make_repository(tmp_path / "repos" / "demo", use_git=True)
    seen = []

    def run_tests(workspace, image, **kwargs):
        seen.append(kwargs.get("checks"))
        return {"passed": True, "exit_code": 0, "profile": "python", "output": "1 passed" if not kwargs.get("checks")
                else "no tests ran in 0.01s"}
    monkeypatch.setattr("brain.service.run_tests", run_tests)
    model = StandIn({".py": "def test_x():\n    import main\n    assert main.x == 2\n"}, {"main.py": "x = 2\n"})
    brain = Brain(repo.parent, tmp_path / "data", model, "img", requirement_checks=True)

    async def flow():
        task = await create_direct(brain, "Set x to 2")
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    assert seen[-1] and seen[-1].startswith("test_requirements_")
    assert task["status"] == "passed" and task["completion_verified"] is False
    assert task["completion"]["status"] == "unverified" and "pytest ran no tests" in task["completion"]["detail"]
    assert task["requirement_audit"][-1]["stage"] == "not_executed"


# What a real small model writes: checks next to the existing tests, wrapped in a Markdown fence --

NODE_IN_TEST_DIR = NODE_HEAD.replace("'./src/add.js'", "'../src/add.js'")


class NextToTests(StandIn):
    """Puts the checks in test/ (where the project's tests are) and wraps them in a code fence, as
    qwen2.5-coder:3b did in real runs."""
    async def propose(self, root, goal, memories, repository_context=None, **kwargs):
        raw = await super().propose(root, goal, memories, repository_context, **kwargs)
        if not goal.startswith("Do not implement anything"):
            return raw
        proposal = json.loads(raw)
        change = proposal["changes"][0]
        change.update(path="test/" + change["path"], content="```javascript\n" + change["content"] + "```")
        return json.dumps(proposal)


@needs_node
@pytest.mark.parametrize("checks, verified", [
    (NODE_IN_TEST_DIR + "test('add(-1, 1) is 0', () => assert.equal(add(-1, 1), 0));\n", True),
    (NODE_IN_TEST_DIR + "test('add(2, 2) is 5', () => assert.equal(add(2, 2), 5));\n", False),
    (NODE_IN_TEST_DIR + "test('add(-1, 1) is 0', { skip: true }, () => assert.equal(add(-1, 1), 0));\n", False),
    ("```javascript\n" + NODE_IN_TEST_DIR + "```", False),  # no tests of its own: a file-level entry only
], ids=["passes", "fails", "skipped", "no-tests"])
def test_node_checks_next_to_the_tests_run_at_that_path_and_need_execution_evidence(tmp_path, monkeypatch,
                                                                                    checks, verified):
    calls = host_docker(monkeypatch)
    task = execute(tmp_path, node_project(tmp_path), NextToTests({".mjs": checks}, {"src/add.js": NODE_IMPL}))
    path = task["requirement_tests"].get("path") or ""
    if verified:
        assert path.startswith("test/test_requirements_") and not task["requirement_tests"]["content"].startswith("```")
        assert calls[1] == ["node", "--test", "--test-reporter=tap", path]
    assert (task["completion_verified"] is True) is verified, task["completion"]
    assert ("completion_verified" in kinds(task)) is verified


def test_where_requirement_checks_may_be_written():
    from brain.completion import accept_requirement_tests
    node = {"id": "0123456789abcdef", "goal": "g", "requirement_language": "node"}
    python = {"id": "0123456789abcdef", "goal": "g"}
    body = NODE_PASS

    def offer(task, path, content):
        return accept_requirement_tests(task, json.dumps({"changes": [{"path": path, "content": content}]}))
    assert offer(node, "test/test_requirements_01234567.test.mjs", body)["path"] == "test/test_requirements_01234567.test.mjs"
    assert offer(node, "a/b/c/test_requirements_01234567.test.mjs", body)
    for refused in ("../test_requirements_01234567.test.mjs", "test/../test_requirements_01234567.test.mjs",
                    "/test_requirements_01234567.test.mjs", "a/b/c/d/test_requirements_01234567.test.mjs",
                    ".hidden/test_requirements_01234567.test.mjs", "test/test_requirements_99999999.test.mjs",
                    "test\\test_requirements_01234567.test.mjs"):
        assert offer(node, refused, body) is None, refused
    assert offer(node, "test/test_requirements_01234567.test.mjs", "```js\n" + body + "```")["content"] == body
    fenced = "```python\n" + PY_PASS + "```"
    assert offer(python, "test_requirements_01234567.py", fenced)["content"].startswith("from calc import add")
    assert offer(python, "tests/test_requirements_01234567.py", PY_PASS) is None  # Python checks: root only


def test_the_sandbox_runs_only_safe_checks_paths(tmp_path):
    for refused in ("../test_requirements_01234567.test.mjs", "test/../x/test_requirements_01234567.test.mjs",
                    "tests/test_requirements_01234567.py", "/abs/test_requirements_01234567.test.mjs"):
        result = sandbox.run_tests(tmp_path, IMAGES, checks=refused)
        assert result["passed"] is False and result["exit_code"] is None, refused


def test_checks_in_a_linked_folder_never_reach_outside_the_copy(tmp_path):
    """The checks run in a throwaway copy, where a linked folder becomes a real one: whatever the
    work links, the checks are written inside the copy and nowhere else."""
    from brain.completion import run_requirement_checks
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (workspace / "test").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    seen = []

    def run(copy, images, should_cancel=None, checks=None):
        seen.append((copy / checks).read_text() == NODE_PASS and not (copy / "test").is_symlink())
        return {"passed": True, "exit_code": 0}
    run_requirement_checks(workspace, {"path": "test/test_requirements_01234567.test.mjs", "content": NODE_PASS},
                           run, IMAGES, None)
    assert seen == [True] and not list((tmp_path / "elsewhere").iterdir())


@needs_docker
def test_node_checks_next_to_the_tests_verify_in_the_real_sandbox(tmp_path):
    checks = NODE_IN_TEST_DIR + "test('add(-1, 1) is 0', () => assert.equal(add(-1, 1), 0));\n"
    task = execute(tmp_path, node_project(tmp_path), NextToTests({".mjs": checks}, {"src/add.js": NODE_IMPL}), image=IMAGES)
    assert task["requirement_tests"]["path"].startswith("test/") and task["completion_verified"] is True
