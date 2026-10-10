"""`codingbrain new`: goal-first project creation.

Creation, safety and Git handling run for real (real folders, real Git). Readiness uses the
simulated machine from tests/installer_fakes.py (labelled). The end-to-end build with a real
model and the real sandbox is test_real_*, skipped unless both are available.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.local import config as settings
from brain.local.create import (Blocked, check_name, choose_stack, create_project, missing_dependencies,
                                report, scaffold, slug_name)
from brain.local.paths import Layout

from installer_fakes import FakeMachine


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A home folder like C:\\Users\\Name: Coding Brain's data under AppData, projects under Projects."""
    home = tmp_path / "Users" / "Kkoff"
    data = home / "AppData" / "Local" / "CodingBrain"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODINGBRAIN_HOME", str(data))
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test Person\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    layout = Layout(data).ensure()
    config = settings.load(layout)
    config["models"]["model"] = "qwen2.5-coder:7b"
    settings.save(layout, config)
    return {"home": home, "layout": layout, "gitconfig": gitconfig, "tmp": tmp_path}


def make(env, goal="Build a command-line task manager with due dates", **options):
    options.setdefault("yes", True)
    options.setdefault("check_ready", False)
    options.setdefault("interactive", False)
    return create_project(env["layout"], goal, **options)


def git(path, *arguments):
    return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True).stdout.strip()


def test_creates_folder_baseline_and_scaffold_in_the_projects_folder(env):
    record = make(env, name="tasks")
    path = Path(record["path"])
    assert path == (env["home"] / "Projects" / "tasks").resolve()
    log = git(path, "log", "--format=%s|%an <%ae>").splitlines()
    assert log == ["Scaffold for python (codingbrain new)|Test Person <test@example.com>",
                   "Start tasks (codingbrain new)|Test Person <test@example.com>"]
    assert git(path, "show", "--stat", "--format=", "HEAD~1") == ""  # the baseline is empty
    assert git(path, "status", "--porcelain") == ""
    assert git(path, "branch", "--show-current") == "main"
    assert json.loads((path / "coding-brain.json").read_text()) == {
        "test_profile": "python",
        "test_command": ["python", "-m", "pytest", "-vv", "-p", "no:cacheprovider", "-o", "python_files=*.py"]}
    assert (path / "conftest.py").is_file() and "task manager" in (path / "README.md").read_text()
    # registered, with the goal in project memory as the user's approved instruction
    from brain.local.cli import Context
    context = Context(env["layout"], path)
    assert "task manager" in json.dumps(context.memory.profile())
    assert (context.data / "created.json").is_file()


def test_never_reuses_an_existing_folder(env):
    existing = env["home"] / "Projects" / "tasks"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("mine")
    with pytest.raises(Blocked, match="already exists"):
        make(env, name="tasks")
    assert sorted(path.name for path in existing.iterdir()) == ["keep.txt"]


def test_refuses_overlap_with_coding_brain_data(env):
    with pytest.raises(Blocked, match="overlaps Coding Brain"):
        make(env, name="inside", root=str(env["layout"].home / "data"))
    assert not (env["layout"].home / "data" / "inside").exists()


def test_refuses_to_nest_inside_another_repository(env):
    outer = env["home"] / "Projects" / "outer"
    outer.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    with pytest.raises(Blocked, match="inside the Git repository"):
        make(env, name="nested", root=str(outer))
    assert not (outer / "nested").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows; junctions tested below")
def test_refuses_a_linked_projects_folder(env):
    real = env["tmp"] / "elsewhere"
    real.mkdir()
    link = env["home"] / "Projects"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(Blocked, match="is a link"):
        make(env, name="tasks")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_refuses_a_junction_projects_folder(env):
    real = env["tmp"] / "elsewhere"
    real.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(env["home"] / "Projects"), str(real)], check=True,
                   capture_output=True)
    with pytest.raises(Blocked, match="is a link"):
        make(env, name="tasks")


def test_allowed_roots_are_enforced(env):
    config = settings.load(env["layout"])
    config["permissions"]["allowed_roots"] = [str(env["home"] / "Work")]
    settings.save(env["layout"], config)
    with pytest.raises(Blocked, match="outside the directories you allowed"):
        make(env, name="tasks")
    (env["home"] / "Work").mkdir()
    assert make(env, name="tasks", root=str(env["home"] / "Work"))["path"]


@pytest.mark.parametrize("name", ["con", "NUL.txt", "a/b", "..", "-x", "x" * 65, "name.", "has space"])
def test_unusable_names(name):
    with pytest.raises(Blocked):
        check_name(name)


def test_no_git_identity_is_a_blocker_not_a_bypass(env):
    env["gitconfig"].write_text("")
    with pytest.raises(Blocked, match="no author identity"):
        make(env, name="tasks")
    assert not (env["home"] / "Projects" / "tasks").exists()
    record = make(env, name="tasks", identity={"name": "Local Only", "email": "local@example.com"})
    path = Path(record["path"])
    assert git(path, "log", "-1", "--format=%an <%ae>") == "Local Only <local@example.com>"
    assert git(path, "config", "--local", "user.email") == "local@example.com"
    assert env["gitconfig"].read_text() == ""  # never written globally


def test_interactive_identity_is_stored_in_the_repository_only(env, monkeypatch):
    env["gitconfig"].write_text("")
    answers = iter(["Ada", "ada@example.com"])
    monkeypatch.setattr("brain.local.cli.prompt", lambda question, default="": next(answers))
    record = make(env, name="tasks", interactive=True)
    assert git(record["path"], "config", "--local", "user.name") == "Ada"
    assert env["gitconfig"].read_text() == ""


def test_not_ready_computer_creates_nothing(env):
    machine = FakeMachine(platform="linux", programs={"git", "ollama"}, ollama_running=True,
                          models={"qwen2.5-coder:7b"})  # SIMULATED: no Docker
    with pytest.raises(Blocked) as error:
        make(env, name="tasks", check_ready=True, system=machine)
    assert "Docker" in str(error.value) and "codingbrain install --full" in str(error.value)
    assert not (env["home"] / "Projects").exists()


def test_failure_midway_removes_only_what_it_created(env, monkeypatch):
    import brain.local.create as create
    real = create._git

    def failing(*arguments, cwd=None, check=True):
        if arguments[:1] == ("commit",) and "--allow-empty" not in arguments:
            raise Blocked("git commit failed: simulated")
        return real(*arguments, cwd=cwd, check=check)
    monkeypatch.setattr(create, "_git", failing)
    with pytest.raises(Blocked, match="simulated"):
        make(env, name="tasks")
    assert not (env["home"] / "Projects" / "tasks").exists()
    assert not (env["home"] / "Projects").exists()  # it created the Projects folder too, so removed it


def test_cancelled_creates_nothing(env):
    with pytest.raises(Blocked, match="cancelled"):
        make(env, name="tasks", yes=False, ask=lambda question, default=False: False)
    assert not (env["home"] / "Projects" / "tasks").exists()


def test_stack_choice_names_and_scaffolds():
    assert choose_stack("Build a task management web app with a dashboard") == "web"
    assert choose_stack("A Node.js script that renames photos") == "node"
    assert choose_stack("A command-line tool that tracks expenses") == "python"
    assert choose_stack("anything", "node") == "node"
    assert slug_name("Build a task management application") == "task-management"
    web = scaffold("web", "board", "A kanban board web page")
    assert json.loads(web["coding-brain.json"])["preview"] == {"static_root": ".", "path": "/index.html"}
    assert json.loads(web["package.json"])["scripts"]["test"] == "node --test"
    assert "dependencies" not in json.loads(web["package.json"])
    from brain.sandbox import profile
    import tempfile
    for stack in ("python", "node", "web"):
        with tempfile.TemporaryDirectory() as folder:
            for path, text in scaffold(stack, "x", "a goal").items():
                (Path(folder) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(folder) / path).write_text(text)
            assert profile(Path(folder))["command"][0] in {"python", "npm"}  # the sandbox accepts the scaffold


def test_missing_dependencies_come_from_sandbox_output():
    task = {"status": "failed", "test_evidence": {"output": "E   ModuleNotFoundError: No module named 'flask'\n"},
            "failure_log": [{"summary": "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'express' imported"}]}
    assert missing_dependencies(task) == ["flask", "express"]
    assert missing_dependencies({"test_evidence": {"output": "Cannot find module './util'"}}) == []
    text = report({"name": "x", "path": "/p", "stack": "python"}, task)
    assert "flask, express" in text and "network approval" in text


def test_cli_reports_blockers_plainly(env, monkeypatch, capsys):
    from brain.local import cli
    machine = FakeMachine(platform="linux", programs={"git"})  # SIMULATED: nothing else installed
    monkeypatch.setattr("brain.local.system.System", lambda: machine)
    code = cli.main(["new", "Build a command-line task manager", "--yes"])
    out = capsys.readouterr().out
    assert code == 1 and out.startswith("Not created:") and "codingbrain install" in out


@pytest.mark.skipif(not (os.environ.get("CODINGBRAIN_TEST_CODING_MODEL") and os.environ.get("CODINGBRAIN_TEST_DOCKER")),
                    reason="needs a real Ollama model and Docker with the sandbox images")
def test_real_goal_first_pipeline(env):
    """Real model, real sandbox. Two separate verdicts:
    - this test (a merge gate) asserts what Coding Brain itself must do whatever the model writes:
      safe project creation, real sandbox execution, test discovery, the independent requirement
      checks, an untouched main branch and an honest report;
    - whether the model actually produced a working application is the capability verdict. It is
      written to $CODINGBRAIN_CAPABILITY_REPORT and reported separately by CI. It is never hidden,
      and it is required before goal-first building is called production-ready."""
    import time
    from brain.local.cli import Context
    from brain.local.create import build
    config = settings.load(env["layout"])
    config["models"]["model"] = os.environ["CODINGBRAIN_TEST_CODING_MODEL"]
    settings.save(env["layout"], config)
    started = time.time()
    record = make(env, "Build a Python module tasks.py that keeps tasks in memory: add_task(title) returns the "
                       "new task's integer id; complete_task(task_id) marks that task done; list_tasks() returns "
                       "the tasks that are not done, and list_tasks(include_done=True) returns all tasks. "
                       "Each task is a dict with id, title and done. Include pytest tests",
                  name="tasks", check_ready=True)
    task = build(env["layout"], record, auto=True, view="plain")
    text = report(record, task)
    print(text)
    root = Path(record["path"])
    evidence = task.get("test_evidence") or {}
    output = evidence.get("output", "")
    kinds = [event["kind"] for event in task.get("events", [])]
    workspace = Context(env["layout"], root).brain.workspace(task["id"])
    written_tests = any("def test_" in path.read_text(errors="replace") for path in workspace.rglob("*.py")
                        if ".git" not in path.parts)
    capability = {"model": config["models"]["model"], "status": task.get("status"),
                  "passed": task.get("status") == "passed" and evidence.get("passed") is True,
                  "seconds": round(time.time() - started), "attempts": kinds.count("test_finished"),
                  "failures": [{key: item.get(key) for key in ("category", "diagnosis", "summary")}
                               for item in task.get("failure_log", [])][-6:],
                  "supervisor_used": any(kind.startswith("supervisor_") for kind in kinds)}
    if os.environ.get("CODINGBRAIN_CAPABILITY_REPORT"):
        Path(os.environ["CODINGBRAIN_CAPABILITY_REPORT"]).write_text(json.dumps(capability, indent=2))
    print(json.dumps(capability, indent=2))
    # The merge gate: Coding Brain's own guarantees.
    assert root.is_relative_to((env["home"] / "Projects").resolve())
    assert git(root, "log", "--format=%s").splitlines()[-1] == "Start tasks (codingbrain new)"
    assert len(git(root, "log", "--format=%H").splitlines()) == 2 and git(root, "status", "--porcelain") == ""
    assert task.get("status") in {"passed", "failed"}, task.get("status")  # finished, not stuck or broken
    assert evidence.get("exit_code") not in (None, 125, 126, 127), output[-500:]  # the sandbox really ran
    assert "test session starts" in output
    if written_tests:
        assert "collected 0 items" not in output  # tests the model wrote were discovered
    assert {"requirement_checks_written", "requirement_checks_skipped", "requirement_checks_rejected"} & set(kinds)
    if evidence.get("passed"):
        assert task.get("completion"), "visible tests passed but the requirement checks did not run"
    if not capability["passed"]:
        assert "Built" not in text and ("failed" in text or "Blocked" in text)  # an honest report
