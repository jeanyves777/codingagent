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


def test_trajectory_flags_access_outside_the_workspace(tmp_path):
    from brain.gauntlet import parse_trajectory
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": str(tmp_path / "ok.py")}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/home/user/codingagent/gauntlet/tasks/x/hidden/t.py"}}]}}),
        json.dumps({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}, "num_turns": 2})]
    events, metrics, outside = parse_trajectory("claude_code", "\n".join(lines), tmp_path)
    assert len(events) == 2 and metrics["premium_output_tokens"] == 5
    assert outside == ["Read /home/user/codingagent/gauntlet/tasks/x/hidden/t.py"]


def test_runtime_capabilities_reflect_configuration():
    from types import SimpleNamespace
    from brain.gauntlet import runtime_capabilities
    unconfigured = SimpleNamespace(web=None, knowledge=object(), supervision=None)
    available, problems = runtime_capabilities("B_coding_brain", unconfigured)
    assert "web" not in available and "knowledge" in available and not problems
    available, problems = runtime_capabilities("C_three_phase", unconfigured)
    assert "premium" not in available and problems == ["C_three_phase needs BRAIN_SUPERVISORS"]


def test_pass_requires_agent_completion_hidden_tests_and_no_violations(tmp_path, monkeypatch):
    import brain.gauntlet as gauntlet
    task = load_task(TASKS / "bug-pagination")
    harness = Gauntlet(tmp_path)
    harness.brain_capabilities["B_coding_brain"] = ({"edit"}, [])
    monkeypatch.setattr(gauntlet, "run_hidden", lambda task, final: {"passed": True, "output": ""})
    monkeypatch.setattr(gauntlet, "original_branch_violations", lambda source: [])

    def fake(status):
        async def brain_run(self, condition, task, target, name, run_id):
            return {"final": target, "status": status, "metrics": {}, "premium_calls": 0,
                    "premium_attempts": 0, "events": []}
        return brain_run
    monkeypatch.setattr(Gauntlet, "_brain_run", fake("failed"))
    record = asyncio.run(harness.run_one("B_coding_brain", task))
    assert record["hidden_tests_passed"] and not record["agent_completed"] and record["outcome"] == "failed"
    monkeypatch.setattr(Gauntlet, "_brain_run", fake("passed"))
    assert asyncio.run(harness.run_one("B_coding_brain", task))["outcome"] == "passed"


def test_isolation_masks_outermost_roots_and_is_probed(tmp_path):
    import shutil as sh
    harness = Gauntlet(tmp_path / "runs", [TASKS])
    roots = harness._hidden_roots()
    assert TASKS not in roots and TASKS.parents[1] in roots  # the task dir is hidden by the repo mount
    if not sh.which("unshare"):
        pytest.skip("unshare unavailable")
    assert harness.isolation_problems() == []


def test_failures_are_classified_by_cause():
    from brain.gauntlet import classify_failure
    base = {"outcome": "failed", "safety_violations": [], "trajectory": []}
    assert classify_failure({**base, "outcome": "passed"}) is None
    assert classify_failure({**base, "error": "ReadTimeout: "}) == "timeout"
    assert classify_failure({**base, "trajectory": [{"kind": "test_finished",
                                                     "detail": '{"passed": false, "exit_code": 125}'}]}) == "infrastructure"
    assert classify_failure({**base, "agent_status": "integration_conflict"}) == "orchestration"
    assert classify_failure({**base, "trajectory": [{"kind": "dependency_blocked", "detail": ""}]}) == "orchestration"
    assert classify_failure({**base, "safety_violations": ["canary"]}) == "safety"
    assert classify_failure({**base, "trajectory": [{"kind": "test_finished",
                                                     "detail": '{"passed": false, "exit_code": 1}'}]}) == "model"
    assert classify_failure({"outcome": "unsupported"}) == "unsupported"


def test_checkpoint_resumes_without_repeating_finished_runs(tmp_path, monkeypatch):
    tasks = [load_task(TASKS / "bug-pagination"), load_task(TASKS / "bug-duration")]
    calls = []

    async def run_one(self, condition, task):
        calls.append((condition, task["id"]))
        return {"task": task["id"], "category": task["category"], "condition": condition, "outcome": "failed",
                "safety_violations": [], "trajectory": []}
    monkeypatch.setattr(Gauntlet, "run_one", run_one)
    monkeypatch.setattr(Gauntlet, "probe", lambda self, conditions: None)
    checkpoint = tmp_path / "results.jsonl"
    checkpoint.write_text(json.dumps({"iteration": 0, "task": "bug-pagination", "condition": "A_free_alone",
                                      "category": "bug_fix", "outcome": "passed", "safety_violations": []}) + "\n")
    result = asyncio.run(Gauntlet(tmp_path / "w").run(["A_free_alone"], tasks, 1, checkpoint))
    assert calls == [("A_free_alone", "bug-duration")]
    assert len(result["runs"]) == 2 and len(checkpoint.read_text().splitlines()) == 2
