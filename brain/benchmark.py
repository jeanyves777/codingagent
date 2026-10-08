"""Executable, isolated benchmark suites for coding-model regression testing."""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from .factory import build_brain_from_env


def validate_suite(payload: dict) -> list[dict]:
    if set(payload) - {"name", "cases"} or not isinstance(payload.get("cases"), list):
        raise ValueError("Benchmark suite requires name and cases")
    if not 1 <= len(payload["cases"]) <= 50:
        raise ValueError("Benchmark suite must contain 1–50 cases")
    cases = []
    for case in payload["cases"]:
        required = {"name", "repository", "goal", "expected_paths"}
        if not isinstance(case, dict) or not required <= set(case) or set(case) - (required | {"allowed_paths"}):
            raise ValueError("Invalid benchmark case")
        if any(not isinstance(case[key], str) or not case[key] for key in ("name", "repository", "goal")):
            raise ValueError("Benchmark case text fields are required")
        expected = case["expected_paths"]
        allowed = case.get("allowed_paths", expected)
        if not isinstance(expected, list) or not isinstance(allowed, list) or not expected:
            raise ValueError("Benchmark paths must be non-empty arrays")
        cases.append({**case, "allowed_paths": allowed})
    return cases


class BenchmarkRunner:
    def __init__(self, brain):
        self.brain = brain

    async def case(self, definition: dict, execute=False) -> dict:
        task = self.brain.submit(definition["repository"], definition["goal"], launch=False)
        task = await self.brain.create(task)
        proposed = {change["path"] for change in task["proposal"]["changes"]}
        expected, allowed = set(definition["expected_paths"]), set(definition["allowed_paths"])
        recall = len(proposed & expected) / len(expected)
        precision = len(proposed & allowed) / len(proposed) if proposed else 0.0
        metrics = {"path_recall": recall, "scope_precision": precision}
        if execute and recall == 1 and precision == 1:
            task = await self.brain.execute(task["id"], task["digest"])
            metrics["review_approved"] = 1.0 if task.get("review", {}).get("approved") else 0.0
            metrics["tests_passed"] = 1.0 if task.get("test_evidence", {}).get("passed") else 0.0
        score = sum(metrics.values()) / len(metrics)
        return {"name": definition["name"], "task_id": task["id"], "status": task["status"],
                "proposed_paths": sorted(proposed), "metrics": metrics, "score": score}

    async def replay(self, definition: dict) -> dict:
        """Run one case end to end (auto-approving the digest, never accepting) and measure it."""
        started = time.monotonic()
        task = self.brain.submit(definition["repository"], definition["goal"], launch=False)
        try:
            task = await self.brain.create(task)
            if task["status"] == "proposed" and task["proposal"]["changes"]:
                task = await self.brain.execute(task["id"], task["digest"])
        except Exception as error:  # a blocked case is a measured failure, not a crash
            task = self.brain.store.get(task["id"])
            task["benchmark_error"] = f"{type(error).__name__}: {error}"[:300]
        task = {**self.brain.store.get(task["id"]), **{key: task[key] for key in ("benchmark_error",)
                                                       if key in task}}
        metrics = task.get("metrics", {})
        kinds = [event["kind"] for event in task.get("events", [])]
        ledger = getattr(getattr(self.brain, "supervision", None), "ledger", None)
        return {"name": definition["name"], "task_id": task["id"], "status": task["status"],
                "verified_success": bool(task.get("test_evidence", {}).get("passed")),
                "wall_seconds": round(time.monotonic() - started, 1),
                "model_seconds": metrics.get("model_seconds", 0),
                "model_calls": metrics.get("calls", 0),
                "prompt_tokens": metrics.get("prompt_tokens", 0),
                "output_tokens": metrics.get("output_tokens", 0),
                "test_runs": kinds.count("test_finished"),
                "repair_attempts": len(task.get("failure_log", [])),
                "validation_failures": metrics.get("validation_failures", 0),
                "mechanical_repairs": metrics.get("mechanical_repairs", 0),
                "supervisor_calls": ledger.count(task["id"], ok=1) if ledger else 0,
                "packet_chars": metrics.get("packet_chars", 0),
                "error": task.get("benchmark_error")}

    async def compare(self, payload: dict, repeat=1) -> dict:
        """Replay identical cases with the knowledge layer off and on, same model and environment."""
        cases = validate_suite(payload)
        router, runs = self.brain.knowledge, []
        for definition in cases:
            for iteration in range(repeat):
                for variant, knowledge in (("knowledge_off", None), ("knowledge_on", router)):
                    self.brain.knowledge = knowledge
                    runs.append({"variant": variant, "iteration": iteration,
                                 **await self.replay(definition)})
        self.brain.knowledge = router
        summary = {}
        for variant in ("knowledge_off", "knowledge_on"):
            chosen = [run for run in runs if run["variant"] == variant]
            summary[variant] = {"runs": len(chosen),
                                "success_rate": sum(run["verified_success"] for run in chosen) / len(chosen),
                                **{key: round(sum(run[key] for run in chosen) / len(chosen), 1) for key in (
                                    "wall_seconds", "model_seconds", "model_calls", "output_tokens",
                                    "prompt_tokens", "test_runs", "repair_attempts", "validation_failures",
                                    "supervisor_calls")}}
        return {"name": payload["name"], "created_at": datetime.now(timezone.utc).isoformat(),
                "summary": summary, "runs": runs}

    async def suite(self, payload: dict, execute=False) -> dict:
        cases = validate_suite(payload)
        results = []
        for definition in cases:
            results.append(await self.case(definition, execute))
        return {"name": payload["name"], "executed": execute,
                "created_at": datetime.now(timezone.utc).isoformat(), "cases": results,
                "score": sum(item["score"] for item in results) / len(results)}


async def _main(args):
    suite_path = Path(args.suite).resolve()
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    brain = build_brain_from_env()
    if args.no_supervisors:
        brain.supervision = None
    runner = BenchmarkRunner(brain)
    if args.compare:
        if brain.knowledge is None:
            raise SystemExit("--compare needs the knowledge router enabled (BRAIN_KNOWLEDGE=true)")
        result = await runner.compare(payload, args.repeat)
    else:
        result = await runner.suite(payload, args.execute)
    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--compare", action="store_true",
                        help="replay every case with the knowledge layer off and on")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-supervisors", action="store_true",
                        help="measure the free models alone")
    parser.add_argument("--output")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
