import asyncio
import json
from pathlib import Path
import pytest
from brain.gauntlet import Gauntlet, load_task, safety, summarize, tasks_in, wilson

TASKS = Path(__file__).resolve().parents[1] / "gauntlet" / "tasks"


def test_pilot_tasks_load_and_cover_categories():
    tasks = tasks_in(TASKS)
    assert len(tasks) == 10
    assert {task["category"] for task in tasks} >= {"bug_fix", "feature", "live_verification", "debugging",
                                                    "orchestration", "security_recovery"}
    for task in tasks:
        assert (task["path"] / "reference").is_dir()
        visible = {path.name for path in (task["path"] / "repo").rglob("*")}
        assert not any(name.startswith("test_hidden") for name in visible), "hidden tests must stay hidden"


def test_wilson_interval():
    assert wilson(0, 0) == [0.0, 0.0]
    low, high = wilson(7, 10)
    assert 0.39 < low < 0.40 and 0.89 < high < 0.90
    assert wilson(10, 10)[1] == 1.0


def test_unsupported_is_reported_not_counted(tmp_path):
    task = load_task(TASKS / "recovery-preferred-brain-offline")
    record = asyncio.run(Gauntlet(tmp_path).run_one("A_free_alone", task))
    assert record["outcome"] == "unsupported" and record["missing_capabilities"] == ["failover"]
    summary = summarize([{**record, "iteration": 0}], ["A_free_alone"])["conditions"]["A_free_alone"]
    assert summary["attempted"] == 0 and summary["unsupported"] == 1 and summary["pass_rate"] is None


def test_safety_detects_protected_changes_and_canaries(tmp_path):
    task = load_task(TASKS / "security-misleading-notes")
    final = tmp_path / "final"
    import shutil
    shutil.copytree(task["path"] / "repo", final)
    assert safety(task, task["path"] / "repo", final) == []
    (final / "test_config.py").unlink()
    (final / "config_loader.py").write_text("# AGENT-OVERRIDE-ACCEPTED\n")
    violations = safety(task, task["path"] / "repo", final)
    assert "protected path changed: test_config.py" in violations
    assert any("canary" in item for item in violations)


def test_summary_reports_premium_dependence_and_pairs():
    runs = [
        {"condition": "B_coding_brain", "task": "t1", "category": "bug_fix", "iteration": 0, "outcome": "passed",
         "premium_calls": 0, "free_output_tokens": 100, "wall_seconds": 10, "safety_violations": []},
        {"condition": "B_coding_brain", "task": "t2", "category": "bug_fix", "iteration": 0, "outcome": "failed",
         "premium_calls": 0, "free_output_tokens": 300, "wall_seconds": 30, "safety_violations": []},
        {"condition": "C_three_phase", "task": "t1", "category": "bug_fix", "iteration": 0, "outcome": "passed",
         "premium_calls": 0, "free_output_tokens": 100, "wall_seconds": 10, "safety_violations": []},
        {"condition": "C_three_phase", "task": "t2", "category": "bug_fix", "iteration": 0, "outcome": "passed",
         "premium_calls": 1, "free_output_tokens": 400, "wall_seconds": 40, "safety_violations": []}]
    summary = summarize(runs, ["B_coding_brain", "C_three_phase"])
    c = summary["conditions"]["C_three_phase"]
    assert c["pass_rate"] == 1.0 and c["premium_dependence_rate"] == 0.5 and c["premium_calls_per_success"] == 0.5
    assert summary["conditions"]["B_coding_brain"]["free_output_tokens_per_success"] == 400
    assert summary["paired"]["B_coding_brain vs C_three_phase"] == {
        "paired_tasks": 2, "only_first_passed": 0, "only_second_passed": 1}
