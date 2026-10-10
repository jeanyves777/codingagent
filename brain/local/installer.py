"""`codingbrain install`: prepare the whole environment, with permission, safely resumable.

  1. Preflight: Windows version and build, architecture, memory, disk, administrator rights,
     winget, PowerShell execution policy, network, proxy variables and virtualization signals.
  2. A plan: each component's state from a real check and what would be done (source, download
     size, whether Windows will ask for administrator rights, licence terms).
  3. Permission for each change (or once for the shown plan with --yes), then the changes in
     order: Python and Git, WSL 2, Docker Desktop, Ollama, the model, Claude Code, Codex, the
     sandbox images, the knowledge library and the multimodal extras.
  4. Checkpoints after every step in <home>/install/state.json: a rerun re-checks and skips what
     is done; a step interrupted midway is checked again rather than assumed; after a restart
     `codingbrain install --resume` (or the one-time resume you allowed) continues.
  5. Readiness levels from deep checks, kept as evidence for `codingbrain doctor`.

Progress goes to the activity journal (<home>/data/install.sqlite3), so it is shown live here and
`codingbrain watch --install` can follow it from another terminal. Progress text is what the
tools report (bytes downloaded by Ollama, winget's and Docker's own output) and elapsed time;
nothing is estimated. No credentials are read, stored or logged.
"""
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config as settings
from .components import BY_ID, COMPONENTS, GB, PROFILES, Env, Status, check_all
from .paths import Layout
from .readiness import assess, render

LOCK_STALE = 6 * 3600


class InstallState:
    """The checkpoint file. Written atomically after each step."""

    def __init__(self, layout: Layout):
        self.path = layout.home / "install" / "state.json"
        self.evidence_path = layout.home / "install" / "evidence.json"
        self.data = self._read(self.path) or {"steps": {}, "declined": [], "profile": None, "runs": []}
        self.evidence = self._read(self.evidence_path) or {}

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for path, payload in ((self.path, self.data), (self.evidence_path, self.evidence)):
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            os.replace(temporary, path)

    def step(self, component_id: str, state: str, detail: str = ""):
        self.data["steps"][component_id] = {"state": state, "detail": detail[:500], "at": time.time()}
        self.save()


def preflight(system, layout: Layout, profile: str) -> dict:
    """Facts about this computer and anything that stops the chosen profile from installing."""
    facts = system.facts()
    ram = facts.get("ram") or 0
    free = system.disk_free(layout.home)
    gpu = system.gpu()
    report = {"os": facts.get("caption") or system.platform, "build": facts.get("build"), "arch": facts.get("arch"),
              "cpu": facts.get("cpu"), "cores": facts.get("cores"), "ram_gb": round(ram / GB, 1) if ram else None,
              "disk_free_gb": round(free / GB, 1), "gpu": gpu and {"name": gpu["name"], "vram_gb": round(gpu["vram"] / GB, 1)},
              "admin": facts.get("admin"), "execution_policy": facts.get("execution_policy"),
              "winget": bool(system.which("winget")) if system.platform == "windows" else None,
              "proxy": sorted(name for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY")
                              if system.environ().get(name)),  # names only; values may hold credentials
              "simulated": system.simulated, "blockers": [], "warnings": []}
    from .system import assess_virtualization
    report["virtualization"] = assess_virtualization(facts)
    network = {}
    for name, url in (("github", "https://api.github.com"), ("ollama_library", "https://registry.ollama.ai/v2/"),
                      ("docker_hub", "https://registry-1.docker.io/v2/")):
        code, _ = system.http("GET", url, timeout=10)
        network[name] = code != 0  # any HTTP answer (even 401) means it is reachable
    report["network"] = network
    need, comfortable = (4, 10) if profile == "local" else (10, 25)
    if free < need * GB:
        report["blockers"].append(f"only {free / GB:.1f} GB free on the drive holding {layout.home}; the {profile} "
                                  f"profile needs at least {need} GB")
    elif free < comfortable * GB:
        report["warnings"].append(f"{free / GB:.1f} GB free: a complete {profile} installation can use about "
                                  f"{comfortable} GB (each download is checked against free space first)")
    if ram and ram < 8 * GB:
        report["warnings"].append(f"{ram / GB:.0f} GB of memory: only small local models will run well")
    if system.platform == "windows":
        if (facts.get("build") or 0) and facts["build"] < 19041 and profile == "full":
            report["blockers"].append(f"Windows build {facts['build']} is too old for WSL 2 and Docker Desktop "
                                      "(needs 19041 / version 2004 or newer)")
        if not report["winget"]:
            report["warnings"].append("winget is not available: install 'App Installer' from the Microsoft Store to "
                                      "let Coding Brain install components; otherwise each is explained for you")
        if facts.get("execution_policy") in {"Restricted", "AllSigned"}:
            report["warnings"].append(f"PowerShell execution policy is {facts['execution_policy']}: the launcher and "
                                      "updates use .cmd files and are not affected; scripts you run need -ExecutionPolicy Bypass")
        if profile == "full" and report["virtualization"]["state"] == "possibly_disabled":
            report["warnings"].append(report["virtualization"]["advice"])
    if not any(network.values()):
        report["warnings"].append("no network access: downloads will fail; components already present still work"
                                  + (" (proxy variables are set: " + ", ".join(report["proxy"]) + ")" if report["proxy"] else ""))
    return report


class Installer:
    def __init__(self, layout: Layout, system=None, ask=None, yes: bool = False, mode: str = "live",
                 stream=None, interactive: bool | None = None):
        from .system import System
        self.layout = layout.ensure()
        self.system = system or System()
        self.yes = yes
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.ask = ask or self._ask
        self.mode, self.stream = mode, stream or sys.stdout
        self.state = InstallState(self.layout)
        self.config = settings.load(self.layout)
        self.run_id = "install-" + uuid.uuid4().hex[:10]
        from ..telemetry import Telemetry
        self.journal = Telemetry(self.layout.data / "install.sqlite3")
        self.view = None
        self.env = Env(self.system, self.layout, self.config, progress=self.progress, evidence=self.state.evidence)

    # output and consent -------------------------------------------------------------------------
    def say(self, text: str = ""):
        if self.mode == "json":
            return
        if self.view:
            self.view.pause()
        print(text, file=self.stream, flush=True)
        if self.view:
            self.view.resume()

    def _ask(self, question: str, default: bool = False) -> bool:
        if self.yes:
            return True
        if not self.interactive:
            return False
        from .cli import ask
        if self.view:
            self.view.pause()
        try:
            return ask(question, default)
        finally:
            if self.view:
                self.view.resume()

    def event(self, event_type: str, component: str | None = None, status: str | None = None, summary: str = "",
              duration: float | None = None, data: dict | None = None):
        try:
            self.journal.append(event_type, task_id=self.run_id, agent="installer",
                                phase=f"install:{component}" if component else "install", status=status,
                                summary=summary, duration=duration, data=data)
        except Exception:
            pass  # progress display never stops an installation

    def progress(self, text: str):
        """A line a tool reported (rate limited), or the elapsed time while it is quiet."""
        now, source = time.monotonic(), text.split(" ", 1)[0]
        last = getattr(self, "_last_by_source", {})
        if now - last.get(source, 0) < 2 and not text.startswith(("waiting", "ollama: success")):
            return
        self._last_by_source = {**last, source: now}
        self._last_progress = now
        self.event("progress", getattr(self, "_current", None), "RUNNING", " ".join(text.split())[:300])

    @contextmanager
    def operation(self, component_id: str, summary: str):
        """A long step: RUNNING with a heartbeat while it is quiet, then its outcome."""
        from .. import accounting
        self._current = component_id
        started = time.monotonic()
        self.event("operation", component_id, "RUNNING", summary)
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(accounting.HEARTBEAT_SECONDS):
                if time.monotonic() - getattr(self, "_last_progress", 0) >= accounting.HEARTBEAT_SECONDS:
                    self.event("progress", component_id, "RUNNING",
                               f"still working: {int(time.monotonic() - started)} s elapsed (no new output)")
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        outcome = {"status": "COMPLETED", "summary": summary}
        try:
            yield outcome
        except BaseException as error:
            outcome.update(status="FAILED", summary=f"{summary}: {type(error).__name__}: {str(error)[:300]}")
            raise
        finally:
            stop.set()
            self._current = None
            self.event("operation", component_id, outcome["status"], outcome["summary"],
                       duration=time.monotonic() - started)

    # the run ---------------------------------------------------------------------------------
    def components_for(self, profile: str, only=None, skip=()) -> list[str]:
        ids = list(only) if only else PROFILES[profile]
        unknown = [item for item in ids if item not in BY_ID]
        if unknown:
            raise SystemExit(f"Unknown component(s): {', '.join(unknown)}. Known: {', '.join(BY_ID)}")
        return [item for item in [c.id for c in COMPONENTS] if item in ids and item not in skip]

    def plan(self, profile: str, only=None, skip=(), deep: bool = False) -> dict:
        ids = self.components_for(profile, only, skip)
        results = check_all(self.env, ids, deep=deep)
        return {"profile": profile, "components": {key: results[key].to_dict() for key in ids}}

    def install(self, profile: str | None = None, only=None, skip=(), resume: bool = False, plan_only: bool = False,
                retry_declined: bool = False, register_resume: bool | None = None, deep_plan: bool = False) -> dict:
        profile = profile or self.state.data.get("profile") or "local"
        if self._locked():
            raise SystemExit("Another Coding Brain installation is running on this computer. Wait for it to finish "
                             "(`codingbrain watch --install` follows it).")
        self.state.data["profile"] = profile
        self.state.data["runs"] = (self.state.data.get("runs") or [])[-19:] + [{"id": self.run_id, "at": time.time(),
                                                                                "profile": profile}]
        if retry_declined:
            self.state.data["declined"] = []
        elif only:  # naming a component asks about it again
            self.state.data["declined"] = [item for item in self.state.data.get("declined", []) if item not in only]
        if self.state.evidence.get("pending_restart") and resume:
            self.state.evidence.pop("pending_restart")  # check whether the restart finished the job
            self.system.clear_resume()
        self._lock()
        from .live import LiveView
        try:
            with LiveView(self.journal, self.mode, task_ids=[self.run_id], stream=self.stream) as view:
                self.view = view
                return self._install(profile, only, skip, plan_only, register_resume, deep_plan)
        finally:
            self.view = None
            self.state.save()
            self._unlock()

    def _install(self, profile, only, skip, plan_only, register_resume, deep_plan=False) -> dict:
        self.event("stage", "preflight", "RUNNING", f"Checking this computer ({profile} profile)")
        facts = preflight(self.system, self.layout, profile)
        self.event("stage", "preflight", "BLOCKED" if facts["blockers"] else "COMPLETED", "Preflight",
                   data={key: value for key, value in facts.items() if key != "virtualization"})
        self.say(render_preflight(facts))
        for blocker in facts["blockers"]:
            self.say(f"Blocked: {blocker}")
        if facts["blockers"] and not plan_only:
            return self.finish(profile, facts, blocked=True)
        ids = self.components_for(profile, only, skip)
        self.env.results.clear()
        if deep_plan:
            with self.operation("verify", "full checks of every component"):
                check_all(self.env, ids, deep=True)
        else:
            check_all(self.env, ids)
        changes = [cid for cid in ids if self.env.results[cid].action and cid not in self.state.data["declined"]]
        self.say("\nPlan:")
        for cid in ids:
            status = self.env.results[cid]
            if cid in self.state.data["declined"]:
                self.say(f"  {BY_ID[cid].title}: skipped (you declined it earlier; --retry-declined asks again)")
            elif status.action:
                for line in BY_ID[cid].plan_lines(self.env, status):
                    self.say("  " + line)
                if status.detail:
                    self.say(f"    now: {status.detail}")
            else:
                self.say(f"  {BY_ID[cid].title}: {status.state} — {status.detail}")
        if plan_only or not changes:
            if not changes:
                self.say("\nNothing to install.")
            return self.finish(profile, facts, deep=not plan_only)
        if self.yes:
            self.say("\n--yes: proceeding with the plan above (Windows still asks before any administrator change).")
        for cid in ids:
            if cid in self.state.data["declined"]:
                continue
            component = BY_ID[cid]
            status = component.check(self.env) if cid not in changes else self.env.results[cid]
            waiting = [need for need in component.requires if need in self.env.results and not self.env.results[need].ready]
            if waiting:
                status = Status("waiting", "needs " + ", ".join(BY_ID[need].title for need in waiting) + " first")
                self.env.results[cid] = status
                self.state.step(cid, "waiting", status.detail)
                continue
            if not status.action:
                self.env.results[cid] = status
                self.state.step(cid, "done" if status.ready else status.state, status.detail)
                continue
            if status.action in {"manual", "restart"}:
                self.say(f"\n{component.title}: {status.detail}")
                self.state.step(cid, status.state, status.detail)
                if status.action == "restart":
                    return self.finish(profile, facts, restart=cid, register_resume=register_resume)
                continue
            if status.action == "sign_in" and not self.interactive:
                detail = f"{status.detail}; sign in from a terminal: codingbrain install --only {cid}"
                self.say(f"\n{component.title}: {detail}")
                self.state.step(cid, status.state, detail)
                continue
            question = (f"{component.title}: {status.action.replace('_', ' ')} now?" if status.action != "install" else
                        f"Install {component.title}" + (f" ({component.size})" if component.size else "") + "?")
            if not self.ask(question, not component.optional):
                self.state.data["declined"].append(cid)
                self.state.step(cid, "declined", "you declined")
                self.event("stage", cid, "BLOCKED", f"{component.title}: declined")
                continue
            self.state.step(cid, "running", status.action)
            self.acted = getattr(self, "acted", set()) | {cid}
            try:
                with self.operation(cid, f"{component.title}: {status.action.replace('_', ' ')}") as outcome:
                    result = component.install(self.env, status)
                    outcome["status"] = "COMPLETED" if result.ready else "BLOCKED" if result.state in {
                        "needs_restart", "needs_sign_in", "waiting", "unsupported"} else "FAILED"
                    outcome["summary"] = f"{component.title}: {result.state} — {result.detail}"
            except KeyboardInterrupt:
                self.state.step(cid, "interrupted", "stopped by you; rerun to continue")
                raise
            except Exception as error:
                result = Status("failed", f"{type(error).__name__}: {str(error)[:300]}")
            self.env.results[cid] = result
            self.state.step(cid, "done" if result.ready else result.state, result.detail)
            if result.state == "needs_restart":
                return self.finish(profile, facts, restart=cid, register_resume=register_resume)
        return self.finish(profile, facts, deep=True)

    def finish(self, profile, facts, deep=False, blocked=False, restart=None, register_resume=None) -> dict:
        ids = PROFILES[profile]
        outcomes = {cid: self.env.results[cid] for cid in getattr(self, "acted", ()) if cid in self.env.results}
        if not blocked and not restart and deep:
            self.event("stage", "verify", "RUNNING", "Verifying: model generation, sandbox start, sign-ins")
            self.env.results.clear()
            with self.operation("verify", "deep readiness checks"):
                check_all(self.env, [c.id for c in COMPONENTS], deep=True)
            self.event("stage", "verify", "COMPLETED", "Verification")
        else:
            self.env.results.clear()
            check_all(self.env, [c.id for c in COMPONENTS])
        for cid, outcome in outcomes.items():  # a step's own failure says more than a later "missing"
            if not self.env.results[cid].ready and outcome.state not in {"waiting"}:
                self.env.results[cid] = outcome
        report = readiness_report(self.layout, self.env, profile, ids, self.state.data.get("declined", []))
        report["preflight"] = facts
        report["run"] = self.run_id
        if restart:
            report["restart_required"] = restart
            self.say(f"\nWindows must restart to finish {BY_ID[restart].title}.")
            command = f'"{sys.executable}" -m brain.local install --resume'
            registered = False
            if register_resume or (register_resume is None and self.ask(
                    "Continue the installation automatically once after you sign in again?", True)):
                registered = self.system.register_resume(f'cmd.exe /k {command}')
            report["resume_registered"] = registered
            self.say("Save your work and restart Windows. " + ("Setup continues after you sign in."
                                                                if registered else "Then run: codingbrain install --resume"))
        self.state.data["last"] = {"at": time.time(), "level": report["level"], "profile": profile}
        self.state.save()
        self.event("stage", None, "COMPLETED" if report["level"] not in {"blocked"} else "BLOCKED",
                   f"Readiness: {report['label']}", data={"level": report["level"], "reached": report["reached"]})
        self.say("\n" + render(report))
        return report

    # one installation at a time ------------------------------------------------------------------------
    def _lock_path(self) -> Path:
        return self.layout.locks / "install.json"

    def _locked(self) -> bool:
        from .session import pid_alive
        try:
            holder = json.loads(self._lock_path().read_text())
        except (OSError, ValueError):
            return False
        return holder.get("pid") != os.getpid() and pid_alive(int(holder.get("pid", 0))) and \
            time.time() - holder.get("at", 0) < LOCK_STALE

    def _lock(self):
        self._lock_path().write_text(json.dumps({"pid": os.getpid(), "at": time.time(), "run": self.run_id}))

    def _unlock(self):
        self._lock_path().unlink(missing_ok=True)


def readiness_report(layout, env: Env, profile: str | None, expected=(), declined=()) -> dict:
    from .cli import doctor_report
    app = doctor_report(layout, offline=True)
    problems = [item["name"] for item in app["checks"] if item["core"] and not item["ok"]]
    report = assess(env.results, app_ok=not problems, app_problems=problems, declined=declined, config=env.config,
                    profile=profile, expected=expected)
    report["components"] = {key: value.to_dict() for key, value in env.results.items()}
    return report


def render_preflight(facts: dict) -> str:
    lines = ["This computer" + (" (SIMULATED)" if facts.get("simulated") else "") + ":",
             f"  {facts['os']}" + (f" build {facts['build']}" if facts.get("build") else "") + f", {facts.get('arch')}",
             f"  memory {facts.get('ram_gb')} GB, free disk {facts['disk_free_gb']} GB"
             + (f", GPU {facts['gpu']['name']} ({facts['gpu']['vram_gb']} GB)" if facts.get("gpu") else ""),
             f"  administrator: {'yes' if facts.get('admin') else 'no (Windows will ask when a step needs it)'}"
             + (f", winget: {'yes' if facts['winget'] else 'no'}" if facts.get("winget") is not None else ""),
             f"  virtualization: {facts['virtualization']['state'].replace('_', ' ')} — "
             + "; ".join(facts["virtualization"]["evidence"]),
             "  network: " + ", ".join(f"{name} {'reachable' if ok else 'unreachable'}" for name, ok in facts["network"].items())]
    lines.extend(f"  warning: {warning}" for warning in facts["warnings"])
    return "\n".join(lines)


def selftest(layout: Layout, keep: bool = False, stream=None) -> dict:
    """A disposable end-to-end task: a new throwaway Git project with a failing test, a real goal
    for the configured model, the real sandbox, and the result checked. Your projects are not
    involved; the throwaway project and its Coding Brain data are deleted afterwards."""
    import asyncio
    import shutil
    import subprocess
    import tempfile
    from .cli import Context, drain
    stream = stream or sys.stdout
    root = Path(tempfile.mkdtemp(prefix="codingbrain-selftest-")) / "selftest-project"
    root.mkdir()
    (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calculator.py").write_text("from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                                             encoding="utf-8")
    git = ["git", "-C", str(root), "-c", "user.name=Coding Brain selftest", "-c", "user.email=selftest@localhost"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "selftest: a calculator with a bug"], check=True)
    started = time.time()
    result = {"project": str(root), "goal": "Fix add() in calculator.py so that the existing test passes.",
              "status": None, "passed": False}
    context = None
    try:
        context = Context(layout, root)
        brain = context.brain

        async def go():
            task = brain.submit(context.root.name, result["goal"], launch=False)
            task = await brain.create(task)
            for _ in range(10):
                await drain(brain)
                task = brain.store.get(task["id"])
                if task["status"] == "proposed" and task["proposal"]["changes"]:
                    await brain.execute(task["id"], task["digest"])
                    continue
                if task["status"] in {"passed", "failed", "blocked", "awaiting_implementer"} or not brain.jobs:
                    break
            return brain.store.get(task["id"])
        task = asyncio.run(go())
        evidence = task.get("test_evidence") or {}
        result.update(status=task["status"], passed=task["status"] == "passed" and evidence.get("passed") is True,
                      tests_exit_code=evidence.get("exit_code"), author=task.get("proposal_author"),
                      model=context.config["models"]["model"], seconds=round(time.time() - started, 1),
                      files=[change["path"] for change in (task.get("proposal") or {}).get("changes", [])],
                      failure=(task.get("failure_log") or [{}])[-1].get("summary", "")[:400] if task["status"] != "passed" else "")
    except SystemExit as error:
        result.update(status="blocked", failure=str(error))
    except Exception as error:
        result.update(status="error", failure=f"{type(error).__name__}: {str(error)[:400]}")
    finally:
        if not keep:
            shutil.rmtree(root.parent, ignore_errors=True)
            if context is not None:
                shutil.rmtree(context.data, ignore_errors=True)
    return result
