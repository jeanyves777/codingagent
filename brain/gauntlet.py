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
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = {"bug_fix", "feature", "live_verification", "debugging", "orchestration", "git_pr_ci",
              "security_recovery", "repository_creation"}
CONDITIONS = ("A_free_alone", "B_coding_brain", "C_three_phase", "claude_code", "codex")
CAPABILITIES = {  # what each condition can do; tasks list what they require
    "A_free_alone": {"edit"},
    "B_coding_brain": {"edit", "web", "orchestration", "failover"},
    "C_three_phase": {"edit", "web", "orchestration", "failover", "premium"},
    "claude_code": {"edit", "shell", "web", "premium"},
    "codex": {"edit", "shell", "web", "premium"},
}
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
    return {"task": task["id"], "original_fails": not original["passed"], "reference_passes": reference["passed"],
            "valid": not original["passed"] and reference["passed"],
            "detail": None if reference["passed"] else reference["output"][-800:]}


def wilson(successes: int, total: int, z: float = 1.96) -> list[float]:
    if not total:
        return [0.0, 0.0]
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)]


class Gauntlet:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)

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
                "metrics": stored.get("metrics", {}),
                "premium_calls": ledger.count(item["id"], ok=1) if ledger else 0,
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
                "premium_calls": sum(ledger.count(item["id"], ok=1) for item in [group, *children]) if ledger else 0}

    def _external(self, condition: str, task: dict, target: Path) -> dict:
        prompt = (task["goal"] + "\n\nWork only inside the current directory. Do not modify existing tests. "
                  "Run the tests to check your work, then stop.")
        env = {key: value for key, value in os.environ.items()
               if key not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"}}
        if condition == "claude_code":
            command = ["claude", "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
                       "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python -m pytest:*),Bash(pytest:*)",
                       "--no-session-persistence"]
            if os.environ.get("GAUNTLET_CLAUDE_MODEL"):
                command += ["--model", os.environ["GAUNTLET_CLAUDE_MODEL"]]
        else:
            command = ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral",
                       "--json", "-"]
        try:
            result = subprocess.run(command, input=prompt, cwd=target, env=env, capture_output=True,
                                    text=True, timeout=1800)
            output, code = result.stdout, result.returncode
        except FileNotFoundError:
            return {"final": target, "status": "unavailable", "metrics": {}, "premium_calls": 0, "events": [],
                    "error": f"{command[0]} is not installed"}
        except subprocess.TimeoutExpired:
            return {"final": target, "status": "timeout", "metrics": {}, "premium_calls": 1, "events": [],
                    "error": "agent timed out"}
        metrics = {}
        if condition == "claude_code":
            try:
                data = json.loads(output)
                usage = data.get("usage") or {}
                metrics = {"premium_input_tokens": usage.get("input_tokens", 0) +
                           usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                           "premium_output_tokens": usage.get("output_tokens", 0),
                           "turns": data.get("num_turns"), "cost_usd_equivalent": data.get("total_cost_usd")}
            except ValueError:
                pass
        return {"final": target, "status": "finished" if code == 0 else f"exit {code}", "metrics": metrics,
                "premium_calls": 1, "events": [], "error": None if code == 0 else output[-500:]}

    async def run_one(self, condition: str, task: dict) -> dict:
        missing = set(task["requires"]) - CAPABILITIES[condition]
        base = {"task": task["id"], "category": task["category"], "condition": condition}
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
        metrics = result["metrics"]
        return {**base, "outcome": "passed" if hidden["passed"] and not violations else "failed",
                "hidden_tests_passed": hidden["passed"], "safety_violations": violations,
                "agent_status": result["status"], "wall_seconds": wall,
                "free_output_tokens": metrics.get("output_tokens", 0),
                "free_prompt_tokens": metrics.get("prompt_tokens", 0),
                "free_model_calls": metrics.get("calls", 0),
                "premium_calls": result["premium_calls"],
                "premium_output_tokens": metrics.get("premium_output_tokens", 0),
                "test_runs": sum(1 for event in result["events"] if event["kind"] == "test_finished"),
                "validation_failures": metrics.get("validation_failures", 0),
                "error": result.get("error"), "hidden_output": hidden["output"][-600:],
                "trajectory": result["events"]}

    async def run(self, conditions: list[str], tasks: list[dict], repeat: int = 1) -> dict:
        runs = []
        for iteration in range(repeat):
            for task in tasks:
                for condition in conditions:
                    record = await self.run_one(condition, task)
                    record["iteration"] = iteration
                    runs.append(record)
                    print(json.dumps({key: record.get(key) for key in (
                        "condition", "task", "outcome", "wall_seconds", "free_output_tokens", "premium_calls",
                        "safety_violations")}), flush=True)
        return {"created_at": datetime.now(timezone.utc).isoformat(), "conditions": conditions,
                "tasks": [task["id"] for task in tasks], "repeat": repeat,
                "environment": environment(), "summary": summarize(runs, conditions), "runs": runs}


def summarize(runs: list[dict], conditions: list[str]) -> dict:
    summary = {}
    for condition in conditions:
        chosen = [run for run in runs if run["condition"] == condition]
        attempted = [run for run in chosen if run["outcome"] != "unsupported"]
        passed = [run for run in attempted if run["outcome"] == "passed"]
        premium_passes = [run for run in passed if run.get("premium_calls")]
        summary[condition] = {
            "attempted": len(attempted), "unsupported": len(chosen) - len(attempted),
            "passed": len(passed), "pass_rate": round(len(passed) / len(attempted), 3) if attempted else None,
            "pass_rate_95ci": wilson(len(passed), len(attempted)),
            "premium_dependence_rate": round(len(premium_passes) / len(passed), 3) if passed else None,
            "premium_calls_per_success": round(sum(run.get("premium_calls", 0) for run in attempted)
                                               / len(passed), 2) if passed else None,
            "free_output_tokens_per_success": round(sum(run.get("free_output_tokens", 0) for run in attempted)
                                                    / len(passed)) if passed else None,
            "mean_wall_seconds": round(sum(run.get("wall_seconds", 0) for run in attempted) / len(attempted), 1)
            if attempted else None,
            "safety_violations": sum(len(run.get("safety_violations", [])) for run in attempted),
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


def environment() -> dict:
    import platform
    return {"python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count(),
            "model": os.environ.get("BRAIN_MODEL"), "provider": os.environ.get("BRAIN_PROVIDER", "ollama"),
            "supervisors": os.environ.get("BRAIN_SUPERVISORS"), "coding_brain": "0.8.0"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m brain.gauntlet")
    parser.add_argument("command", choices=["validate", "run"])
    parser.add_argument("--tasks", default="gauntlet/tasks")
    parser.add_argument("--only", action="append", help="task id (repeatable)")
    parser.add_argument("--condition", action="append", choices=CONDITIONS)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--workdir", default="gauntlet-runs")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    tasks = tasks_in(Path(args.tasks), args.only)
    if args.command == "validate":
        result = [validate(task) for task in tasks]
    else:
        conditions = args.condition or ["A_free_alone", "B_coding_brain"]
        result = asyncio.run(Gauntlet(Path(args.workdir).resolve()).run(conditions, tasks, args.repeat))
    text = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text if args.command == "validate" else json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
