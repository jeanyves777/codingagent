import asyncio
import json
import pytest
from brain.service import Brain
from brain.validators import ProposalInvalid, classify_test_failure, format_diagnostics, validate_change
from tests.test_brain import create_direct, make_repository

BROKEN = '"""Small helpers.\n\ndef run():\n    return 1\n'
FIXED = '"""Small helpers."""\n\ndef run():\n    return 2\n'


class SequenceModel:
    """Returns the queued file contents for main.py, one per proposal."""
    def __init__(self, contents):
        self.contents, self.goals = list(contents), []

    async def propose(self, root, goal, memories, repository_context=None):
        self.goals.append(goal)
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return json.dumps({"plan": "p", "changes": [{"path": "main.py", "content": content}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "looks fine"}


def brain_with(tmp_path, contents, **options):
    repository = make_repository(tmp_path / "repos" / "demo")
    return Brain(repository.parent, tmp_path / "data", SequenceModel(contents), "img", **options)


def test_validators_report_compact_diagnostics_without_executing():
    marker = "/tmp/coding-brain-should-not-exist"
    hostile = f"open({marker!r}, 'w').write('ran')\n"
    assert validate_change("evil.py", hostile) == []
    import os
    assert not os.path.exists(marker)
    report = format_diagnostics(validate_change("calc.py", BROKEN))
    assert report.startswith("Validation: FAILED\nCategory: Python syntax\nFile: calc.py\nLine: 1")
    assert "Required action:" in report
    assert validate_change("a.json", '{"a": }')[0].category == "JSON syntax"
    assert validate_change("a.toml", "x = = 1")[0].category == "TOML syntax"
    assert validate_change("a.tsx", "const x = <div>;")[0].line == 1
    assert validate_change("a.js", "export const ok = () => 1;\n") == []
    assert validate_change("notes.md", "anything") == []


def test_malformed_code_gets_a_focused_correction_before_review(tmp_path):
    brain = brain_with(tmp_path, [BROKEN, FIXED])

    async def flow():
        return await create_direct(brain)
    task = asyncio.run(flow())
    assert task["status"] == "proposed"
    assert task["proposal"]["changes"][0]["content"] == FIXED
    correction = brain.model.goals[1]
    assert "Category: Python syntax" in correction and "Rejected main.py around line 1" in correction
    assert task["metrics"]["validation_failures"] == 1
    assert [event["kind"] for event in task["events"]].count("validation_failed") == 1


def test_validation_retries_are_bounded(tmp_path):
    brain = brain_with(tmp_path, [BROKEN], validation_retries=1)

    async def flow():
        await create_direct(brain)
    with pytest.raises(ProposalInvalid):
        asyncio.run(flow())
    assert len(brain.model.goals) == 2


def test_no_op_proposals_are_rejected_by_the_scope_gate(tmp_path):
    repository = make_repository(tmp_path / "repos" / "demo")
    current = (repository / "main.py").read_text()
    brain = Brain(repository.parent, tmp_path / "data", SequenceModel([current]), "img",
                  validation_retries=0)
    with pytest.raises(ProposalInvalid, match="Category: Scope"):
        asyncio.run(create_direct(brain))


def test_syntax_errors_in_repairs_never_reach_reviewer_or_tests(tmp_path, monkeypatch):
    tests = []

    def run(*args, **kwargs):
        tests.append(1)
        return {"passed": False, "exit_code": 1, "output": "FAILED test_main.py::test_ok"}
    monkeypatch.setattr("brain.service.run_tests", run)
    brain = brain_with(tmp_path, ["x = 2\n", BROKEN], validation_retries=1, max_free_attempts=2)
    reviews = []
    original_review = brain.model.review

    async def review(goal, diff):
        reviews.append(diff)
        return await original_review(goal, diff)
    brain.model.review = review

    async def flow():
        task = await create_direct(brain)
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    # One review and one test run for the valid first proposal; the broken repairs stop at validation.
    assert task["status"] == "failed" and len(tests) == 1 and len(reviews) == 1
    assert [item["category"] for item in task["failure_log"]] == ["test_failure", "validation"]


def test_test_failures_are_classified_and_compacted():
    noisy = "\n".join(["collected 5 items", *["noise"] * 200,
                       "FAILED test_a.py::test_x - assert 1 == 2", "E   assert 1 == 2",
                       "1 failed, 4 passed in 0.1s"])
    result = classify_test_failure({"exit_code": 1, "output": noisy})
    assert result["category"] == "test_failure" and "noise" not in result["summary"]
    assert "FAILED test_a.py::test_x" in result["summary"]
    assert classify_test_failure({"exit_code": 2, "output": "E   SyntaxError: x"})["category"] == "syntax"
    assert classify_test_failure({"exit_code": None, "output": "Sandbox timed out"})["category"] == "timeout"
    assert classify_test_failure({"exit_code": 5, "output": ""})["category"] == "no_tests"


def test_double_escaped_newlines_are_repaired_deterministically(tmp_path):
    from brain.validators import mechanical_repair
    escaped = '"""Helpers."""\\n\\ndef run():\\n    return 2\\n'
    brain = brain_with(tmp_path, [escaped])
    task = asyncio.run(create_direct(brain))
    assert task["status"] == "proposed" and len(brain.model.goals) == 1
    assert task["proposal"]["changes"][0]["content"] == '"""Helpers."""\n\ndef run():\n    return 2\n'
    assert task["metrics"]["mechanical_repairs"] == 1
    assert mechanical_repair("ok.py", 'x = "a\\nb"\n') is None
    assert mechanical_repair("broken.py", 'x = (\\n') is None
    assert mechanical_repair("data.json", '{"a": 1}\\n') is None
