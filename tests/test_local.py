"""Local installation: configuration, project recognition, the CLI, updates and rollback."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from brain.local import config as settings
from brain.local import updater
from brain.local.paths import Layout
from brain.local.project import detect, project_id


def git(path, *arguments):
    return subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True).stdout


def make_repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True)
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return path


def snapshot(path: Path) -> dict:
    return {item.relative_to(path).as_posix(): item.read_bytes() for item in path.rglob("*")
            if item.is_file() and ".git" not in item.parts}


# Configuration --------------------------------------------------------------------------------

def test_configuration_uses_existing_brains_format_without_credentials(tmp_path):
    from brain.brains import load_brains
    layout = Layout(tmp_path / "home").ensure()
    config = settings.load(layout)
    config["models"].update(model="qwen2.5-coder:7b", fast_model="qwen2.5-coder:1.5b", extra_brains={
        "groq": {"provider": "openai", "url": "https://api.groq.com/openai/v1", "model": "llama",
                 "api_key_env": "GROQ_API_KEY"}})
    config["supervisors"]["claude"]["enabled"] = True
    config["budgets"]["daily_limit"] = 5
    environment = settings.project_environment(layout, config, tmp_path / "work" / "app", tmp_path / "data")
    brains = load_brains(Path(environment["BRAIN_BRAINS_CONFIG"]))
    assert brains["roles"]["implementer"] == ["main", "groq"] and brains["roles"]["fast"] == ["fast", "main", "groq"]
    assert list(brains["supervisors"]) == ["claude"] and brains["supervision"]["daily_limit"] == 5
    written = Path(environment["BRAIN_BRAINS_CONFIG"]).read_text()
    assert "GROQ_API_KEY" in written and "sk-" not in written  # names a variable, never a key
    assert environment["BRAIN_REPOSITORIES"] == str(tmp_path / "work")
    assert environment["BRAIN_SUPERVISION_LEDGER"].startswith(str(layout.data))


def test_allowed_roots_restrict_projects(tmp_path):
    config = settings.load(Layout(tmp_path / "home"))
    assert settings.permitted(config, tmp_path / "anything")
    config["permissions"]["allowed_roots"] = [str(tmp_path / "projects")]
    assert settings.permitted(config, tmp_path / "projects" / "app")
    assert not settings.permitted(config, tmp_path / "elsewhere" / "app")


def test_shared_supervision_ledger_spans_projects(tmp_path, monkeypatch):
    from brain.brains import build_supervision
    monkeypatch.setenv("BRAIN_SUPERVISION_LEDGER", str(tmp_path / "shared.sqlite3"))
    policy = build_supervision({"supervisors": {"claude": {"provider": "claude_cli"}}, "supervision": {}},
                               tmp_path / "project-data")
    assert Path(policy.ledger.path) == tmp_path / "shared.sqlite3"


# Project recognition --------------------------------------------------------------------------

def test_detects_python_and_node_projects_read_only(tmp_path):
    python = make_repo(tmp_path / "svc", {
        "pyproject.toml": '[project]\nname = "svc"\ndependencies = ["fastapi", "pytest"]\n',
        "svc/api.py": "def handler():\n    return 1\n", "tests/test_api.py": "def test_x():\n    pass\n",
        "README.md": "# svc\n", "uv.lock": ""})
    before = snapshot(python)
    found = detect(python / "svc")  # from a subdirectory
    assert found["root"] == str(python.resolve()) and found["git"] and found["branch"]
    assert found["languages"] == ["Python"] and {"FastAPI", "pytest"} <= set(found["frameworks"])
    assert {"uv", "pip/pyproject"} <= set(found["dependency_managers"]) and "README.md" in found["docs"]
    assert found["commands"]["test"] == "python -m pytest"
    node = make_repo(tmp_path / "web", {
        "package.json": json.dumps({"scripts": {"build": "vite build", "test": "vitest"},
                                    "dependencies": {"react": "18"}, "devDependencies": {"vitest": "1"}}),
        "pnpm-lock.yaml": "", "src/App.tsx": "export const App = () => null\n"})
    web = detect(node)
    assert web["languages"] == ["TypeScript"] and {"React", "Vitest"} <= set(web["frameworks"])
    assert web["commands"] == {"build": "pnpm run build", "test": "pnpm test"} and web["dependency_managers"] == ["pnpm"]
    assert snapshot(python) == before and git(python, "status", "--porcelain") == ""


def test_project_identity_is_stable_and_isolated(tmp_path):
    first, second = tmp_path / "a" / "app", tmp_path / "b" / "app"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert project_id(first) == project_id(first) and project_id(first) != project_id(second)
    assert project_id(first).startswith("app-")


# CLI ------------------------------------------------------------------------------------------

def run_cli(home: Path, cwd: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "CODINGBRAIN_HOME": str(home),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    return subprocess.run([sys.executable, "-m", "brain.local", *arguments], cwd=cwd, env=environment,
                          capture_output=True, text=True, timeout=120)


def test_cli_recognizes_any_repository_without_changing_it(tmp_path):
    repo = make_repo(tmp_path / "projects" / "MyApplication", {"main.py": "print('hi')\n"})
    before = snapshot(repo)
    home = tmp_path / "home"
    version = run_cli(home, repo, "--version")
    assert version.returncode == 0 and version.stdout.startswith("codingbrain 0.9")
    initialized = run_cli(home, repo, "init")
    assert initialized.returncode == 0 and "Nothing in the project was changed" in initialized.stdout
    status = run_cli(home, repo, "status")
    assert "MyApplication" in status.stdout and "not configured" in status.stdout
    configured = run_cli(home, repo, "setup", "--non-interactive", "--model", "qwen3-4b", "--execution", "propose")
    assert configured.returncode == 0
    doctor = run_cli(home, repo, "doctor", "--offline", "--json")
    assert doctor.returncode == 0 and json.loads(doctor.stdout)["ok"]
    assert snapshot(repo) == before and git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--list") .strip() in {"* master", "* main"}
    record = json.loads(next((home / "data" / "projects").glob("MyApplication-*/project.json")).read_text())
    assert record["root"] == str(repo.resolve())
    # A second project gets its own memory.
    other = make_repo(tmp_path / "projects" / "Other", {"x.py": "x = 1\n"})
    run_cli(home, other, "init")
    assert len(list((home / "data" / "projects").iterdir())) == 2


def test_cli_refuses_projects_outside_allowed_roots(tmp_path):
    repo = make_repo(tmp_path / "elsewhere" / "app", {"main.py": "x = 1\n"})
    home = tmp_path / "home"
    run_cli(home, repo, "setup", "--non-interactive", "--model", "m", "--allowed-root", str(tmp_path / "projects"))
    result = run_cli(home, repo, "run", "do something")
    assert result.returncode != 0 and "outside the directories you allowed" in (result.stderr + result.stdout)


# Updates --------------------------------------------------------------------------------------

def make_release(folder: Path, version: str, *, corrupt=False, min_upgrade_from="0.9.0") -> Path:
    folder.mkdir(parents=True)
    wheel = folder / f"coding_brain-{version}-py3-none-any.whl"
    wheel.write_bytes(f"wheel {version}".encode())
    (folder / "constraints.txt").write_text("httpx==0.28.1\n")
    (folder / "release.json").write_text(json.dumps({"version": version, "min_upgrade_from": min_upgrade_from,
                                                     "requires_python": ">=3.11", "notes": f"notes {version}"}))
    sums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(folder.iterdir())]
    (folder / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    if corrupt:
        wheel.write_bytes(b"tampered")
    return folder


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A layout with 0.9.0 active, user state present, and the venv/migrate/doctor steps faked."""
    layout = Layout(tmp_path / "home").ensure()
    calls = []

    def fake_install(layout_, version, wheel, constraints, base_python):
        python = layout_.venv_python(version)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text(wheel.read_text())
        calls.append(("install", version))
        return python

    behaviour = {"migrate": 0, "doctor": True}

    def fake_run(python, layout_, *arguments, timeout=900):
        calls.append(arguments[2])
        if arguments[2] == "migrate":
            (layout_.config / "migrated-by.txt").write_text(str(python))
            return subprocess.CompletedProcess(arguments, behaviour["migrate"], '{"from": 1, "to": 1}', "boom")
        ok = behaviour["doctor"]
        return subprocess.CompletedProcess(arguments, 0 if ok else 1, json.dumps({"ok": ok}), "")
    monkeypatch.setattr(updater, "install_version", fake_install)
    monkeypatch.setattr(updater, "_run", fake_run)
    fake_install(layout, "0.9.0", make_release(tmp_path / "r090", "0.9.0") / "coding_brain-0.9.0-py3-none-any.whl",
                 None, None)
    updater.switch(layout, "0.9.0", sys.executable)
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "mine"}})
    project = layout.projects / "app-123"
    project.mkdir(parents=True)
    with sqlite3.connect(project / "brain.sqlite3") as db:
        db.execute("CREATE TABLE tasks (id TEXT, body TEXT)")
        db.execute("INSERT INTO tasks VALUES ('t1', ?)", (json.dumps({"id": "t1", "status": "accepted"}),))
    (project / "tasks" / "t1" / "workspace").mkdir(parents=True)
    (project / "tasks" / "t1" / "workspace" / "big.bin").write_bytes(b"x" * 1000)
    return layout, behaviour, calls


def memory_rows(layout):
    with sqlite3.connect(layout.projects / "app-123" / "brain.sqlite3") as db:
        return db.execute("SELECT id FROM tasks").fetchall()


def test_update_installs_side_by_side_and_preserves_state(installed, tmp_path):
    layout, _, calls = installed
    result = updater.install_release(layout, updater.DirectorySource(make_release(tmp_path / "r091", "0.9.1")),
                                     "0.9.0", log=lambda message: None)
    assert result["status"] == "installed" and result["installed"] == "0.9.1"
    current = updater.read_current(layout)
    assert current["version"] == "0.9.1" and current["previous"] == ["0.9.0"]
    assert layout.venv_python("0.9.0").exists()  # the old version stays for rollback
    launcher = (layout.bin / ("codingbrain.cmd" if sys.platform == "win32" else "codingbrain")).read_text()
    # On Windows the launcher refers to %LOCALAPPDATA% rather than spelling out the user folder.
    launcher = launcher.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", "%LOCALAPPDATA%"))
    assert str(layout.venv_python("0.9.1")) in launcher
    assert settings.load(layout)["models"]["model"] == "mine" and memory_rows(layout) == [("t1",)]
    backup = Path(result["backup"])
    manifest = json.loads((backup / "manifest.json").read_text())
    assert "data/projects/app-123/brain.sqlite3" in manifest["files"]
    assert not any("workspace" in name for name in manifest["files"])  # worktrees are not state
    assert calls[-3:] == [("install", "0.9.1"), "migrate", "doctor"]
    again = updater.install_release(layout, updater.DirectorySource(tmp_path / "r091"), "0.9.1")
    assert again["status"] == "up_to_date"


def test_update_rejects_tampered_downloads_without_changes(installed, tmp_path):
    layout, _, calls = installed
    with pytest.raises(updater.UpdateError, match="Checksum mismatch"):
        updater.install_release(layout, updater.DirectorySource(make_release(tmp_path / "bad", "0.9.1", corrupt=True)),
                                "0.9.0", log=lambda message: None)
    assert updater.read_current(layout)["version"] == "0.9.0" and not layout.venv_python("0.9.1").exists()
    assert not list(layout.backups.iterdir())


def test_failed_health_check_restores_state_and_keeps_old_version(installed, tmp_path):
    layout, behaviour, _ = installed
    behaviour["doctor"] = False
    with pytest.raises(updater.UpdateError, match="Health check failed"):
        updater.install_release(layout, updater.DirectorySource(make_release(tmp_path / "r091", "0.9.1")), "0.9.0",
                                log=lambda message: None)
    assert updater.read_current(layout)["version"] == "0.9.0"
    assert not (layout.versions / "0.9.1").exists()
    assert not (layout.config / "migrated-by.txt").exists()  # the migration's change was undone
    assert settings.load(layout)["models"]["model"] == "mine" and memory_rows(layout) == [("t1",)]


def test_failed_migration_restores_state(installed, tmp_path):
    layout, behaviour, _ = installed
    behaviour["migrate"] = 1
    with pytest.raises(updater.UpdateError, match="State migration failed"):
        updater.install_release(layout, updater.DirectorySource(make_release(tmp_path / "r091", "0.9.1")), "0.9.0",
                                log=lambda message: None)
    assert updater.read_current(layout)["version"] == "0.9.0" and not (layout.config / "migrated-by.txt").exists()


def test_no_update_during_an_active_session_or_running_task(installed, tmp_path):
    layout, _, _ = installed
    source = updater.DirectorySource(make_release(tmp_path / "r091", "0.9.1"))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (layout.locks / f"session-{child.pid}.json").write_text(json.dumps({"pid": child.pid, "project": "app-123"}))
        with pytest.raises(updater.UpdateError, match="session is running"):
            updater.install_release(layout, source, "0.9.0")
    finally:
        child.kill()
        child.wait()
    with sqlite3.connect(layout.projects / "app-123" / "brain.sqlite3") as db:
        db.execute("INSERT INTO tasks VALUES ('t2', ?)", (json.dumps({"id": "t2", "status": "running"}),))
    with pytest.raises(updater.UpdateError, match="mid-execution"):
        updater.install_release(layout, source, "0.9.0")
    assert updater.read_current(layout)["version"] == "0.9.0"


def test_upgrade_compatibility_is_checked(installed, tmp_path):
    layout, _, _ = installed
    source = updater.DirectorySource(make_release(tmp_path / "r100", "1.0.0", min_upgrade_from="0.9.5"))
    with pytest.raises(updater.UpdateError, match="cannot upgrade 0.9.0 directly"):
        updater.install_release(layout, source, "0.9.0", log=lambda message: None)


def test_rollback_switches_back_and_can_restore_state(installed, tmp_path):
    layout, _, _ = installed
    updater.install_release(layout, updater.DirectorySource(make_release(tmp_path / "r091", "0.9.1")), "0.9.0",
                            log=lambda message: None)
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "changed"}})
    result = updater.rollback(layout, restore_backup=True, log=lambda message: None)
    assert result["version"] == "0.9.0" and updater.read_current(layout)["version"] == "0.9.0"
    assert settings.load(layout)["models"]["model"] == "mine"  # state from before the update
    assert Path(result["current_state_saved_to"]).exists()  # and the newer state was kept aside
    assert updater.read_current(layout)["previous"] == ["0.9.1"]


def test_windows_launcher_is_one_self_terminating_line(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Zoë\AppData\Local")
    layout = Layout(tmp_path / "home")
    target = updater.write_launcher(layout, Path(r"C:\Users\Zoë\AppData\Local\CodingBrain\app\versions\0.9.1\venv\Scripts\python.exe"))
    text = target.read_bytes().decode()
    assert target.name == "codingbrain.cmd" and text.count("\r\n") == 1 and text.endswith("& exit /b\r\n")
    # No non-ASCII user folder in the batch file: cmd.exe reads it in the console code page.
    assert text.startswith('@"%LOCALAPPDATA%\\CodingBrain\\app') and "-m brain.local %*" in text and text.isascii()


def test_migrations_refuse_newer_state(tmp_path):
    from brain.local.migrations import migrate
    layout = Layout(tmp_path / "home").ensure()
    settings.save(layout, {**settings.load(layout), "schema_version": settings.SCHEMA_VERSION + 1})
    with pytest.raises(RuntimeError, match="newer than this version supports"):
        migrate(layout)
    settings.save(layout, {**settings.load(layout), "schema_version": settings.SCHEMA_VERSION})
    assert migrate(layout)["to"] == settings.SCHEMA_VERSION


# Sessions: run, pause, reopen, resume, accept -------------------------------------------------

class ScriptedModel:
    async def propose(self, root, goal, memories, repository_context=None, **kwargs):
        return json.dumps({"plan": "set x", "changes": [{"path": "main.py", "content": "x = 2\n"}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}


def test_goal_pause_reopen_resume_and_accept_as_a_new_branch(tmp_path, monkeypatch):
    import asyncio
    from brain.local import cli
    from brain.service import Brain
    monkeypatch.setattr("brain.service.run_tests", lambda workspace, image, **kwargs: {
        "passed": (workspace / "main.py").read_text() == "x = 2\n", "exit_code": 0, "output": "1 passed"})
    repo = make_repo(tmp_path / "projects" / "app", {"main.py": "x = 1\n", "test_main.py": "def test():\n    pass\n"})
    before = snapshot(repo)
    layout = Layout(tmp_path / "home").ensure()
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "m"}})

    def context():
        found = cli.Context(layout, repo)
        found.register()
        found._brain = Brain(repo.parent, found.data, ScriptedModel(), "img")
        return found
    first = context()
    cli.run_goal(first, "Set x to 2", auto=False)  # not a terminal: the plan is not run without approval
    assert not list(layout.locks.glob("session-*.json"))  # the session lock is released
    second = context()  # a new terminal: memory and tasks persist
    task = cli.find_task(second, None)
    assert task["status"] == "proposed" and task["goal"] == "Set x to 2"
    task = asyncio.run(cli.handle(second, task, auto=True))
    assert task["status"] == "passed"  # acceptance still requires a human
    accepted = asyncio.run(cli.accept(second, task))
    branch = accepted["branch"]
    assert branch.startswith("codingbrain/set-x-to-2-")
    assert git(repo, "show", f"{branch}:main.py") == "x = 2\n"
    assert git(repo, "branch", "--show-current").strip() in {"master", "main"}
    assert snapshot(repo) == before and git(repo, "status", "--porcelain") == ""


def test_interrupted_task_is_resumed_after_restart(tmp_path, monkeypatch):
    import asyncio
    from brain.local import cli
    from brain.service import Brain
    monkeypatch.setattr("brain.service.run_tests", lambda workspace, image, **kwargs: {
        "passed": True, "exit_code": 0, "output": "1 passed"})
    repo = make_repo(tmp_path / "projects" / "app", {"main.py": "x = 1\n", "test_main.py": "def test():\n    pass\n"})
    layout = Layout(tmp_path / "home").ensure()
    settings.save(layout, {**settings.load(layout), "models": {**settings.DEFAULTS["models"], "model": "m"}})
    found = cli.Context(layout, repo)
    found.register()
    brain = Brain(repo.parent, found.data, ScriptedModel(), "img")
    task = brain.submit("app", "Set x to 2", launch=False)
    task["status"] = "running"  # the terminal was closed mid-task
    brain.store.save(task)
    restarted = cli.Context(layout, repo)
    restarted._brain = Brain(repo.parent, restarted.data, ScriptedModel(), "img")
    current = cli.find_task(restarted, task["id"][:8])
    assert current["status"] == "blocked"

    async def resume():
        restarted.brain.retry(current["id"])
        await cli.drain(restarted.brain)
        return await cli.handle(restarted, current, True)
    assert asyncio.run(resume())["status"] == "passed"
