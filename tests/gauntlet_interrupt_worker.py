"""Helper process for the forced-interruption test (not collected by pytest)."""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain.gauntlet as gauntlet  # noqa: E402
from brain.gauntlet import Gauntlet, load_task  # noqa: E402

TASKS = Path(__file__).resolve().parents[1] / "gauntlet" / "tasks"
workdir, checkpoint, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
ran = []


async def run_one(self, condition, task):
    ran.append(task["id"])
    if mode == "slow" and task["id"] != "bug-pagination":
        time.sleep(60)  # killed here
    return {"task": task["id"], "category": task["category"], "condition": condition, "outcome": "failed",
            "safety_violations": [], "trajectory": [], "wall_seconds": 0.1}

Gauntlet.run_one = run_one
Gauntlet.probe = lambda self, conditions: None
gauntlet.environment = lambda tasks=None: {"settings": "fixed"}
gauntlet.config_fingerprint = lambda env: "fixed"
tasks = [load_task(TASKS / name) for name in ("bug-pagination", "bug-duration", "bug-textstats")]
result = asyncio.run(Gauntlet(workdir / "w").run(["A_free_alone"], tasks, 1, checkpoint))
print(json.dumps({"ran": ran, "interrupted": [item["task"] for item in result["interrupted_runs"]],
                  "completed": [run["task"] for run in result["runs"]]}))
