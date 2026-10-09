"""`codingbrain`: use Coding Brain in any project on this computer.

    cd C:\\Projects\\MyApplication
    codingbrain                 # recognize the project, then ask for engineering goals
    codingbrain run "goal"      # one goal (add --orchestrate for multi-agent delegation)
    codingbrain init | status | tasks | resume [id] | accept <id> | doctor | setup
    codingbrain update [--check] [--channel dev] | rollback | --version

Work happens in isolated Git worktrees outside the project. Nothing in the project changes until
you accept a tested result, and even then only a new branch (codingbrain/...) is created; your
checked-out branch and working files are never modified.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as settings
from .paths import Layout
from .project import detect

RESUMABLE = {"proposed", "passed", "awaiting_implementer", "awaiting_tool_approval", "blocked", "failed",
             "queued", "integration_conflict"}


def version() -> str:
    """The running code's version: pyproject.toml when run from a source tree, else the installed
    distribution's metadata."""
    source = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if source.exists():
        found = re.search(r'^version = "([^"]+)"', source.read_text(encoding="utf-8"), re.M)
        if found:
            return found.group(1)
    try:
        from importlib.metadata import version as installed
        return installed("coding-brain")
    except Exception:
        return "unknown"


def ask(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def prompt(question: str, default: str = "") -> str:
    if not sys.stdin.isatty():
        return default
    answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


# Project context ------------------------------------------------------------------------------

class Context:
    def __init__(self, layout: Layout, start: Path):
        self.layout = layout.ensure()
        self.config = settings.load(layout)
        self.project = detect(start)
        self.root = Path(self.project["root"])
        self.data = layout.projects / self.project["id"]
        self._brain = None

    def register(self):
        self.data.mkdir(parents=True, exist_ok=True)
        record_file = self.data / "project.json"
        record = json.loads(record_file.read_text(encoding="utf-8")) if record_file.exists() else \
            {"first_seen": time.time()}
        record.update({"root": str(self.root), "name": self.root.name, "last_opened": time.time()})
        record_file.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def check(self):
        if self.root.parent == self.root:
            raise SystemExit("Coding Brain needs a project folder, not a drive root.")
        if not settings.permitted(self.config, self.root):
            raise SystemExit(f"{self.root} is outside the directories you allowed "
                             f"({', '.join(self.config['permissions']['allowed_roots'])}). "
                             "Change this with `codingbrain setup`.")
        if not settings.configured(self.config):
            raise SystemExit("Coding Brain is not configured yet. Run `codingbrain setup` first.")

    @property
    def brain(self):
        if self._brain is None:
            self.check()
            self.register()
            os.environ.update(settings.project_environment(self.layout, self.config, self.root, self.data))
            from ..factory import build_brain_from_env
            self._brain = build_brain_from_env()
            self._brain.project_knowledge = self.memory.context_for
        return self._brain

    @property
    def memory(self):
        if getattr(self, "_memory", None) is None:
            from .memory import ProjectMemory
            self.data.mkdir(parents=True, exist_ok=True)
            self._memory = ProjectMemory(self.layout, self.project)
        return self._memory

    def tasks(self) -> list[dict]:
        if not (self.data / "brain.sqlite3").exists():
            return []
        return sorted((task for task in self.brain.store.tasks() if task.get("repository") == self.root.name),
                      key=lambda task: task["events"][0]["time"] if task.get("events") else "", reverse=True)


def describe(project: dict) -> str:
    lines = [f"Project    {project['name']}  ({project['root']})"]
    if project["git"]:
        lines.append(f"Git        branch {project['branch']} at {project['head']}, "
                     f"{project['changes']} uncommitted change(s)")
    else:
        lines.append("Git        not a Git repository (work happens on a snapshot copy)")
    for label, key in (("Languages", "languages"), ("Frameworks", "frameworks"),
                       ("Packages", "dependency_managers"), ("Docs", "docs")):
        if project[key]:
            lines.append(f"{label:<11}{', '.join(project[key])}")
    if project["commands"]:
        lines.append("Commands   " + "; ".join(f"{name}: {command}" for name, command in project["commands"].items()))
    return "\n".join(lines)


def task_line(task: dict) -> str:
    goal = task.get("goal", "").replace("\n", " ")
    return f"{task['id'][:8]}  {task.get('kind', 'task'):<13} {task['status']:<22} {goal[:70]}"


# Running goals --------------------------------------------------------------------------------

async def drain(brain):
    while brain.jobs:
        await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
        await asyncio.sleep(0)


def branch_for(context: Context, commit: str, label: str) -> str:
    """Create a new branch at an accepted commit, without checking it out."""
    base = f"codingbrain/{label}"
    name, suffix = base, 1
    while subprocess.run(["git", "-C", str(context.root), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                         capture_output=True).returncode == 0:
        suffix += 1
        name = f"{base}-{suffix}"
    subprocess.run(["git", "-C", str(context.root), "branch", name, commit], check=True, capture_output=True)
    return name


def slug(goal: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:32].strip("-") or "change"


async def handle(context: Context, task: dict, auto: bool) -> dict:
    """Carry a task forward, asking before each protected step."""
    brain = context.brain
    while True:
        task = brain.store.get(task["id"])
        status = task["status"]
        if status == "proposed":
            proposal = task["proposal"]
            print(f"\nPlan ({task.get('proposal_author', 'implementer')}): {proposal['plan'][:1500]}")
            print("Files: " + ", ".join(change["path"] for change in proposal["changes"]))
            if not proposal["changes"]:
                print("Analysis only; nothing to execute.")
                return task
            if sys.stdin.isatty() and ask("Show the full diff?"):
                print(task.get("diff", "")[:20000])
            if not (auto or ask("Run it: review, apply in an isolated worktree and test in the sandbox?")):
                print(f"Left for later: `codingbrain resume {task['id'][:8]}`")
                return task
            await brain.execute(task["id"], task["digest"])
        elif status == "awaiting_tool_approval":
            request = task.get("pending_approval_id")
            print(f"The model asks to use a protected tool (request {request}).")
            approved = ask("Approve this tool request?")
            brain.decide_tool_approval(request, approved)
            await drain(brain)
            if not approved:
                return brain.store.get(task["id"])
        elif status == "queued" and task.get("pending_goal"):
            await brain.resume(task["id"])
        elif status == "passed":
            evidence = task.get("test_evidence", {})
            completion = (task.get("completion") or {}).get("status", "unchecked")
            print(f"\nTests passed in the sandbox (exit {evidence.get('exit_code')}); "
                  f"requirement checks: {completion}.")
            if task.get("review_disputed"):
                print("Note: the reviewer objected; its reason is in `codingbrain status`.")
            if not ask("Accept this result and create a branch with it?"):
                print(f"Not accepted. Accept later with `codingbrain accept {task['id'][:8]}`.")
                return task
            return await accept(context, task)
        elif status == "awaiting_implementer":
            print("No free model is reachable. Start Ollama (or your model server), then `codingbrain resume`.")
            return task
        else:
            if status in {"failed", "blocked", "integration_conflict"}:
                last = (task.get("failure_log") or [{}])[-1]
                context.memory.remember("repair", f"Attempt at '{task['goal'][:200]}' ended {status}: "
                                        f"{last.get('category', '')} {str(last.get('summary', ''))[:300]}",
                                        verified=False, ref=f"task:{task['id']}")
                print(f"\nTask {status}. {last.get('category', '')} {str(last.get('summary', ''))[:800]}")
                print(f"Retry with `codingbrain resume {task['id'][:8]}`.")
            return task


async def accept(context: Context, task: dict) -> dict:
    task = await context.brain.accept(task["id"], f"Accepted locally: {task['goal'][:200]}")
    if task.get("commit") and task["status"] == "accepted" and not task.get("parent_id"):
        name = branch_for(context, task["commit"], f"{slug(task['goal'])}-{task['id'][:6]}")
        task["branch"] = name
        context.brain.store.save(task)
        print(f"Accepted. New branch {name} (your current branch is unchanged). Review it with "
              f"`git log {name}` and merge when ready.")
    if task["status"] == "accepted":
        # Tested in the sandbox and accepted by the user: a verified fact about the project.
        plan = (task.get("proposal") or {}).get("plan", "")[:300]
        context.memory.remember("change", f"Accepted: {task['goal'][:240]}. Plan: {plan}"
                                + (f" Branch {task['branch']}." if task.get("branch") else ""),
                                verified=True, ref=f"task:{task['id']}")
    return task


async def orchestrate(context: Context, goal: str, auto: bool) -> dict:
    brain = context.brain
    group = brain.delegate(context.root.name, goal)
    for _ in range(200):
        await drain(brain)
        group = brain.store.get(group["id"])
        if group["status"] in {"completed", "attention_required", "integration_conflict", "cancelled", "blocked"}:
            break
        progressed = False
        for child in group.get("children", []):
            task = brain.store.get(child["id"])
            if task["status"] in {"proposed", "passed", "awaiting_tool_approval"}:
                print(f"\nAssignment {child['name']}: {task['goal'][:200]}")
                before = task["status"]
                task = await handle(context, task, auto)
                progressed |= task["status"] != before
        if not progressed and not brain.jobs:
            break
    group = brain.store.get(group["id"])
    print(f"\nOrchestration {group['status']}.")
    if group["status"] == "completed" and group.get("integration_head"):
        name = branch_for(context, group["integration_head"], f"{slug(goal)}-{group['id'][:6]}")
        print(f"Integrated result on new branch {name}; your current branch is unchanged.")
    return group


def run_goal(context: Context, goal: str, orchestrate_goal: bool = False, auto: bool | None = None):
    from .session import session
    auto = context.config["autonomy"]["execution"] == "auto" if auto is None else auto
    if context.project["tracked_changes"]:
        print("Note: Coding Brain starts from your last commit; uncommitted tracked changes are not included "
              "and must be committed or stashed first.")
    with session(context.layout, context.project["id"], goal[:80]):
        try:
            if orchestrate_goal:
                return asyncio.run(orchestrate(context, goal, auto))

            async def single():
                task = context.brain.submit(context.root.name, goal, launch=False)
                task = await context.brain.create(task)
                return await handle(context, task, auto)
            return asyncio.run(single())
        except KeyboardInterrupt:
            print("\nPaused. Your work is saved; continue with `codingbrain resume`.")


# Commands -------------------------------------------------------------------------------------

def cmd_init(args, layout):
    context = Context(layout, Path.cwd())
    context.register()
    print(describe(context.project))
    from ..intelligence import build_index
    index = build_index(context.root)  # read-only scan
    files = {item["path"] for item in index.get("symbols", [])}
    print(f"Indexed    {len(index.get('symbols', []))} symbols in {len(files)} source files, "
          f"{len(index.get('dependencies', []))} imports")
    print(f"Memory     {context.data}")
    print("Nothing in the project was changed. Run `codingbrain` to give it a goal.")


def cmd_status(args, layout):
    context = Context(layout, Path.cwd())
    print(describe(context.project))
    models = context.config["models"]
    supervisors = [name for name, spec in context.config["supervisors"].items() if spec.get("enabled")]
    print(f"Model      {models['provider']} {models['model'] or '(not configured)'}"
          + (f", fast {models['fast_model']}" if models.get("fast_model") else ""))
    print(f"Premium    {', '.join(supervisors) or 'off'}; budgets "
          + ", ".join(f"{key} {value}" for key, value in context.config["budgets"].items()))
    print(f"Version    {version()}")
    if not settings.configured(context.config):
        return
    tasks = context.tasks()
    print(f"\nTasks ({len(tasks)})" if tasks else "\nNo Coding Brain tasks in this project yet.")
    for task in tasks[:15]:
        print("  " + task_line(task))


def cmd_tasks(args, layout):
    context = Context(layout, Path.cwd())
    for task in context.tasks():
        print(task_line(task))


def find_task(context: Context, prefix: str | None) -> dict | None:
    tasks = context.tasks()
    if prefix:
        found = [task for task in tasks if task["id"].startswith(prefix)]
        if len(found) != 1:
            raise SystemExit(f"No single task matches {prefix!r}")
        return found[0]
    return next((task for task in tasks if task["status"] in RESUMABLE and task.get("kind") == "task"), None)


def cmd_resume(args, layout):
    context = Context(layout, Path.cwd())
    task = find_task(context, args.task)
    if not task:
        print("Nothing to resume in this project.")
        return
    print(task_line(task))
    from .session import session
    with session(layout, context.project["id"], "resume"):
        async def go():
            brain = context.brain
            current = brain.store.get(task["id"])
            if current["status"] in {"blocked", "failed", "integration_conflict", "awaiting_implementer"}:
                brain.retry(current["id"])
                await drain(brain)
            return await handle(context, current, args.yes)
        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            print("\nPaused. Continue with `codingbrain resume`.")


def cmd_accept(args, layout):
    context = Context(layout, Path.cwd())
    task = find_task(context, args.task)
    if not task or task["status"] != "passed":
        raise SystemExit("Only a task whose tests passed can be accepted.")
    asyncio.run(accept(context, task))


def onboard(context: Context, interactive: bool):
    """First launch in a project: discover, import project-local knowledge, ask before private
    agent memory, reconcile with the code and summarize. Later launches sync changes only."""
    from .memory import render_profile
    memory = context.memory
    first = not memory.initialized
    if first:
        print("\nLooking for what earlier agents and the project already know...")
    totals = memory.import_sources(only_changed=not first)
    private = [source for source in memory.scan() if not source.get("authorized") and source.get("status") != "declined"]
    if private and first:
        print(f"Found {len(private)} private memory source(s) from other agents (not imported without your consent):")
        for source in private[:10]:
            print(f"  {source['id']}  {source['agent']:<12} {source['kind']:<20} {source['ref']}")
        if interactive and ask("Import these too (read-only, secrets redacted, kept as untrusted history)?"):
            totals = memory.import_sources(authorize=[source["id"] for source in private])
        elif interactive:
            memory.store.authorize([source["id"] for source in private if source["scope"] != "account_global"], False)
            memory.global_store.authorize([source["id"] for source in private if source["scope"] == "account_global"], False)
            print("Skipped. Import later with `codingbrain memory import --source <id>`.")
    if first or totals.get("new") or totals.get("superseded"):
        print(render_profile(memory.profile()))
    return totals


def cmd_run(args, layout):
    context = Context(layout, Path.cwd())
    context.check()
    print(describe(context.project))
    onboard(context, interactive=sys.stdin.isatty())
    run_goal(context, " ".join(args.goal), args.orchestrate, True if args.yes else None)


def cmd_shell(args, layout):
    context = Context(layout, Path.cwd())
    print(f"Coding Brain {version()}\n" + describe(context.project))
    context.check()
    onboard(context, interactive=sys.stdin.isatty())
    pending = [task for task in context.tasks() if task["status"] in RESUMABLE and task.get("kind") == "task"]
    if pending:
        print(f"\n{len(pending)} unfinished task(s); `codingbrain resume` continues the latest:")
        for task in pending[:5]:
            print("  " + task_line(task))
    print("\nDescribe an engineering goal (prefix with 'orchestrate:' for multi-agent work). Empty line exits.")
    while True:
        try:
            goal = input("\ngoal> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not goal:
            return
        orchestrated = goal.lower().startswith("orchestrate:")
        run_goal(context, goal.split(":", 1)[1].strip() if orchestrated else goal, orchestrated)


def doctor_report(layout: Layout, offline: bool) -> dict:
    checks = []

    def check(name, ok, detail="", core=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "core": core})

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        import importlib
        for module in ("brain.service", "brain.factory", "tree_sitter"):
            importlib.import_module(module)
        check("application", True, f"coding-brain {version()} at {Path(__file__).resolve().parents[1]}")
    except Exception as error:
        check("application", False, f"{type(error).__name__}: {error}")
    git = shutil.which("git")
    check("git", git, git or "not found on PATH; install Git for Windows")
    try:
        layout.ensure()
        probe = layout.data / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        check("state directories", True, str(layout.home))
    except OSError as error:
        check("state directories", False, str(error))
    try:
        config = settings.load(layout)
        schema = config.get("schema_version")
        check("state schema", schema == settings.SCHEMA_VERSION, f"{schema} (this version uses {settings.SCHEMA_VERSION})")
        if settings.configured(config):
            from ..brains import load_brains
            brains_file = layout.home / "config" / "brains.check.json"
            brains_file.write_text(json.dumps(settings.brains_config(config)))
            try:
                load_brains(brains_file)
            finally:
                brains_file.unlink(missing_ok=True)
            check("configuration", True, f"{config['models']['provider']} {config['models']['model']}")
        else:
            check("configuration", True, "not configured yet: run `codingbrain setup`", core=False)
    except Exception as error:
        config = None
        check("configuration", False, f"{type(error).__name__}: {error}")
    from .updater import read_current
    current = read_current(layout)
    if current:
        check("installed version", True, f"active {current.get('version')}, rollback to "
              f"{', '.join(current.get('previous', [])) or 'none'}", core=False)
    if not offline:
        docker = shutil.which("docker")
        images = []
        if docker:
            listed = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                                    capture_output=True, text=True, timeout=30)
            images = listed.stdout.split() if listed.returncode == 0 else []
            wanted = (config or settings.DEFAULTS)["sandbox"]["python_image"]
            check("docker sandbox", wanted in images,
                  f"{wanted} {'ready' if wanted in images else 'missing: run `codingbrain setup --sandbox`'}"
                  if listed.returncode == 0 else "Docker is installed but not running", core=False)
        else:
            check("docker sandbox", False, "Docker not found: tests cannot run until Docker Desktop is installed",
                  core=False)
        if config and config["models"]["provider"] == "ollama":
            try:
                import httpx
                tags = httpx.get(config["models"]["url"].rstrip("/") + "/api/tags", timeout=5, trust_env=False).json()
                names = [item["name"] for item in tags.get("models", [])]
                wanted = config["models"]["model"]
                found = any(name == wanted or name.split(":")[0] == wanted.split(":")[0] for name in names)
                check("local model", found, f"Ollama has {len(names)} model(s); {wanted or 'none selected'} "
                      f"{'available' if found else 'not pulled'}", core=False)
            except Exception as error:
                check("local model", False, f"Ollama not reachable at {config['models']['url']}: "
                      f"{type(error).__name__}", core=False)
        for name, detail in premium_status().items():
            check(f"premium: {name}", detail["usable"], detail["detail"], core=False)
    return {"ok": all(item["ok"] for item in checks if item["core"]), "version": version(), "checks": checks}


def premium_status() -> dict:
    """What the signed-in Claude and Codex CLIs actually allow, using the existing connectors.
    A subscription sign-in is reported as such; API-key billing is refused, never assumed."""
    from ..subscriptions import ClaudeCodeSupervisor, CodexSupervisor, SubscriptionError
    found = {}
    for name, kind in (("claude", ClaudeCodeSupervisor), ("codex", CodexSupervisor)):
        supervisor = kind(name)
        if not shutil.which(supervisor.command):
            found[name] = {"usable": False, "detail": f"{supervisor.command} CLI not installed"}
            continue
        try:
            asyncio.run(supervisor.verify())
            found[name] = {"usable": True, "detail": "signed in with a subscription; Coding Brain uses it "
                           "read-only, within your configured budgets and the CLI's own usage limits"}
        except SubscriptionError as error:
            found[name] = {"usable": False, "detail": str(error)}
        except Exception as error:
            found[name] = {"usable": False, "detail": f"{type(error).__name__}: {error}"[:200]}
    return found


def cmd_doctor(args, layout):
    report = doctor_report(layout, args.offline)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            mark = "ok " if item["ok"] else ("ERR" if item["core"] else "-- ")
            print(f"[{mark}] {item['name']:<20} {item['detail']}")
        print("\nHealthy." if report["ok"] else "\nProblems found above.")
    return 0 if report["ok"] else 1


def cmd_setup(args, layout):
    layout.ensure()
    config = settings.load(layout)
    models = config["models"]
    interactive = sys.stdin.isatty() and not args.non_interactive
    print("Coding Brain setup. No credentials are stored: hosted providers read keys from environment "
          "variables you name, and Claude/Codex use their own signed-in CLIs.\n")
    if args.provider:
        models["provider"] = args.provider
    if args.url:
        models["url"] = args.url
    if interactive and not args.model:
        models["provider"] = prompt("Model provider (ollama or openai-compatible)", models["provider"])
        models["url"] = prompt("Model server URL", models["url"] if models["provider"] == "ollama" else
                               models.get("url") or "http://localhost:1234/v1")
        if models["provider"] == "ollama":
            try:
                import httpx
                names = [item["name"] for item in httpx.get(models["url"].rstrip("/") + "/api/tags", timeout=5,
                                                            trust_env=False).json().get("models", [])]
                print("Installed Ollama models: " + (", ".join(names) or "none (run `ollama pull <model>`)"))
            except Exception:
                print(f"Ollama is not reachable at {models['url']} yet; you can still save the settings.")
        else:
            models["api_key_env"] = prompt("Environment variable holding its API key (blank for none)",
                                           models.get("api_key_env") or "") or None
    models["model"] = args.model or (prompt("Main model", models["model"]) if interactive else models["model"])
    models["fast_model"] = args.fast_model if args.fast_model is not None else (
        prompt("Optional faster model for simple tasks", models["fast_model"]) if interactive else models["fast_model"])
    premium = premium_status() if (interactive or args.enable_claude or args.enable_codex) else {}
    for name in ("claude", "codex"):
        wanted = getattr(args, f"enable_{name}")
        if interactive and wanted is None:
            detail = premium.get(name, {})
            print(f"{name}: {detail.get('detail', 'unknown')}")
            wanted = detail.get("usable") and ask(f"Use {name} for budgeted planning and diagnosis?", True)
        if wanted is not None:
            config["supervisors"][name]["enabled"] = bool(wanted)
            if wanted and premium and not premium.get(name, {}).get("usable"):
                print(f"Warning: {name} is enabled but not usable now: {premium.get(name, {}).get('detail')}")
    if interactive:
        for key in ("plan_budget", "diagnose_budget", "daily_limit"):
            config["budgets"][key] = int(prompt(f"Premium {key.replace('_', ' ')}", str(config["budgets"][key])))
        config["autonomy"]["execution"] = prompt("Execution approval: propose (ask before running each plan) "
                                                 "or auto", config["autonomy"]["execution"])
        roots = prompt("Allowed project roots, separated by ';' (blank = any folder)",
                       ";".join(config["permissions"]["allowed_roots"]))
        config["permissions"]["allowed_roots"] = [item.strip() for item in roots.split(";") if item.strip()]
    if args.execution:
        config["autonomy"]["execution"] = args.execution
    if args.allowed_root:
        config["permissions"]["allowed_roots"] = args.allowed_root
    if args.channel:
        config["update"]["channel"] = args.channel
    if config["autonomy"]["execution"] not in {"propose", "auto"}:
        raise SystemExit("Execution approval must be propose or auto")
    path = settings.save(layout, config)
    print(f"Saved {path}")
    if args.sandbox or (interactive and ask("Build the Docker test sandbox images now?", True)):
        build_sandbox(config)
    if args.knowledge or (interactive and ask("Import the engineering knowledge library now (downloads "
                                               "license-checked sources from GitHub)?", False)):
        manifest = Path(__file__).parent / "assets" / "knowledge.example.json"
        os.environ["BRAIN_KNOWLEDGE_DB"] = str(layout.data / "knowledge.sqlite3")
        subprocess.run([sys.executable, "-m", "brain.knowledge", "sync", str(manifest)], check=False)
    return 0


def build_sandbox(config: dict):
    if not shutil.which("docker"):
        print("Docker is not installed; install Docker Desktop, then run `codingbrain setup --sandbox`.")
        return
    assets = Path(__file__).parent / "assets"
    for dockerfile, image in (("Dockerfile.sandbox", config["sandbox"]["python_image"]),
                              ("Dockerfile.sandbox.node", config["sandbox"]["node_image"])):
        print(f"Building {image}")
        subprocess.run(["docker", "build", "-t", image, "-f", str(assets / dockerfile), str(assets)], check=False)


def release_source(args, config):
    from .updater import DirectorySource, GitHubSource
    if args.source:
        return DirectorySource(Path(args.source))
    return GitHubSource(channel=args.channel or config["update"].get("channel", "stable"))


def cmd_update(args, layout):
    from .updater import UpdateError, install_release, read_current
    current = read_current(layout)
    if not current:
        raise SystemExit("This copy was not installed with the installer (no app/current.json); "
                         "update it with Git or reinstall with install.ps1.")
    config = settings.load(layout)
    source = release_source(args, config)
    try:
        if args.check:
            release = source.release(args.version)
            from packaging.version import Version
            newer = Version(release.version) > Version(current["version"])
            print(f"Installed {current['version']}; available {release.version} ({release.tag})"
                  + ("; run `codingbrain update`" if newer else "; up to date"))
            return 0
        result = install_release(layout, source, current["version"], args.version, args.force)
    except UpdateError as error:
        print(f"Update not applied: {error}")
        return 1
    if result["status"] == "up_to_date":
        print(f"Coding Brain {result['installed']} is up to date.")
    else:
        print(f"Updated {result['previous']} -> {result['installed']}. Backup: {result['backup']}. "
              "Roll back with `codingbrain rollback`.")
        if result.get("notes"):
            print("\nRelease notes:\n" + result["notes"][:4000])
    return 0


def cmd_rollback(args, layout):
    from .updater import UpdateError, rollback
    try:
        result = rollback(layout, args.restore_state)
    except UpdateError as error:
        print(f"Rollback not applied: {error}")
        return 1
    print(f"Active version is now {result['version']}.")
    return 0


def cmd_memory(args, layout):
    from .memory import AUTHORITY, render_profile
    context = Context(layout, Path.cwd())
    context.register()
    memory = context.memory
    action = args.action
    if action == "scan":
        for source in memory.scan():
            state = ("imported" if source.get("imported_sha256") == source["sha256"] else
                     "changed" if source.get("imported_sha256") else
                     "needs authorization" if not source.get("authorized") else "new")
            print(f"{source['id']}  {source['scope']:<15} {source['agent']:<13} {source['kind']:<20} {state:<20} {source['ref']}")
    elif action in {"import", "sync"}:
        authorize = list(args.source or [])
        if args.all_private:
            authorize += [source["id"] for source in memory.scan() if not source.get("authorized")]
        totals = memory.import_sources(authorize=authorize, only_changed=action == "sync")
        print(json.dumps(totals) if args.json else ", ".join(f"{key} {value}" for key, value in totals.items()))
    elif action == "status":
        status = memory.status()
        print(json.dumps(status, indent=2) if args.json else "\n".join(f"{key}: {value}" for key, value in status.items()))
    elif action == "show":
        profile = memory.profile()
        print(json.dumps(profile, indent=2) if args.json else render_profile(profile))
    elif action == "conflicts":
        if args.resolve is not None:
            memory.store.resolve(args.resolve, args.keep or "both")
            print(f"Conflict {args.resolve} resolved: keep {args.keep or 'both'}")
        for item in memory.store.conflicts():
            print(f"#{item['id']} {item['reason']}\n  a {item['a']}: {(item['a_record'] or {}).get('text', '')[:200]}\n"
                  f"  b {item['b']}: {(item['b_record'] or {}).get('text', '')[:200]}")
        print("Resolve with `codingbrain memory conflicts --resolve <id> --keep a|b|both`.")
    elif action == "forget":
        for record_id in args.ids:
            memory.store.set_status(record_id, "removed", "removed by the user")
        print(f"Removed {len(args.ids)} record(s); they stay in the audit log.")
    elif action == "approve":
        for record_id in args.ids:
            memory.store.approve(record_id)
        print(f"Approved {len(args.ids)} record(s) as project rules (authority 2).")
    elif action == "rule":
        text = " ".join(args.text)
        store = memory.global_store if args.global_ else memory.store
        record = store.add("rule", text, "user", 2, "approved", "codingbrain memory rule")
        print(f"Added {'global' if args.global_ else 'project'} rule {record} (approved, authority 2).")
    elif action == "contribute":
        # A controlled adapter for other agents: a findings file is imported as unverified history.
        path = Path(args.file).resolve()
        if not path.is_file() or not path.is_relative_to(context.root):
            raise SystemExit("Contributions must be a file inside this project.")
        from .memory import _source, parse_markdown
        source = _source("contribution", args.agent, "contribution", path, "generated")
        memory.store.note_sources([source])
        counts = memory.store.upsert(source, [dict(item, category=item["category"] if item["category"] != "rule" else "note")
                                              for item in parse_markdown(path.read_text(encoding="utf-8"), "note")])
        memory.reconcile()
        print(f"Recorded {counts['new']} finding(s) from {args.agent} as unverified history.")
    if args.explain:
        print("\nAuthority (lower wins): " + "; ".join(f"{level} {name}" for level, name in AUTHORITY.items()))
    return 0


def cmd_migrate(args, layout):
    from .migrations import migrate
    result = migrate(layout.ensure())
    print(json.dumps(result) if args.json else f"State schema {result['from']} -> {result['to']}")


def cmd_post_install(args, layout):
    """Called by install.ps1 after it installs a verified release into app/versions/<version>."""
    from .updater import prune, switch
    from .migrations import migrate
    layout.ensure()
    migrate(layout)
    report = doctor_report(layout, offline=True)
    if not report["ok"]:
        print(json.dumps(report, indent=2))
        return 1
    switch(layout, args.version, args.base_python)
    prune(layout)
    print(f"Coding Brain {args.version} installed. Launcher: {layout.bin}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="codingbrain", description="Coding Brain for the project in the "
                                     "current directory.")
    parser.add_argument("--version", action="version", version=f"codingbrain {version()}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init", help="recognize and register this project (read-only)")
    commands.add_parser("status", help="project, configuration and tasks")
    commands.add_parser("tasks", help="all tasks for this project")
    resume = commands.add_parser("resume", help="continue the latest unfinished task, or the given one")
    resume.add_argument("task", nargs="?")
    resume.add_argument("--yes", action="store_true", help="run the plan without asking")
    accept_parser = commands.add_parser("accept", help="accept a tested task as a new branch")
    accept_parser.add_argument("task", nargs="?")
    run = commands.add_parser("run", help="work on one goal")
    run.add_argument("goal", nargs="+")
    run.add_argument("--orchestrate", action="store_true", help="split into dependent assignments")
    run.add_argument("--yes", action="store_true", help="run plans without asking (acceptance still asks)")
    doctor = commands.add_parser("doctor", help="check the installation, models and premium CLIs")
    doctor.add_argument("--offline", action="store_true", help="only local checks")
    doctor.add_argument("--json", action="store_true")
    setup = commands.add_parser("setup", help="configure models, premium supervisors, budgets and approvals")
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--provider", choices=["ollama", "openai"])
    setup.add_argument("--url")
    setup.add_argument("--model")
    setup.add_argument("--fast-model")
    setup.add_argument("--enable-claude", action=argparse.BooleanOptionalAction, default=None)
    setup.add_argument("--enable-codex", action=argparse.BooleanOptionalAction, default=None)
    setup.add_argument("--execution", choices=["propose", "auto"])
    setup.add_argument("--allowed-root", action="append")
    setup.add_argument("--channel", choices=["stable", "dev"])
    setup.add_argument("--sandbox", action="store_true", help="build the Docker test sandbox images")
    setup.add_argument("--knowledge", action="store_true", help="import the engineering knowledge library")
    update = commands.add_parser("update", help="install the latest release (stable by default)")
    update.add_argument("--check", action="store_true", help="only report whether an update exists")
    update.add_argument("--channel", choices=["stable", "dev"])
    update.add_argument("--version", help="install this exact release version")
    update.add_argument("--source", help="a folder with release assets instead of GitHub")
    update.add_argument("--force", action="store_true", help="reinstall even if not newer")
    rollback_parser = commands.add_parser("rollback", help="switch back to the previous installed version")
    rollback_parser.add_argument("--restore-state", action="store_true",
                                 help="also restore the state backup taken before the update")
    memory_parser = commands.add_parser("memory", help="cross-agent project memory: scan, import, status, sync, "
                                        "conflicts, show, forget, approve, rule, contribute")
    memory_parser.add_argument("action", choices=["scan", "import", "status", "sync", "conflicts", "show", "forget",
                                                  "approve", "rule", "contribute"])
    memory_parser.add_argument("ids", nargs="*", help="record ids (forget, approve) or rule text (rule)")
    memory_parser.add_argument("--source", action="append", help="authorize and import this private source id")
    memory_parser.add_argument("--all-private", action="store_true", help="authorize every discovered private source")
    memory_parser.add_argument("--resolve", type=int, help="conflict id to resolve")
    memory_parser.add_argument("--keep", choices=["a", "b", "both"])
    memory_parser.add_argument("--global", dest="global_", action="store_true", help="rule: for every project")
    memory_parser.add_argument("--agent", default="external", help="contribute: the contributing agent")
    memory_parser.add_argument("--file", help="contribute: a findings file inside the project")
    memory_parser.add_argument("--json", action="store_true")
    memory_parser.add_argument("--explain", action="store_true", help="print the authority hierarchy")
    commands.add_parser("version", help="print the version")
    migrate_parser = commands.add_parser("migrate", help=argparse.SUPPRESS)
    migrate_parser.add_argument("--json", action="store_true")
    post = commands.add_parser("post-install", help=argparse.SUPPRESS)
    post.add_argument("--version", required=True)
    post.add_argument("--base-python")
    args = parser.parse_args(argv)
    if args.command == "memory" and args.action == "rule":
        args.text = args.ids
    for stream in (sys.stdout, sys.stderr):  # never crash on a console that cannot show a character
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    layout = Layout.default()
    handlers = {"init": cmd_init, "status": cmd_status, "tasks": cmd_tasks, "resume": cmd_resume,
                "accept": cmd_accept, "run": cmd_run, "doctor": cmd_doctor, "setup": cmd_setup,
                "update": cmd_update, "rollback": cmd_rollback, "migrate": cmd_migrate,
                "post-install": cmd_post_install, "memory": cmd_memory, None: cmd_shell,
                "version": lambda args, layout: print(f"codingbrain {version()}")}
    return handlers[args.command](args, layout) or 0
