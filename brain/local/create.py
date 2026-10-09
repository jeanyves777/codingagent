"""`codingbrain new "<goal>"`: start a new application from a sentence.

  1. Readiness: a configured local model, Git, and the Docker sandbox the result is tested in.
     Anything missing is a precise blocker (`codingbrain install` prepares it); nothing is created.
  2. A new, empty folder: <projects root>/<name>. An existing path is never reused or overwritten;
     the path may not overlap Coding Brain's own data, must be inside the roots you allowed, and
     may not be inside another Git repository.
  3. Git: `git init`, an empty baseline commit authored by you (your Git identity; if you have
     none, you are asked for one and it is stored in this repository only, never globally), then
     a scaffold commit: README, .gitignore, coding-brain.json (the test profile) and the test
     setup for the chosen stack. Every scaffold file is shown before it is written.
  4. Project memory: your goal (approved, from you) and the project's constraints.
  5. Your goal goes through the normal governed lifecycle (plan, review, isolated worktree,
     tests in the offline sandbox, visual checks for web pages, your acceptance), with live
     activity. The result is a branch; your main branch keeps the scaffold until you merge.

Stacks are dependency-free on purpose: the sandbox has no network, so Python projects use the
standard library with pytest (included in the sandbox), Node projects the built-in node:test
runner, and web projects plain HTML/CSS/JS with node:test for their logic. A goal that needs
third-party packages ends with a precise blocker naming them; dependency acquisition needs your
explicit network approval and is never granted quietly (docs/new-project.md).
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config as settings
from .paths import Layout

STACKS = ("python", "node", "web")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
WEB = re.compile(r"(?i)\b(web ?app|website|web page|webpage|landing page|frontend|front-end|html|browser|"
                 r"single.page|spa|dashboard|portfolio|ui\b)")
NODE = re.compile(r"(?i)\b(node(\.js)?|javascript|typescript|npm|express)\b")


class Blocked(Exception):
    """Something the user has to decide or fix; nothing was created."""


def choose_stack(goal: str, requested: str | None = None) -> str:
    if requested:
        return requested
    if WEB.search(goal):
        return "web"
    if NODE.search(goal):
        return "node"
    return "python"


def slug_name(goal: str) -> str:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", goal.lower())
             if word not in {"a", "an", "the", "build", "create", "make", "write", "app", "application", "for", "with",
                             "that", "and", "to", "of", "me", "simple", "small", "new", "please"}]
    return "-".join(words[:4])[:40] or "new-app"


def check_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) or name.endswith((".", "-")):
        raise Blocked(f"'{name}' is not a usable folder name: use letters, digits, '.', '_' or '-' (at most 64, "
                      "starting with a letter or digit)")
    if name.split(".")[0].lower() in RESERVED:
        raise Blocked(f"'{name}' is a reserved device name on Windows; choose another name")
    return name


def projects_root(config: dict, override: str | None) -> Path:
    chosen = override or (config.get("create") or {}).get("projects_root") or str(Path.home() / "Projects")
    return Path(chosen).expanduser().absolute()


def _git(*arguments, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(["git", *arguments], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                            timeout=60)
    if check and result.returncode:
        raise Blocked(f"git {' '.join(arguments[:2])} failed: {(result.stderr or result.stdout).strip()[-400:]}")
    return result.stdout.strip() if result.returncode == 0 else ""


def git_identity(where: Path) -> dict:
    """Your effective Git identity (global or system configuration), if any."""
    probe = where if where.is_dir() else Path.home()
    return {"name": _git("config", "user.name", cwd=probe, check=False),
            "email": _git("config", "user.email", cwd=probe, check=False)}


def plan_target(layout: Layout, config: dict, name: str, root: Path) -> Path:
    """The folder to create, after every safety check. Raises Blocked."""
    from ..service import is_junction, overlaps
    check_name(name)
    target = root / name
    if target.exists() or target.is_symlink():
        raise Blocked(f"{target} already exists. `codingbrain new` only creates new folders; open an existing "
                      f"project with `cd {target}; codingbrain`, or choose another --name")
    if root.parent == root:
        raise Blocked("choose a projects folder, not a drive root")
    existing = root
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    for ancestor in [existing, *existing.parents]:
        if ancestor.is_symlink() or is_junction(ancestor):
            if ancestor == existing or ancestor.is_relative_to(Path.home()):
                raise Blocked(f"{ancestor} is a link (symlink or junction). Use the real folder it points to "
                              f"({ancestor.resolve()}) as --in, so the project's location is unambiguous")
    resolved = existing.resolve() / target.relative_to(existing)
    if overlaps(resolved, layout.home):
        raise Blocked(f"{resolved} overlaps Coding Brain's own data folder ({layout.home}); choose another --in")
    if not settings.permitted(config, resolved.parent if resolved.parent.exists() else existing.resolve()):
        raise Blocked(f"{resolved} is outside the directories you allowed "
                      f"({', '.join(config['permissions']['allowed_roots'])}). Change this with `codingbrain setup`.")
    inside = _git("rev-parse", "--show-toplevel", cwd=existing, check=False)
    if inside:
        raise Blocked(f"{existing} is inside the Git repository {inside}; a new project there would be nested in "
                      "it. Choose a folder outside any repository with --in")
    return resolved


SCAFFOLD_COMMON = {
    "README.md": "# {name}\n\n{goal}\n\nCreated with `codingbrain new`. Coding Brain builds the application on a "
                 "branch and runs its tests in an offline sandbox before you accept it.\n\n## Dependencies\n\n"
                 "{dependencies}\n",
}


def scaffold(stack: str, name: str, goal: str) -> dict[str, str]:
    """The files of the scaffold commit. Data only: nothing here is executed."""
    dependencies = {
        "python": "Python standard library only; tests use pytest (available in the sandbox).",
        "node": "No npm packages; tests use Node's built-in `node:test` runner (`npm test`).",
        "web": "Plain HTML, CSS and JavaScript (no build step, no npm packages); logic is tested with Node's "
               "built-in `node:test` runner (`npm test`), and pages are checked in a browser by Coding Brain.",
    }[stack]
    files = {path: text.format(name=name, goal=goal.strip(), dependencies=dependencies)
             for path, text in SCAFFOLD_COMMON.items()}
    if stack == "python":
        files[".gitignore"] = "__pycache__/\n*.pyc\n.venv/\n.pytest_cache/\n"
        # -vv: full assertion diffs, so a failed attempt tells the repair exactly what differed
        # (pytest's quiet default truncates them).
        files["coding-brain.json"] = json.dumps({"test_profile": "python", "test_command": [
            "python", "-m", "pytest", "-vv", "-p", "no:cacheprovider"]}, indent=2) + "\n"
        # Makes modules in the project root importable from tests/ without packaging.
        files["conftest.py"] = "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent))\n"
        files["tests/README.md"] = "Tests for this project (pytest). Coding Brain adds tests for each requirement.\n"
    else:
        package = {"name": name.lower(), "version": "0.1.0", "private": True, "type": "module",
                   "scripts": {"test": "node --test"}}
        files[".gitignore"] = "node_modules/\n"
        files["package.json"] = json.dumps(package, indent=2) + "\n"
        profile = {"test_profile": "node", "test_command": ["npm", "test"]}
        if stack == "web":
            profile["preview"] = {"static_root": ".", "path": "/index.html"}
        files["coding-brain.json"] = json.dumps(profile, indent=2) + "\n"
        files["test/README.md"] = "Tests for this project (`node --test`, built into Node). Coding Brain adds tests " \
                                  "for each requirement.\n"
    return files


def readiness_blockers(layout: Layout, config: dict, system=None) -> tuple[list[str], list[str]]:
    """(blockers, warnings): what must work before a new project can be built and tested."""
    from .components import Env, check_all
    from .installer import InstallState
    from .system import System
    env = Env(system or System(), layout, config, evidence=InstallState(layout).evidence)
    results = check_all(env, ["git", "ollama", "model", "docker", "sandbox"])
    blockers, warnings = [], []
    if not settings.configured(config):
        blockers.append("no local model is configured: run `codingbrain install` (or `codingbrain setup`)")
    for name, why in (("git", "Git"), ("ollama", "the local model server"), ("model", "the local model"),
                      ("docker", "Docker (tests run in its offline sandbox)"), ("sandbox", "the sandbox images")):
        status = results[name]
        if not status.ready:
            blockers.append(f"{why}: {status.detail or status.state} — `codingbrain install"
                            + (" --full" if name in {"docker", "sandbox"} else "") + "`")
        elif (status.data or {}).get("unverified"):
            warnings.append(f"{why}: not verified yet ({status.detail}); `codingbrain doctor --full` proves it")
    return blockers, warnings


MISSING_MODULE = [re.compile(r"ModuleNotFoundError: No module named '([^']+)'"),
                  re.compile(r"Cannot find (?:module|package) '([^']+)'"),
                  re.compile(r"ERR_MODULE_NOT_FOUND[^\n]*'([^']+)'")]


def missing_dependencies(task: dict) -> list[str]:
    """Third-party packages a failed task needed, from the sandbox output (not guessed)."""
    output = ((task.get("test_evidence") or {}).get("output") or "") + "\n" + "\n".join(
        str(item.get("summary", "")) for item in task.get("failure_log") or [])
    found = []
    for pattern in MISSING_MODULE:
        for match in pattern.findall(output):
            name = match.split(".")[0] if not match.startswith((".", "/")) else None
            if name and not name.startswith("node:") and name not in found:
                found.append(name)
    return found


def create_project(layout: Layout, goal: str, name: str | None = None, root: str | None = None,
                   stack: str | None = None, yes: bool = False, interactive: bool | None = None,
                   identity: dict | None = None, ask=None, out=None, system=None, check_ready: bool = True) -> dict:
    """Create the project and its baseline; returns where it is. Building the goal is separate
    (`build`), so a blocker before the build leaves nothing half-made."""
    from .cli import ask as default_ask, prompt
    out = out or sys.stdout
    ask = ask or default_ask
    interactive = sys.stdin.isatty() if interactive is None else interactive
    config = settings.load(layout.ensure())
    goal = " ".join(goal.split())
    if len(goal) < 8:
        raise Blocked("describe the application you want in a sentence, e.g. "
                      "codingbrain new \"Build a task manager with due dates and priorities\"")
    if check_ready:
        blockers, warnings = readiness_blockers(layout, config, system)
        if blockers:
            raise Blocked("this computer is not ready to build and test a new application:\n  "
                          + "\n  ".join(blockers))
        for warning in warnings:
            print(f"Note: {warning}", file=out)
    stack = choose_stack(goal, stack)
    name = name or slug_name(goal)
    folder_root = projects_root(config, root)
    target = plan_target(layout, config, name, folder_root)
    files = scaffold(stack, name, goal)
    who = identity or git_identity(target.parent if target.parent.exists() else Path.home())
    print(f"New project: {target}\nStack: {stack} (dependency-free; see README)\n"
          f"Goal: {goal}\nGit: empty baseline commit, then a scaffold commit with:", file=out)
    for path in files:
        print(f"  {path}", file=out)
    if not (who.get("name") and who.get("email")):
        if not interactive:
            raise Blocked("Git has no author identity on this computer. Set yours once with\n"
                          "  git config --global user.name \"Your Name\"\n"
                          "  git config --global user.email \"you@example.com\"\n"
                          "or pass --git-name and --git-email (stored in the new repository only)")
        print("Git has no author identity on this computer. The new repository's commits need one; it is "
              "stored in this repository only (not globally).", file=out)
        who = {"name": prompt("Your name for Git commits", who.get("name") or ""),
               "email": prompt("Your email for Git commits", who.get("email") or "")}
        if not (who["name"] and who["email"]):
            raise Blocked("a Git author name and email are needed for the new repository's commits")
        who["local"] = True
    if not yes and not ask(f"Create {target} and start building?", True):
        raise Blocked("cancelled; nothing was created")
    created_root = not folder_root.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # fails if something appeared meanwhile: never reuse a folder
    try:
        _git("init", "-q", cwd=target)
        _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=target)
        if who.get("local") or identity:
            _git("config", "user.name", who["name"], cwd=target)
            _git("config", "user.email", who["email"], cwd=target)
        _git("commit", "-q", "--allow-empty", "-m", f"Start {name} (codingbrain new)", cwd=target)
        for relative, text in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        _git("add", "-A", cwd=target)
        _git("commit", "-q", "-m", f"Scaffold for {stack} (codingbrain new)\n\nGoal: {goal}", cwd=target)
    except Exception:
        import shutil
        shutil.rmtree(target, ignore_errors=True)  # only the folder this command just created
        if created_root:
            try:
                folder_root.rmdir()
            except OSError:
                pass
        raise
    from .cli import Context
    context = Context(layout, target)
    context.register()
    memory = context.memory
    memory.store.add("goal", goal, "user", 2, "approved", "codingbrain new")
    memory.remember("decision", f"Created with `codingbrain new` as a {stack} project. The test sandbox is offline: "
                    + {"python": "use the Python standard library; tests use pytest.",
                       "node": "use no npm packages; tests use node:test (`npm test`).",
                       "web": "plain HTML/CSS/JS with no build step or npm packages; put pages in index.html and "
                              "logic in ES modules tested with node:test (`npm test`)."}[stack],
                    verified=True, ref="codingbrain new")
    record = {"path": str(target), "name": name, "stack": stack, "goal": goal, "created_at": time.time(),
              "head": _git("rev-parse", "HEAD", cwd=target)}
    (context.data / "created.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def build(layout: Layout, record: dict, orchestrate: bool = False, auto: bool | None = None, view=None) -> dict:
    """Run the goal through the normal lifecycle in the new project; returns the final task."""
    from .cli import Context, run_goal
    context = Context(layout, Path(record["path"]))
    context.check()
    visual = None
    if record["stack"] == "web" and context.config["visual"].get("enabled", True):
        visual = {"enabled": True, "references": [], "max_repairs": context.config["visual"]["max_repairs"],
                  "viewports": context.config["visual"]["viewports"]}
    task = run_goal(context, record["goal"], orchestrate, auto, visual=visual, view=view)
    return task or {}


def report(record: dict, task: dict) -> str:
    """What happened and the next step, in plain words."""
    status = task.get("status", "unknown")
    lines = [f"\nProject {record['name']} at {record['path']}"]
    if status == "accepted":
        lines.append(f"Built, tested and accepted on branch {task.get('branch') or '(see git branch)'}.")
        lines.append(f"  cd {record['path']}\n  git switch {task.get('branch', '<branch>')}   # the application"
                     + ("\n  npm test" if record["stack"] != "python" else "\n  python -m pytest"))
    elif status == "passed":
        lines.append("Built and tested in the sandbox; waiting for your acceptance: "
                     f"`cd {record['path']}; codingbrain accept {task['id'][:8]}`")
    elif status in {"proposed"}:
        lines.append(f"A plan is ready: `cd {record['path']}; codingbrain resume`")
    else:
        missing = missing_dependencies(task)
        if missing:
            lines.append("Blocked: the solution needs third-party packages the offline sandbox does not have: "
                         + ", ".join(missing) + ".")
            lines.append("  Either continue without them (`codingbrain resume` and ask for a standard-library / "
                         "built-in solution), or acquire them with your explicit network approval "
                         "(see docs/new-project.md, \"Dependencies\").")
        elif task:
            lines.append(f"Task {status}. Details: `cd {record['path']}; codingbrain trace`; retry: `codingbrain resume`.")
        else:
            lines.append(f"The project is ready; start building with `cd {record['path']}; codingbrain run \"...\"`.")
    return "\n".join(lines)
