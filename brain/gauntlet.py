"""Coding Brain Gauntlet: paired, reproducible evaluation with hidden acceptance tests.

A task directory holds:
  task.json   goal, category, sandbox image, required capabilities, protected paths, canaries
  repo/       the repository the agent receives
  hidden/     acceptance tests overlaid only at evaluation time (the agent never sees them)
  reference/  files overlaid on repo/ to form a known-good solution (used to validate the task)

Conditions:
  A_free_alone    the free model with deterministic gates, knowledge, web and supervision off
  B_coding_brain  free model + Knowledge Router, code tools, deterministic gates, web verification
  C_three_phase   B + budgeted Claude/Codex supervision (BRAIN_SUPERVISORS)
  claude_code     Claude Code CLI alone in a copy of the repository (premium)
  codex           Codex CLI alone in a copy of the repository (premium)

Every run is evaluated the same way: the final files are copied to a clean directory, hidden
tests are overlaid, and they run in the offline Docker sandbox. Protected paths and canary
strings are checked for safety. A task needing a capability a condition lacks is reported as
unsupported, never counted as a pass or silently dropped.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = {"bug_fix", "feature", "live_verification", "debugging", "orchestration", "git_pr_ci",
              "security_recovery", "repository_creation"}
CONDITIONS = ("A_free_alone", "B_coding_brain", "C_three_phase", "claude_code", "codex")
DESIGNED = {  # the most each condition can offer; runtime_capabilities() checks what is configured
    "A_free_alone": {"edit"},
    "B_coding_brain": {"edit", "web", "knowledge", "orchestration", "failover"},
    "C_three_phase": {"edit", "web", "knowledge", "orchestration", "failover", "premium"},
    "claude_code": {"edit", "shell", "web", "premium"},
    "codex": {"edit", "shell", "web", "premium"},
}
COMPLETED = {"passed", "completed", "finished"}
IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc")


def load_task(directory: Path) -> dict:
    task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    required = {"id", "category", "goal"}
    if not required <= set(task) or task["category"] not in CATEGORIES:
        raise ValueError(f"{directory}: task.json needs id, goal and a known category")
    for part in ("repo", "hidden"):
        if not (directory / part).is_dir():
            raise ValueError(f"{directory}: missing {part}/")
    return {"image": "coding-brain-sandbox:0.1", "requires": [], "protected": [], "canaries": [],
            "mode": "task", "hidden_command": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            **task, "path": directory}


def tasks_in(root: Path, only: list[str] | None = None) -> list[dict]:
    found = [load_task(path) for path in sorted(root.iterdir()) if (path / "task.json").exists()]
    return [task for task in found if not only or task["id"] in only]


def digest_tree(root: Path, paths: list[str]) -> dict:
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() if (root / path).is_file() else None
            for path in paths}


def run_hidden(task: dict, final: Path) -> dict:
    """Copy final files to a clean directory, overlay hidden tests, run them offline."""
    with_tests = Path(os.environ.get("TMPDIR", "/tmp")) / f"gauntlet-eval-{uuid.uuid4().hex}"
    shutil.copytree(final, with_tests, ignore=IGNORED)
    shutil.copytree(task["path"] / "hidden", with_tests, dirs_exist_ok=True)
    os.chmod(with_tests, 0o755)
    for path in with_tests.rglob("*"):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=512m", "--cpus=1",
               "--user=65534:65534", "--tmpfs=/tmp:rw,nosuid,size=64m",
               "--mount", f"type=bind,source={with_tests},target=/code,readonly", "--workdir=/code",
               "--env=PYTHONDONTWRITEBYTECODE=1", task["image"], *task["hidden_command"]]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        passed, output = result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        passed, output = False, "hidden tests timed out"
    finally:
        shutil.rmtree(with_tests, ignore_errors=True)
    return {"passed": passed, "output": output}


def safety(task: dict, original: Path, final: Path) -> list[str]:
    violations = []
    before, after = digest_tree(original, task["protected"]), digest_tree(final, task["protected"])
    violations += [f"protected path changed: {path}" for path in task["protected"] if before[path] != after[path]]
    for path in final.rglob("*"):
        if path.is_file() and ".git" not in path.parts and path.stat().st_size < 1_000_000:
            relative = path.relative_to(final)
            text = path.read_text(encoding="utf-8", errors="replace")
            source = original / relative
            before = source.read_text(encoding="utf-8", errors="replace") if source.is_file() else ""
            # A canary counts only where the agent put it, not where the task planted it.
            violations += [f"canary {canary!r} in {relative.as_posix()}" for canary in task["canaries"]
                           if text.count(canary) > before.count(canary)]
    return violations


def validate(task: dict) -> dict:
    """A task is sound when its original repo fails the hidden tests and the reference passes."""
    original = run_hidden(task, task["path"] / "repo")
    staged = Path(os.environ.get("TMPDIR", "/tmp")) / f"gauntlet-ref-{uuid.uuid4().hex}"
    shutil.copytree(task["path"] / "repo", staged, ignore=IGNORED)
    shutil.copytree(task["path"] / "reference", staged, dirs_exist_ok=True)
    reference = run_hidden(task, staged)
    shutil.rmtree(staged, ignore_errors=True)
    visible = sorted(path.name for path in (task["path"] / "repo").rglob("test_*.py"))
    # Coding Brain fails closed when no tests run, so every task must give the agent visible tests.
    return {"task": task["id"], "original_fails": not original["passed"], "reference_passes": reference["passed"],
            "visible_tests": visible,
            "valid": not original["passed"] and reference["passed"] and bool(visible),
            "detail": None if reference["passed"] else reference["output"][-800:]}


def runtime_capabilities(condition: str, brain=None) -> tuple[set[str], list[str]]:
    """Capabilities actually available now, and problems that make a condition misconfigured."""
    available, problems = set(DESIGNED[condition]), []
    if brain is not None:
        if brain.web is None:
            available.discard("web")
        if brain.knowledge is None:
            available.discard("knowledge")
        if condition == "C_three_phase" and brain.supervision is None:
            available.discard("premium")
            problems.append("C_three_phase needs BRAIN_SUPERVISORS")
    elif condition in ("claude_code", "codex"):
        executable = "claude" if condition == "claude_code" else "codex"
        if not shutil.which(executable):
            return set(), [f"{executable} is not installed"]
        status = subprocess.run([executable, "auth", "status"] if executable == "claude"
                                else [executable, "login", "status"], capture_output=True, text=True, timeout=30)
        text = (status.stdout + status.stderr).lower()
        if status.returncode or "not logged in" in text or '"loggedin": false' in text:
            problems.append(f"{executable} is not signed in")
    return available, problems


def wilson(successes: int, total: int, z: float = 1.96) -> list[float]:
    if not total:
        return [0.0, 0.0]
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)]


class Gauntlet:
    def __init__(self, workdir: Path, task_roots: list[Path] | None = None):
        self.workdir = workdir
        self.task_roots = [path.resolve() for path in (task_roots or [])]
        self.brain_capabilities = {}
        workdir.mkdir(parents=True, exist_ok=True)

    def probe(self, conditions: list[str]):
        """Build each Coding Brain condition once from the real environment to learn what it can do."""
        from .factory import build_brain_from_env
        for condition in conditions:
            if condition in ("claude_code", "codex"):
                continue
            saved = {key: os.environ.get(key) for key in ("BRAIN_REPOSITORIES", "BRAIN_DATA")}
            probe_root = self.workdir / "probe"
            (probe_root / "repositories").mkdir(parents=True, exist_ok=True)
            os.environ["BRAIN_REPOSITORIES"] = str(probe_root / "repositories")
            os.environ["BRAIN_DATA"] = str(probe_root / "data")
            try:
                brain = build_brain_from_env()
                if condition == "A_free_alone":
                    brain.knowledge, brain.web, brain.supervision = None, None, None
                elif condition == "B_coding_brain":
                    brain.supervision = None
                self.brain_capabilities[condition] = runtime_capabilities(condition, brain)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _stage(self, task: dict, run_id: str) -> tuple[Path, str]:
        repositories = self.workdir / run_id / "repositories"
        name = task["id"].replace("/", "-")
        target = repositories / name
        shutil.copytree(task["path"] / "repo", target, ignore=IGNORED)
        for command in (["init", "-q"], ["config", "user.name", "Gauntlet"],
                        ["config", "user.email", "gauntlet@localhost"], ["add", "-A"],
                        ["commit", "-q", "-m", "task"]):
            subprocess.run(["git", "-C", str(target), *command], check=True, capture_output=True)
        return target, name

    async def _brain_run(self, condition: str, task: dict, target: Path, name: str, run_id: str) -> dict:
        from .factory import build_brain_from_env
        os.environ["BRAIN_REPOSITORIES"] = str(target.parent)
        os.environ["BRAIN_DATA"] = str(self.workdir / run_id / "data")
        brain = build_brain_from_env()
        brain.image = {"python": task["image"], "node": task["image"]}
        if condition == "A_free_alone":
            brain.knowledge, brain.web, brain.supervision, brain.gates = None, None, None, False
        elif condition == "B_coding_brain":
            brain.supervision = None
        if task["mode"] == "orchestration" and condition == "A_free_alone":
            task = {**task, "mode": "task"}  # the free model alone has no orchestration engine
        if condition != "A_free_alone" and task.get("web_allowlist") and brain.web:
            brain.web.broker.policy.allow = list(task["web_allowlist"])
        if task.get("chaos", {}).get("preferred_brain_offline"):
            from .brains import FailoverModel
            from .model import OllamaModel
            offline = OllamaModel("http://127.0.0.1:9", "offline-preferred-brain")
            brain.model.strong = FailoverModel([offline, brain.model.strong])
            brain.model.fast = FailoverModel([offline, brain.model.fast])
        if task["mode"] == "orchestration":
            return await self._orchestrate(brain, name, task["goal"])
        item = brain.submit(name, task["goal"], launch=False)
        try:
            item = await brain.create(item)
            if item["status"] == "proposed" and item["proposal"]["changes"]:
                item = await brain.execute(item["id"], item["digest"])
        except Exception as error:
            item = {**brain.store.get(item["id"]), "error": f"{type(error).__name__}: {error}"[:300]}
        stored = {**brain.store.get(item["id"]), **({"error": item["error"]} if "error" in item else {})}
        workspace = brain.workspace(item["id"])
        ledger = getattr(brain.supervision, "ledger", None)
        return {"final": workspace if workspace.exists() else target, "status": stored["status"],
                "metrics": {**stored.get("metrics", {}), "repair_attempts": len(stored.get("failure_log", []))},
                "premium_calls": ledger.count(item["id"], ok=1) if ledger else 0,
                "premium_attempts": ledger.count(item["id"]) if ledger else 0,
                "events": [{"kind": event["kind"], "detail": event["detail"][:500]} for event in stored["events"]],
                "error": stored.get("error")}

    async def _orchestrate(self, brain, name: str, goal: str) -> dict:
        """Delegate, auto-approve each proposed assignment, accept the ones whose tests pass so
        dependents start, and evaluate the integration worktree. Human approvals are simulated
        by the harness only; nothing is merged into the source branch."""
        group = brain.delegate(name, goal)
        error = None
        for _ in range(40):
            while brain.jobs:
                await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
                await asyncio.sleep(0)
            group = brain.store.get(group["id"])
            if group["status"] in {"completed", "attention_required", "integration_conflict", "cancelled",
                                   "blocked"}:
                break
            progressed = False
            for child in group.get("children", []):
                task = brain.store.get(child["id"])
                try:
                    if task["status"] == "proposed" and task["proposal"]["changes"]:
                        task = await brain.execute(task["id"], task["digest"])
                        progressed = True
                    if task["status"] == "passed":
                        await brain.accept(task["id"], "Accepted by the Gauntlet harness")
                        progressed = True
                except Exception as failure:
                    error = f"{type(failure).__name__}: {failure}"[:300]
            if not progressed and not brain.jobs:
                break
        group = brain.store.get(group["id"])
        children = [brain.store.get(child["id"]) for child in group.get("children", [])]
        metrics = {}
        for child in children:
            for key, value in child.get("metrics", {}).items():
                if isinstance(value, (int, float)):
                    metrics[key] = metrics.get(key, 0) + value
        ledger = getattr(brain.supervision, "ledger", None)
        final = Path(group["integration_workspace"]) if group.get("integration_workspace") else None
        events = [{"kind": event["kind"], "detail": event["detail"][:300]}
                  for item in [group, *children] for event in item.get("events", [])]
        return {"final": final if final and final.exists() else brain.repository(name), "status": group["status"],
                "metrics": metrics, "events": events, "error": error,
                "premium_calls": sum(ledger.count(item["id"], ok=1) for item in [group, *children]) if ledger else 0,
                "premium_attempts": sum(ledger.count(item["id"]) for item in [group, *children]) if ledger else 0}

    def _hidden_roots(self) -> list[Path]:
        """Directories an external agent must not see: Coding Brain's repository (task sources,
        hidden tests and Git history), the task directory, and every Gauntlet run."""
        roots = sorted({Path(__file__).resolve().parents[1], self.workdir.resolve(), *self.task_roots},
                       key=lambda path: len(path.parts))
        # Mask only outermost directories: a nested one is already hidden by its parent's mount.
        return [root for index, root in enumerate(roots)
                if not any(root.is_relative_to(other) for other in roots[:index])]

    def isolation_problems(self) -> list[str]:
        """Prove the mount namespace hides every root before any external agent runs."""
        masks = " && ".join(f"mount -t tmpfs -o size=1m,mode=000 none {shlex.quote(str(root))}"
                            for root in self._hidden_roots())
        checks = " && ".join(f"[ -z \"$(ls -A {shlex.quote(str(root))} 2>/dev/null)\" ]"
                             for root in self._hidden_roots())
        result = subprocess.run(["unshare", "--mount", "--propagation", "private", "sh", "-c",
                                 f"{masks} && {checks}"], capture_output=True, text=True, timeout=30)
        return [] if result.returncode == 0 else [f"isolation failed: {(result.stderr or 'roots visible')[:200]}"]

    def _external(self, condition: str, task: dict, target: Path) -> dict:
        """Run a premium CLI agent in its own mount namespace: the hidden roots are replaced by
        empty tmpfs mounts, so hidden tests are unreachable even by absolute path or Git history.
        The agent works on a copy outside those roots, which is copied back for evaluation."""
        prompt = (task["goal"] + "\n\nWork only inside the current directory. Do not modify existing tests. "
                  "Run the tests to check your work, then stop.")
        env = {key: value for key, value in os.environ.items()
               if key not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY"}}
        isolated = Path("/var/tmp") / f"gauntlet-agent-{uuid.uuid4().hex}"
        shutil.copytree(target, isolated)
        if condition == "claude_code":
            command = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                       "--permission-mode", "acceptEdits",
                       "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python -m pytest:*),Bash(pytest:*)",
                       "--no-session-persistence"]
            if os.environ.get("GAUNTLET_CLAUDE_MODEL"):
                command += ["--model", os.environ["GAUNTLET_CLAUDE_MODEL"]]
        else:
            command = ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral",
                       "--json", "-"]
        masks = " && ".join(f"mount -t tmpfs -o size=1m,mode=000 none {shlex.quote(str(root))}"
                            for root in self._hidden_roots())
        wrapper = ["unshare", "--mount", "--propagation", "private", "sh", "-c",
                   f"{masks} && cd {shlex.quote(str(isolated))} && exec \"$@\"", "agent", *command]
        try:
            result = subprocess.run(wrapper, input=prompt, env=env, capture_output=True, text=True, timeout=1800)
            output, code = result.stdout, result.returncode
            error = None if code == 0 else (result.stderr or output)[-500:]
        except subprocess.TimeoutExpired:
            output, code, error = "", None, "agent timed out"
        shutil.rmtree(target)
        shutil.copytree(isolated, target)
        shutil.rmtree(isolated, ignore_errors=True)
        events, metrics, outside = parse_trajectory(condition, output, isolated)
        return {"final": target, "status": "finished" if code == 0 else (f"exit {code}" if code is not None else "timeout"),
                "metrics": metrics, "premium_calls": 1, "premium_attempts": 1, "events": events,
                "outside_access": outside, "error": error}

    async def run_one(self, condition: str, task: dict) -> dict:
        base = {"task": task["id"], "category": task["category"], "condition": condition}
        if condition in ("claude_code", "codex"):
            available, problems = runtime_capabilities(condition)
            problems = problems or self.isolation_problems()
        else:
            available, problems = self.brain_capabilities.get(condition) or (DESIGNED[condition], [])
        if problems:
            return {**base, "outcome": "misconfigured", "problems": problems}
        missing = set(task["requires"]) - available
        if missing:
            return {**base, "outcome": "unsupported", "missing_capabilities": sorted(missing)}
        run_id = f"{condition}-{task['id']}-{uuid.uuid4().hex[:8]}"
        target, name = self._stage(task, run_id)
        started = time.monotonic()
        if condition in ("claude_code", "codex"):
            result = await asyncio.to_thread(self._external, condition, task, target)
        else:
            result = await self._brain_run(condition, task, target, name, run_id)
        wall = round(time.monotonic() - started, 1)
        hidden = await asyncio.to_thread(run_hidden, task, result["final"])
        violations = safety(task, task["path"] / "repo", result["final"])
        violations += [f"access outside the workspace: {item}" for item in result.get("outside_access", [])]
        if condition not in ("claude_code", "codex"):
            violations += original_branch_violations(target)
        metrics = result["metrics"]
        completed = result["status"] in COMPLETED
        # A pass needs all three: the agent finished its own way, hidden tests pass, no violations.
        outcome = "passed" if completed and hidden["passed"] and not violations else "failed"
        return {**base, "outcome": outcome, "agent_completed": completed,
                "hidden_tests_passed": hidden["passed"], "safety_violations": violations,
                "agent_status": result["status"], "wall_seconds": wall,
                "free_output_tokens": metrics.get("output_tokens", 0),
                "free_prompt_tokens": metrics.get("prompt_tokens", 0),
                "free_model_calls": metrics.get("calls", 0),
                "premium_calls": result["premium_calls"],
                "premium_attempts": result.get("premium_attempts", result["premium_calls"]),
                "policy_blocks": metrics.get("web_refused", 0),
                "premium_output_tokens": metrics.get("premium_output_tokens", 0),
                "test_runs": sum(1 for event in result["events"] if event["kind"] == "test_finished"),
                "validation_failures": metrics.get("validation_failures", 0),
                "repair_attempts": metrics.get("repair_attempts", 0),
                "error": result.get("error"), "hidden_output": hidden["output"][-600:],
                "trajectory": result["events"]}

    async def run(self, conditions: list[str], tasks: list[dict], repeat: int = 1,
                  checkpoint: Path | None = None) -> dict:
        """Run every (iteration, task, condition). With a checkpoint, each finished record is
        committed as it completes; on restart, finished runs are skipped, interrupted runs are
        recorded and rerun, and a checkpoint from a different configuration is refused."""
        self.probe(conditions)
        env = environment(tasks)
        fingerprint = config_fingerprint(env)
        store = Checkpoint(checkpoint, fingerprint, env) if checkpoint else None
        runs = store.completed() if store else []
        interrupted = store.interrupted() if store else []
        done = {(run["iteration"], run["task"], run["condition"]) for run in runs}
        for iteration in range(repeat):
            for task in tasks:
                for condition in conditions:
                    key = (iteration, task["id"], condition)
                    if key in done:
                        continue
                    if store:
                        store.started(key)
                    record = await self.run_one(condition, task)
                    record.update({"iteration": iteration, "fingerprint": fingerprint})
                    runs.append(record)
                    done.add(key)
                    if store:
                        store.finished(key, record)
                    print(json.dumps({key: record.get(key) for key in (
                        "condition", "task", "outcome", "wall_seconds", "free_output_tokens", "premium_calls",
                        "safety_violations")}), flush=True)
        return {"created_at": datetime.now(timezone.utc).isoformat(), "conditions": conditions,
                "tasks": [task["id"] for task in tasks], "repeat": repeat, "fingerprint": fingerprint,
                "capabilities": {condition: sorted(value[0]) for condition, value in self.brain_capabilities.items()},
                "environment": env, "interrupted_runs": interrupted,
                "summary": summarize(runs, conditions), "runs": runs}


FINGERPRINT_FIELDS = ("coding_brain_commit", "coding_brain_dirty", "settings", "models", "knowledge_sources",
                      "claude_code", "codex", "task_set_sha256")


def config_fingerprint(env: dict) -> str:
    """Everything that must match for runs to be comparable: code, settings, models, knowledge, tasks."""
    stable = {key: env.get(key) for key in FINGERPRINT_FIELDS}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]


class CheckpointMismatch(RuntimeError):
    pass


class Checkpoint:
    """Append-only JSONL with a configuration header and per-line checksums.

    Line kinds: header (fingerprint + environment), started (run key), finished (run key + record).
    Each line carries the SHA-256 of its body and is flushed and fsynced before the next run starts,
    so a torn or partial line fails verification and is ignored instead of counting as a result.
    A run that has a started line but no finished line was interrupted: it is reported and rerun."""

    def __init__(self, path: Path, fingerprint: str, env: dict):
        self.path, self.fingerprint = path, fingerprint
        self.entries = self._read() if path.exists() else []
        headers = [entry for entry in self.entries if entry["kind"] == "header"]
        if headers and headers[0]["fingerprint"] != fingerprint:
            raise CheckpointMismatch(
                f"{path} was written by configuration {headers[0]['fingerprint']}, this run is {fingerprint}; "
                "the model, settings, task set, knowledge, or code changed. Use a new --output.")
        if not headers:
            self._append({"kind": "header", "fingerprint": fingerprint, "environment": env})

    def _read(self) -> list[dict]:
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                wrapper = json.loads(line)
                body = wrapper["body"]
                if hashlib.sha256(body.encode()).hexdigest() != wrapper["sha256"]:
                    continue
                entries.append(json.loads(body))
            except (ValueError, KeyError, TypeError):
                continue  # torn or foreign line: never a result
        return entries

    def _append(self, entry: dict):
        body = json.dumps(entry, default=str, sort_keys=True)
        line = json.dumps({"sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body}) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            if handle.tell() and not self.path.read_bytes().endswith(b"\n"):
                handle.write("\n")  # isolate a torn previous line
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.entries.append(entry)

    def started(self, key: tuple):
        self._append({"kind": "started", "key": list(key), "at": time.time()})

    def finished(self, key: tuple, record: dict):
        self._append({"kind": "finished", "key": list(key), "record": record})

    def completed(self) -> list[dict]:
        seen, runs = set(), []
        for entry in self.entries:
            key = tuple(entry.get("key", []))
            if entry["kind"] == "finished" and key not in seen:  # first finish wins; never duplicated
                seen.add(key)
                runs.append(entry["record"])
        return runs

    def interrupted(self) -> list[dict]:
        finished = {tuple(entry["key"]) for entry in self.entries if entry["kind"] == "finished"}
        return [{"iteration": entry["key"][0], "task": entry["key"][1], "condition": entry["key"][2],
                 "started_at": entry["at"], "status": "interrupted"}
                for entry in self.entries if entry["kind"] == "started" and tuple(entry["key"]) not in finished]


TIMEOUT_MARKERS = ("ReadTimeout", "TimeoutError", "timed out", "Timeout", "budget exhausted before completion")
INFRASTRUCTURE_MARKERS = ("ConnectError", "No sandbox image", "docker", "unavailable", "not installed",
                          "No brain is reachable", "exit code 125", "exit code 126", "exit code 127")
ORCHESTRATION_EVENTS = {"integration_conflict", "dependency_blocked", "queue_failed", "interrupted"}


def classify_failure(run: dict) -> str | None:
    """Why a run did not pass: safety, timeout, infrastructure, orchestration, or model.

    Slow hardware and timeouts are kept apart from reasoning failures, and harness or sandbox
    problems are not charged to the model."""
    if run.get("outcome") == "passed":
        return None
    if run.get("outcome") in {"unsupported", "misconfigured"}:
        return run["outcome"]
    if run.get("safety_violations"):
        return "safety"
    events = run.get("trajectory") or []
    text = " ".join([str(run.get("error") or ""), str(run.get("agent_status") or ""),
                     str(run.get("hidden_output") or "")[-300:]] +
                    [event.get("detail", "") for event in events if event.get("kind") in
                     {"blocked", "test_finished", "supervisor_unavailable"}])
    kinds = {event.get("kind") for event in events}
    test_infrastructure = any(event.get("kind") == "test_finished" and
                              ('"exit_code": null' in event.get("detail", "") or
                               any(f'"exit_code": {code}' in event.get("detail", "") for code in (125, 126, 127)))
                              for event in events)
    if any(event.get("kind") == "test_finished" and '"exit_code": 5' in event.get("detail", "")
           for event in events):
        return "task_design"  # no tests ran: Coding Brain fails closed; not a model failure
    if any(marker in text for marker in TIMEOUT_MARKERS) or run.get("agent_status") == "timeout":
        return "timeout"
    if test_infrastructure or any(marker in text for marker in INFRASTRUCTURE_MARKERS):
        return "infrastructure"
    if kinds & ORCHESTRATION_EVENTS or run.get("agent_status") in {"attention_required", "integration_conflict"}:
        return "orchestration"
    return "model"


def summarize(runs: list[dict], conditions: list[str]) -> dict:
    summary = {}
    for condition in conditions:
        chosen = [run for run in runs if run["condition"] == condition]
        attempted = [run for run in chosen if run["outcome"] not in {"unsupported", "misconfigured"}]
        passed = [run for run in attempted if run["outcome"] == "passed"]
        premium_passes = [run for run in passed if run.get("premium_calls")]
        summary[condition] = {
            "attempted": len(attempted),
            "unsupported": sum(run["outcome"] == "unsupported" for run in chosen),
            "misconfigured": sum(run["outcome"] == "misconfigured" for run in chosen),
            "passed": len(passed), "pass_rate": round(len(passed) / len(attempted), 3) if attempted else None,
            "pass_rate_95ci": wilson(len(passed), len(attempted)),
            "premium_dependence_rate": round(len(premium_passes) / len(passed), 3) if passed else None,
            "premium_calls_per_success": round(sum(run.get("premium_calls", 0) for run in attempted)
                                               / len(passed), 2) if passed else None,
            "premium_attempts": sum(run.get("premium_attempts", 0) for run in attempted),
            "completed_but_hidden_failed": sum(bool(run.get("agent_completed") and not run.get("hidden_tests_passed"))
                                               for run in attempted),
            "hidden_passed_but_not_completed": sum(bool(run.get("hidden_tests_passed") and not run.get("agent_completed"))
                                                   for run in attempted),
            "free_output_tokens_per_success": round(sum(run.get("free_output_tokens", 0) for run in attempted)
                                                    / len(passed)) if passed else None,
            "mean_wall_seconds": round(sum(run.get("wall_seconds", 0) for run in attempted) / len(attempted), 1)
            if attempted else None,
            "safety_violations": sum(len(run.get("safety_violations", [])) for run in attempted),
            "failure_causes": {cause: sum(classify_failure(run) == cause for run in attempted)
                               for cause in ("model", "orchestration", "infrastructure", "timeout", "safety",
                                             "task_design")},
            "pass_rate_excluding_timeouts_and_infrastructure": (
                round(len(passed) / max(1, sum(classify_failure(run) not in {"timeout", "infrastructure",
                                                                              "task_design"}
                                               for run in attempted)), 3) if attempted else None),
            "by_category": {category: f"{sum(r['outcome'] == 'passed' for r in attempted if r['category'] == category)}"
                                      f"/{sum(1 for r in attempted if r['category'] == category)}"
                            for category in sorted({r["category"] for r in attempted})}}
    pairs = {}
    for first in conditions:
        for second in conditions:
            if first < second:
                tasks = {(run["task"], run["iteration"]) for run in runs}
                outcome = {(run["condition"], run["task"], run["iteration"]): run["outcome"] for run in runs}
                both = [key for key in tasks if outcome.get((first, *key)) in {"passed", "failed"}
                        and outcome.get((second, *key)) in {"passed", "failed"}]
                pairs[f"{first} vs {second}"] = {
                    "paired_tasks": len(both),
                    "only_first_passed": sum(outcome[(first, *key)] == "passed" != outcome[(second, *key)]
                                             for key in both),
                    "only_second_passed": sum(outcome[(second, *key)] == "passed" != outcome[(first, *key)]
                                              for key in both)}
    return {"conditions": summary, "paired": pairs}


def _command(*arguments) -> str | None:
    try:
        result = subprocess.run(list(arguments), capture_output=True, text=True, timeout=30)
        return (result.stdout or result.stderr).strip()[:200] or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def markdown_report(saved: dict) -> str:
    """Human-readable pilot report: per-condition results, failure causes, per-task matrix."""
    conditions = saved["conditions"]
    summary = saved["summary"]["conditions"]
    lines = ["| Metric | " + " | ".join(conditions) + " |", "| --- |" + " --- |" * len(conditions)]
    rows = [("Passed / attempted", lambda c: f"{c['passed']}/{c['attempted']}"),
            ("Pass rate (95% CI)", lambda c: f"{c['pass_rate']} {c['pass_rate_95ci']}"),
            ("Pass rate excl. timeouts/infra", lambda c: str(c["pass_rate_excluding_timeouts_and_infrastructure"])),
            ("Unsupported / misconfigured", lambda c: f"{c['unsupported']} / {c['misconfigured']}"),
            ("Failures: model", lambda c: str(c["failure_causes"]["model"])),
            ("Failures: orchestration", lambda c: str(c["failure_causes"]["orchestration"])),
            ("Failures: infrastructure", lambda c: str(c["failure_causes"]["infrastructure"])),
            ("Failures: timeout", lambda c: str(c["failure_causes"]["timeout"])),
            ("Failures: safety", lambda c: str(c["failure_causes"]["safety"])),
            ("Failures: task design", lambda c: str(c["failure_causes"].get("task_design", 0))),
            ("Premium attempts", lambda c: str(c["premium_attempts"])),
            ("Premium dependence rate", lambda c: str(c["premium_dependence_rate"])),
            ("Free output tokens / success", lambda c: str(c["free_output_tokens_per_success"])),
            ("Mean wall seconds", lambda c: str(c["mean_wall_seconds"]))]
    for label, value in rows:
        lines.append(f"| {label} | " + " | ".join(value(summary[condition]) for condition in conditions) + " |")
    tasks = sorted({run["task"] for run in saved["runs"]})
    matrix = ["", "| Task | " + " | ".join(conditions) + " |", "| --- |" + " --- |" * len(conditions)]
    for task in tasks:
        cells = []
        for condition in conditions:
            found = [run for run in saved["runs"] if run["task"] == task and run["condition"] == condition]
            cells.append(", ".join(run["outcome"] + (f" ({run['failure_cause']})" if run.get("failure_cause")
                                                      and run["outcome"] == "failed" else "") for run in found))
        matrix.append(f"| {task} | " + " | ".join(cells) + " |")
    paired = ["", "| Pair | Matched tasks | Only first passed | Only second passed |", "| --- | --- | --- | --- |"]
    paired += [f"| {name} | {value['paired_tasks']} | {value['only_first_passed']} | {value['only_second_passed']} |"
               for name, value in saved["summary"]["paired"].items()]
    return "\n".join(lines + matrix + paired)


def environment(tasks: list[dict] | None = None) -> dict:
    """Everything needed to reproduce or audit a run."""
    import platform
    root = Path(__file__).resolve().parents[1]
    settings = {key: value for key, value in os.environ.items()
                if key.startswith(("BRAIN_", "GAUNTLET_")) and "TOKEN" not in key and "KEY" not in key}
    models = {}
    url = os.environ.get("BRAIN_MODEL_URL", "http://localhost:11434")
    for name in {os.environ.get("BRAIN_MODEL"), os.environ.get("BRAIN_FAST_MODEL")} - {None}:
        try:
            import httpx
            shown = httpx.post(url.rstrip("/") + "/api/show", json={"model": name}, timeout=10, trust_env=False).json()
            models[name] = {"digest_of_details": hashlib.sha256(json.dumps(shown.get("details", {}),
                                                                           sort_keys=True).encode()).hexdigest()[:16],
                            "details": shown.get("details"), "parameters": shown.get("parameters")}
        except Exception as error:
            models[name] = {"error": str(error)[:100]}
    knowledge = []
    database = Path(os.environ.get("BRAIN_KNOWLEDGE_DB", Path(os.environ.get("BRAIN_DATA", "brain-data")) / "knowledge.sqlite3"))
    if database.exists():
        from .knowledge import KnowledgeLibrary
        knowledge = [{key: source[key] for key in ("name", "revision", "license", "documents")}
                     for source in KnowledgeLibrary(database).sources()]
    task_hash = hashlib.sha256()
    for task in tasks or []:
        for path in sorted(task["path"].rglob("*")):
            if path.is_file():
                task_hash.update(path.relative_to(task["path"].parent).as_posix().encode() + path.read_bytes())
    return {"python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count(),
            "coding_brain_commit": _command("git", "-C", str(root), "rev-parse", "HEAD"),
            "coding_brain_dirty": bool(_command("git", "-C", str(root), "status", "--porcelain")),
            "settings": settings, "models": models, "knowledge_sources": knowledge,
            "claude_code": _command("claude", "--version"), "codex": _command("codex", "--version"),
            "docker": _command("docker", "version", "--format", "{{.Server.Version}}"),
            "task_set_sha256": task_hash.hexdigest()}


def parse_trajectory(condition: str, output: str, workspace: Path) -> tuple[list, dict, list]:
    """Tool calls, token usage, and any file access outside the agent's workspace."""
    events, metrics, outside = [], {}, []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if condition == "claude_code":
            if item.get("type") == "assistant":
                for block in item.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        events.append({"kind": "tool_use", "detail": json.dumps(
                            {"name": block["name"], "input": block.get("input")})[:500]})
                        for key in ("file_path", "path", "notebook_path"):
                            value = (block.get("input") or {}).get(key)
                            if isinstance(value, str) and value.startswith("/") and \
                                    not Path(value).resolve().is_relative_to(workspace):
                                outside.append(f"{block['name']} {value}")
            elif item.get("type") == "result":
                usage = item.get("usage") or {}
                metrics = {"premium_input_tokens": usage.get("input_tokens", 0) +
                           usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                           "premium_output_tokens": usage.get("output_tokens", 0),
                           "turns": item.get("num_turns"), "cost_usd_equivalent": item.get("total_cost_usd")}
        else:
            events.append({"kind": item.get("type", "event"), "detail": json.dumps(item)[:500]})
    return events, metrics, outside


def original_branch_violations(source: Path) -> list[str]:
    """Coding Brain must never edit the repository it was given; its work lives in worktrees."""
    status = _command("git", "-C", str(source), "status", "--porcelain", "--untracked-files=no")
    commits = _command("git", "-C", str(source), "rev-list", "--count", "HEAD")
    problems = []
    if status:
        problems.append("original repository working tree was modified")
    if commits and commits != "1":
        problems.append("original branch gained commits")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m brain.gauntlet")
    parser.add_argument("command", choices=["validate", "run", "report"])
    parser.add_argument("results", nargs="?", help="report: a saved results JSON file")
    parser.add_argument("--tasks", default="gauntlet/tasks")
    parser.add_argument("--only", action="append", help="task id (repeatable)")
    parser.add_argument("--condition", action="append", choices=CONDITIONS)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--workdir", default="gauntlet-runs")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "report":
        saved = json.loads(Path(args.results).read_text(encoding="utf-8"))
        for run in saved["runs"]:
            run["failure_cause"] = classify_failure(run)
        saved["summary"] = summarize(saved["runs"], saved["conditions"])
        text = json.dumps(saved, indent=2, default=str)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(markdown_report(saved))
        return
    tasks = tasks_in(Path(args.tasks), args.only)
    if args.command == "validate":
        result = [validate(task) for task in tasks]
    else:
        conditions = args.condition or ["A_free_alone", "B_coding_brain"]
        checkpoint = Path(args.output).with_suffix(".jsonl") if args.output else None
        result = asyncio.run(Gauntlet(Path(args.workdir).resolve(), [Path(args.tasks)]).run(
            conditions, tasks, args.repeat, checkpoint))
    text = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text if args.command == "validate" else json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
