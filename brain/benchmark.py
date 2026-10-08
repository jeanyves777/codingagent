"""Executable, isolated benchmark suites for coding-model regression testing."""
import argparse
import asyncio
import json
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
    result = await BenchmarkRunner(build_brain_from_env()).suite(payload, args.execute)
    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
