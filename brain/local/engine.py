"""The typed, versioned engine API: what the desktop app (and any other program) uses to drive
Coding Brain, without parsing formatted command output and without its own copy of the logic.

    codingbrain api --describe     the schema (operations, parameters, results) as JSON
    codingbrain api --stdio        JSON lines: {"id", "op", "params"} in; {"id", "ok", "result" | "error"}
                                   out, plus {"event": ..., "op_id": ...} lines while a long operation runs

Each operation is either `read` or `action`.

Read operations never change a project, start work or spend premium budget; conversation is
read-only too. An action does exactly what its name says, and only when called. In particular:
  - conversation.send only proposes work, as an `action` for the caller to confirm;
  - tasks.approve runs a proposal only with its current digest, so an approval is bound to what
    was shown and cannot be replayed once the task has moved on;
  - acceptance creates a new branch and never changes the checked-out one.
Every action goes through the same service authority checks as the CLI: path safety, the
sandbox, review, premium budgets and digest binding. The API adds no shortcuts and has no --yes.
"""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

from . import config as settings
from .paths import Layout

API_VERSION = "1.0"


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


OPERATIONS = {
    # name: (kind, {param: (type, required)}, result description)
    "engine.info": ("read", {}, "engine and API versions, supported operations, data folder"),
    "engine.describe": ("read", {}, "this schema"),
    "doctor.run": ("read", {"full": ("bool", False)}, "checks and readiness levels (doctor --json)"),
    "install.status": ("read", {}, "installer checkpoints, last readiness level, runs"),
    "install.plan": ("read", {"profile": ("str", False)}, "each component's state and planned action"),
    "providers.list": ("read", {"deep": ("bool", False)}, "provider descriptors (brain.local.providers)"),
    "projects.list": ("read", {}, "registered projects with task counts"),
    "conversation.send": ("read", {"message": ("str", True), "project_id": ("str", False)},
                          "reply, intent, and a proposed action to confirm (never executed here)"),
    "conversation.history": ("read", {"project_id": ("str", False), "limit": ("int", False)}, "recent turns"),
    "tasks.list": ("read", {"project_id": ("str", True)}, "the project's tasks (summaries)"),
    "tasks.get": ("read", {"project_id": ("str", True), "task_id": ("str", True)}, "one task (summary)"),
    "tasks.events": ("read", {"project_id": ("str", True), "task_id": ("str", True), "after_seq": ("int", False)},
                     "journal events after a sequence number"),
    "projects.register": ("action", {"path": ("str", True)}, "registers an existing Git project (read-only to it)"),
    "projects.create": ("action", {"goal": ("str", True), "name": ("str", False), "root": ("str", False),
                                   "stack": ("str", False), "git_name": ("str", False), "git_email": ("str", False)},
                        "creates a new project (folder, Git baseline, scaffold); does not start building"),
    "tasks.start": ("action", {"project_id": ("str", True), "goal": ("str", True), "new_project": ("bool", False)},
                    "plans the goal; returns the proposal and its digest (nothing is applied)"),
    "tasks.approve": ("action", {"project_id": ("str", True), "task_id": ("str", True), "digest": ("str", True),
                                 "decision": ("str", True)},
                      "approve: review, apply in the isolated worktree and test; decline: cancel"),
    "tasks.accept": ("action", {"project_id": ("str", True), "task_id": ("str", True)},
                     "accepts a tested task as a new branch"),
    "tasks.tool_decision": ("action", {"project_id": ("str", True), "task_id": ("str", True),
                                       "request_id": ("str", True), "decision": ("str", True)},
                            "approve or decline the protected tool request a task is waiting on "
                            "(pending_tool_approval); the task then continues or stops"),
    "tasks.stop": ("action", {"project_id": ("str", True), "task_id": ("str", True)}, "requests cancellation"),
    "tasks.resume": ("action", {"project_id": ("str", True), "task_id": ("str", True)}, "continues an unfinished task"),
}
LONG = {"tasks.start", "tasks.approve", "tasks.accept", "tasks.resume", "tasks.tool_decision"}
CHECK_TYPES = {"str": str, "bool": bool, "int": int}


def describe() -> dict:
    from .cli import version
    return {"api_version": API_VERSION, "engine_version": version(), "transport": "json-lines over stdio",
            "operations": {name: {"kind": kind, "params": {key: {"type": kind_, "required": required}
                                                           for key, (kind_, required) in params.items()},
                                  "returns": returns}
                           for name, (kind, params, returns) in OPERATIONS.items()}}


def interrupted(task: dict) -> bool:
    """An active task whose engine process on this computer is gone. Reads never start the
    service, so they report this instead of a stale "testing"; the next action marks the task
    blocked (retry required). It is never reported as passed or restarted on its own."""
    import os
    import socket
    from ..service import Brain
    from .session import pid_alive
    owner = task.get("owner") or {}
    if task.get("status") not in Brain.ACTIVE or owner.get("host") != socket.gethostname() or not owner.get("pid"):
        return False
    return owner["pid"] != os.getpid() and not pid_alive(int(owner["pid"]))


def task_summary(task: dict) -> dict:
    proposal = task.get("proposal") or {}
    evidence = task.get("test_evidence") or {}
    return {"id": task["id"], "status": task.get("status"), "goal": task.get("goal", "")[:2000],
            "digest": task.get("digest") if task.get("status") == "proposed" else None,
            "plan": str(proposal.get("plan", ""))[:4000], "author": task.get("proposal_author"),
            "files": [change.get("path") for change in proposal.get("changes", [])][:200],
            "diff": str(task.get("diff") or "")[:100000],
            "tests": {"passed": evidence.get("passed"), "exit_code": evidence.get("exit_code"),
                      "output_tail": str(evidence.get("output") or "")[-4000:]} if evidence else None,
            "requirements": (task.get("completion") or {}).get("status"),
            "failures": [{key: item.get(key) for key in ("attempt", "category", "diagnosis", "summary")}
                         for item in task.get("failure_log", [])][-6:],
            "branch": task.get("branch"), "commit": task.get("commit"),
            "pending_tool_approval": task.get("pending_approval_id"), "interrupted": interrupted(task)}


class Engine:
    def __init__(self, layout: Layout | None = None, cwd: Path | None = None, system=None):
        self.layout = (layout or Layout.default()).ensure()
        self.cwd = Path(cwd or Path.cwd())
        self.system = system
        self.contexts = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="coding-brain-engine")
        self.thread.start()
        self.lock = threading.Lock()
        self._assistant = None

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    # dispatch ------------------------------------------------------------------------------------------
    def call(self, op: str, params: dict | None = None, emit=None):
        if op not in OPERATIONS:
            raise ApiError("unknown_op", f"unknown operation {op!r}; see engine.describe")
        params = dict(params or {})
        _, spec, _ = OPERATIONS[op]
        for key, (kind, required) in spec.items():
            if required and key not in params:
                raise ApiError("bad_params", f"{op} needs '{key}'")
            if key in params and params[key] is not None and not isinstance(params[key], CHECK_TYPES[kind]):
                raise ApiError("bad_params", f"{op}: '{key}' must be {kind}")
        unknown = set(params) - set(spec)
        if unknown:
            raise ApiError("bad_params", f"{op}: unknown parameter(s) {', '.join(sorted(unknown))}")
        handler = getattr(self, "op_" + op.replace(".", "_"))
        try:
            return handler(emit=emit, **params) if op in LONG else handler(**params)
        except ApiError:
            raise
        except SystemExit as error:
            raise ApiError("refused", str(error)) from error
        except (ValueError, KeyError) as error:
            raise ApiError("refused", str(error)) from error

    def run(self, coroutine, timeout: float = 7200):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def context(self, project_id: str):
        if project_id not in self.contexts:
            from .assistant import registry
            from .cli import Context
            match = next((item for item in registry(self.layout) if item["id"] == project_id), None)
            if not match or not match["exists"]:
                raise ApiError("not_found", f"no registered project {project_id!r}")
            self.contexts[project_id] = Context(self.layout, Path(match["root"]))
        return self.contexts[project_id]

    def brain(self, project_id: str):
        context = self.context(project_id)
        if context._brain is None:
            # The service's asyncio objects must belong to the engine loop.
            async def build():
                return context.brain
            self.run(build())
        return context._brain

    # reads ---------------------------------------------------------------------------------------------
    def op_engine_info(self):
        from .cli import version
        return {"engine_version": version(), "api_version": API_VERSION, "operations": sorted(OPERATIONS),
                "home": str(self.layout.home), "python": sys.version.split()[0]}

    def op_engine_describe(self):
        return describe()

    def op_doctor_run(self, full: bool = False):
        from .cli import doctor_report
        return doctor_report(self.layout, offline=False, full=full)

    def op_install_status(self):
        from .installer import InstallState
        state = InstallState(self.layout)
        return {"profile": state.data.get("profile"), "steps": state.data.get("steps", {}),
                "declined": state.data.get("declined", []), "last": state.data.get("last"),
                "runs": state.data.get("runs", [])[-5:], "pending_restart": state.evidence.get("pending_restart")}

    def op_install_plan(self, profile: str | None = None):
        from .installer import Installer
        installer = Installer(self.layout, system=self.system, mode="json", interactive=False)
        return installer.plan(profile or installer.state.data.get("profile") or "local")

    def op_providers_list(self, deep: bool = False):
        from .components import Env
        from .installer import InstallState
        from .providers import descriptors
        from .system import System
        env = Env(self.system or System(), self.layout, settings.load(self.layout),
                  evidence=InstallState(self.layout).evidence)
        return descriptors(env, deep=deep)

    def op_projects_list(self):
        from .assistant import registry
        return registry(self.layout)

    def assistant(self):
        if self._assistant is None:
            from .assistant import Assistant
            self._assistant = Assistant(self.layout, self.cwd, out=lambda text="": None,
                                        read=lambda prompt="": "", interactive=False)
        return self._assistant

    def op_conversation_send(self, message: str, project_id: str | None = None):
        with self.lock:  # one conversation turn at a time keeps the context ordered
            return self.assistant().respond(message, project_id)

    def op_conversation_history(self, project_id: str | None = None, limit: int = 50):
        from .assistant import ConversationLog
        return ConversationLog(self.layout).recent(project_id, max(1, min(limit, 500)))

    def op_tasks_list(self, project_id: str):
        return [task_summary(task) for task in self.context(project_id).tasks()]

    def op_tasks_get(self, project_id: str, task_id: str):
        context = self.context(project_id)
        matches = [task for task in context.tasks() if task["id"].startswith(task_id)]
        if len(matches) != 1:
            raise ApiError("not_found", f"no single task matches {task_id!r}")
        return task_summary(matches[0])

    def op_tasks_events(self, project_id: str, task_id: str, after_seq: int = 0):
        from ..telemetry import Telemetry
        context = self.context(project_id)
        telemetry = context._brain.telemetry if context._brain else Telemetry(context.data / "telemetry.sqlite3")
        return telemetry.journal([task_id], after=after_seq, limit=2000)

    # actions --------------------------------------------------------------------------------------------
    def op_projects_register(self, path: str):
        from .assistant import current_repository
        from .cli import Context
        root = current_repository(Path(path).expanduser())
        if root is None:
            raise ApiError("refused", f"{path} is not inside a Git repository")
        context = Context(self.layout, root)
        context.check()
        context.register()
        return {"id": context.project["id"], "name": context.project["name"], "root": str(root)}

    def op_projects_create(self, goal: str, name=None, root=None, stack=None, git_name=None, git_email=None):
        from .create import Blocked, create_project
        if bool(git_name) != bool(git_email):
            raise ApiError("bad_params", "git_name and git_email go together")
        if stack and stack not in {"python", "node", "web"}:
            raise ApiError("bad_params", "stack must be python, node or web")
        import io
        try:
            record = create_project(self.layout, goal, name=name, root=root, stack=stack, yes=True, interactive=False,
                                    identity={"name": git_name, "email": git_email} if git_name else None,
                                    out=io.StringIO(), system=self.system)
        except Blocked as error:
            raise ApiError("refused", str(error)) from error
        from .project import project_id
        return {**record, "project_id": project_id(Path(record["path"]))}

    def watching(self, project_id: str, emit, scope: set):
        """Stream the journal events of this operation's task while it runs: events whose task_id
        is in `scope`, or whose parent is (subtasks join the scope as they appear). Other tasks in
        the same project never reach this feed. tasks.start fills `scope` once the task exists;
        until then nothing is consumed, so no event of the task is skipped."""
        if emit is None:
            return lambda: None
        telemetry = self.brain(project_id).telemetry
        start = telemetry.last_seq()
        stop = threading.Event()

        def drain(after):
            if not scope:
                return after
            for event in telemetry.journal(None, after=after, limit=500):
                after = event["seq"]
                if event.get("task_id") in scope or event.get("parent_task_id") in scope:
                    if event.get("task_id"):
                        scope.add(event["task_id"])
                    emit(event)
            return after

        def follow():
            after = start
            while not stop.is_set():
                after = drain(after)
                stop.wait(0.5)
            while True:
                last, after = after, drain(after)
                if after == last:
                    break
        thread = threading.Thread(target=follow, daemon=True)
        thread.start()

        def done():
            stop.set()
            thread.join(timeout=5)
        return done

    async def _drain(self, brain):
        while brain.jobs:
            await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
            await asyncio.sleep(0)

    def op_tasks_start(self, project_id: str, goal: str, new_project: bool = False, emit=None):
        brain = self.brain(project_id)
        context = self.context(project_id)
        scope = set()
        done = self.watching(project_id, emit, scope)

        async def go():
            task = brain.submit(context.root.name, goal, launch=False)
            scope.add(task["id"])
            if new_project:
                task["tests_expected"] = True
            try:
                task = await brain.create(task)
                await self._drain(brain)
            except asyncio.CancelledError:  # tasks.stop: the task stopped at a safe boundary
                pass
            return brain.store.get(task["id"])
        try:
            return task_summary(self.run(go()))
        finally:
            done()

    def op_tasks_approve(self, project_id: str, task_id: str, digest: str, decision: str, emit=None):
        if decision not in {"approve", "decline"}:
            raise ApiError("bad_params", "decision must be approve or decline")
        brain = self.brain(project_id)
        task = brain.store.get(task_id)
        if task["status"] != "proposed":
            raise ApiError("refused", f"task is {task['status']}, not waiting for approval")
        if digest != task.get("digest"):
            raise ApiError("refused", "the digest does not match the current proposal; fetch the task again")
        if decision == "decline":
            return self.op_tasks_stop(project_id, task_id)
        done = self.watching(project_id, emit, {task_id})

        async def go():
            try:
                await brain.execute(task_id, digest)
                await self._drain(brain)
            except asyncio.CancelledError:  # tasks.stop: the task stopped at a safe boundary
                pass
            return brain.store.get(task_id)
        try:
            return task_summary(self.run(go()))
        finally:
            done()

    def op_tasks_accept(self, project_id: str, task_id: str, emit=None):
        from .cli import branch_for, slug
        brain = self.brain(project_id)
        context = self.context(project_id)

        async def go():
            task = await brain.accept(task_id, f"Accepted through the engine API: {brain.store.get(task_id)['goal'][:200]}")
            if task.get("commit") and task["status"] == "accepted" and not task.get("parent_id"):
                task["branch"] = branch_for(context, task["commit"], f"{slug(task['goal'])}-{task['id'][:6]}")
                brain.store.save(task)
            return task
        return task_summary(self.run(go()))

    def op_tasks_tool_decision(self, project_id: str, task_id: str, request_id: str, decision: str, emit=None):
        """A protected tool request is single-use and bound to its task: the service refuses a
        request id that is not the one the task is waiting on."""
        if decision not in {"approve", "decline"}:
            raise ApiError("bad_params", "decision must be approve or decline")
        brain = self.brain(project_id)
        task = brain.store.get(task_id)
        if task["status"] != "awaiting_tool_approval" or task.get("pending_approval_id") != request_id:
            raise ApiError("refused", f"task is {task['status']}, not waiting for this tool request")
        done = self.watching(project_id, emit, {task_id})

        async def go():
            try:
                brain.decide_tool_approval(request_id, decision == "approve")
                await self._drain(brain)
            except asyncio.CancelledError:  # tasks.stop: the task stopped at a safe boundary
                pass
            return brain.store.get(task_id)
        try:
            return task_summary(self.run(go()))
        finally:
            done()

    def op_tasks_stop(self, project_id: str, task_id: str):
        """Request cancellation; a running task stops at its next safe boundary (the sandbox polls
        for it). Runs on the engine loop, so it is ordered with the task's own state changes, and
        returns at once: it never waits for the running operation."""
        brain = self.brain(project_id)

        async def go():
            brain.cancel(task_id)
            return brain.store.get(task_id)
        return task_summary(self.run(go(), timeout=60))

    def op_tasks_resume(self, project_id: str, task_id: str, emit=None):
        brain = self.brain(project_id)
        done = self.watching(project_id, emit, {task_id})

        async def go():
            try:
                await brain.resume(task_id)
                await self._drain(brain)
            except asyncio.CancelledError:  # tasks.stop: the task stopped at a safe boundary
                pass
            return brain.store.get(task_id)
        try:
            return task_summary(self.run(go()))
        finally:
            done()


def isolate_stdio():
    """Keep the protocol pipes to this process. The engine starts child processes (git, the
    sandbox, model CLIs); a child must neither inherit the request pipe nor write into the response
    stream. On Windows it is worse than noise: while the reader thread waits on the stdin pipe,
    starting any child that inherits stdin (subprocess duplicates the standard handles) blocks until
    the next request line arrives, so a request that runs git would never be answered.

    Returns private (reader, writer) for the protocol; the standard handles become NUL (stdin)
    and stderr (stdout) for everything else, including stray prints."""
    import io
    import os
    sys.stdout.flush()
    reader = io.TextIOWrapper(io.FileIO(os.dup(0), "rb"), encoding="utf-8", errors="replace")
    writer = io.TextIOWrapper(io.FileIO(os.dup(1), "wb"), encoding="utf-8", newline="\n", write_through=True)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    if sys.platform == "win32":  # make sure children see the new standard handles
        import ctypes
        import msvcrt
        ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))  # STD_INPUT_HANDLE
        ctypes.windll.kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(1))  # STD_OUTPUT_HANDLE
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.stdout = sys.stderr
    return reader, writer


def serve_stdio(engine: Engine, reader=None, writer=None):
    """JSON lines on stdin/stdout. Requests run concurrently, so tasks.stop can arrive while
    tasks.approve is still testing; output lines are never interleaved."""
    if reader is None and writer is None:
        reader, writer = isolate_stdio()
    reader = reader or sys.stdin
    writer = writer or sys.stdout
    write_lock = threading.Lock()
    threads = []

    def write(payload: dict):
        with write_lock:
            writer.write(json.dumps(payload, default=str) + "\n")
            writer.flush()

    def handle(request):
        op_id = None
        try:
            if not isinstance(request, dict):
                raise ApiError("bad_request", "a request is a JSON object: {\"id\": ..., \"op\": \"name\", \"params\": {...}}")
            op_id = request.get("id")
            if isinstance(op_id, bool) or not isinstance(op_id, (str, int, type(None))):
                op_id = None
                raise ApiError("bad_request", "'id' must be a string, an integer or null")
            if not isinstance(request.get("op"), str) or not isinstance(request.get("params", {}), (dict, type(None))):
                raise ApiError("bad_request", "a request is {\"id\": ..., \"op\": \"name\", \"params\": {...}}")
            result = engine.call(request["op"], request.get("params") or {},
                                 emit=lambda event: write({"event": event, "op_id": op_id}))
            write({"id": op_id, "ok": True, "result": result})
        except ApiError as error:
            write({"id": op_id, "ok": False, "error": {"code": error.code, "message": str(error)[:2000]}})
        except Exception as error:  # an engine fault is reported, never a dead pipe or a lost response
            write({"id": op_id, "ok": False, "error": {"code": "engine_error",
                                                       "message": f"{type(error).__name__}: {str(error)[:2000]}"}})

    write({"event": {"event_type": "ready", "api_version": API_VERSION, "at": time.time()}})
    for line in reader:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            write({"id": None, "ok": False, "error": {"code": "bad_request", "message": "not JSON"}})
            continue
        thread = threading.Thread(target=handle, args=(request,), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    engine.close()
    return 0
