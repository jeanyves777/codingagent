"""Regression tests for the six corrections made after the Round One pilot."""
import asyncio
import json
import pytest
from pathlib import Path
from brain.completion import accept_requirement_tests, requirement_file_name
from brain.contracts import Proposal
from brain.service import Brain
from brain.supervision import SupervisionPolicy, SupervisorLedger
from brain.validators import ProposalInvalid, static_issues
from tests.test_brain import create_direct, make_repository
from tests.test_phase7 import DIAGNOSIS, FakeSupervisor

REQUIREMENTS = "def test_goal_says_x_is_two():\n    import main\n    assert main.x == 2\n"


class ScriptedModel:
    """Returns requirement checks when asked for them, else the queued main.py contents."""
    def __init__(self, contents, checks=REQUIREMENTS):
        self.contents, self.checks, self.goals = list(contents), checks, []

    async def propose(self, root, goal, memories, repository_context=None, **kwargs):
        self.goals.append(goal)
        if goal.startswith("Do not implement anything"):
            name = goal.split("named ")[1].split(".py")[0] + ".py"
            return json.dumps({"plan": "checks", "changes": [{"path": name, "content": self.checks}]})
        for line in goal.splitlines():
            if line.startswith("Supervisor") or "Use text.split()" in line:
                return json.dumps({"plan": "p", "changes": [{"path": "main.py", "content": "x = 2\n"}]})
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return json.dumps({"plan": "p", "changes": [{"path": "main.py", "content": content}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}


def sandbox(workspace, image, **kwargs):
    """Visible tests pass whenever main.py parses; the requirement check needs x == 2."""
    namespace = {}
    exec((workspace / "main.py").read_text(), namespace)
    checks = list(workspace.glob("test_requirements_*.py"))
    if checks and "assert main.x == 2" in checks[0].read_text() and namespace.get("x") != 2:
        return {"passed": False, "exit_code": 1, "output": "FAILED test_goal_says_x_is_two\nE assert 3 == 2"}
    if checks and "def broken(" in checks[0].read_text():
        return {"passed": False, "exit_code": 2, "output": "ERROR collecting test_requirements"}
    return {"passed": True, "exit_code": 0, "output": "1 passed"}


def brain_for(tmp_path, model, **options):
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    return Brain(repository.parent, tmp_path / "data", model, "img", requirement_checks=True, **options)


def run(brain, goal="Set x to 2"):
    async def flow():
        task = await create_direct(brain, goal)
        return await brain.execute(task["id"], task["digest"])
    return asyncio.run(flow())


# 1. Completion verification ------------------------------------------------------------------

def test_requirement_checks_catch_a_false_success_and_drive_a_repair(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", sandbox)
    model = ScriptedModel(["x = 3\n", "x = 2\n"])
    task = run(brain_for(tmp_path, model))
    kinds = [event["kind"] for event in task["events"]]
    assert task["status"] == "passed" and task["completion_verified"] is True
    assert kinds.index("requirement_checks_written") < kinds.index("tester")
    assert "requirement_checks_failed" in kinds and "completion_verified" in kinds
    assert task["failure_log"][0]["category"] == "requirement"
    assert "checks written from the goal" in model.goals[-1]
    # The checks prompt contains the goal only, and the checks never reach the deliverable.
    assert model.goals[0].endswith("Goal:\nSet x to 2")
    workspace = tmp_path / "data" / "tasks" / task["id"] / "workspace"
    assert not list(workspace.glob("test_requirements_*.py"))
    assert (workspace / "main.py").read_text() == "x = 2\n"


def test_unmet_requirement_checks_never_leave_the_task_worse(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", sandbox)
    # Visible tests pass with x = 3; every repair is broken or still wrong.
    model = ScriptedModel(["x = 3\n", "x = (\n", "x = 4\n", "x = 5\n"])
    task = run(brain_for(tmp_path, model, max_free_attempts=2))
    workspace = tmp_path / "data" / "tasks" / task["id"] / "workspace"
    assert task["status"] == "passed" and task["completion_verified"] is False
    assert "completion_unverified" in [event["kind"] for event in task["events"]]
    assert (workspace / "main.py").read_text() == "x = 3\n"
    assert task["proposal"]["changes"][0]["content"] == "x = 3\n"


def test_broken_generated_checks_are_discarded_not_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", sandbox)
    model = ScriptedModel(["x = 3\n"], checks="def broken(:\n    pass\ndef test_x():\n    assert 1\n")
    task = run(brain_for(tmp_path, model))
    assert "requirement_checks_skipped" in [event["kind"] for event in task["events"]]  # does not parse
    model = ScriptedModel(["x = 3\n"], checks="def broken():\n    pass\n\ndef test_x():\n    assert 1\n")
    task = run(brain_for(tmp_path / "second", model))
    kinds = [event["kind"] for event in task["events"]]
    assert task["status"] == "passed" and "requirement_checks_discarded" in kinds
    assert task["completion_verified"] is None and task["requirement_tests"] == {}


def test_requirement_file_is_accepted_only_in_the_expected_shape():
    task = {"id": "0123456789abcdef", "goal": "g"}
    name = requirement_file_name(task["id"])
    good = {"changes": [{"path": name, "content": REQUIREMENTS}]}
    assert accept_requirement_tests(task, json.dumps(good))["tests"] == 1
    for bad in ({"changes": [{"path": "main.py", "content": REQUIREMENTS}]},
                {"changes": [{"path": name, "content": "x = 1\n"}]},
                {"changes": [{"path": name, "content": "def test_(:\n"}]},
                {"changes": [{"path": name, "content": "def test_a():\n    pass\n" * 2000}]}):
        assert accept_requirement_tests(task, json.dumps(bad)) is None
    assert accept_requirement_tests(task, "not json") is None
    # Seen live: pytest used without an import (fixed), and an undefined helper (rejected).
    raises = "def test_a():\n    with pytest.raises(ValueError):\n        int('x')\n"
    fixed = accept_requirement_tests(task, json.dumps({"changes": [{"path": name, "content": raises}]}))
    assert fixed["content"].startswith("import pytest\n")
    undefined = "def test_a():\n    assert helper() == 1\n"
    assert accept_requirement_tests(task, json.dumps({"changes": [{"path": name, "content": undefined}]})) is None


def test_requirement_checks_are_off_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", sandbox)
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    model = ScriptedModel(["x = 3\n"])
    brain = Brain(repository.parent, tmp_path / "data", model, "img")
    task = run(brain)
    assert task["status"] == "passed" and "requirement_tests" not in task
    assert not any(goal.startswith("Do not implement") for goal in model.goals)


# 2. Escalation on repeated proposal failures ------------------------------------------------

def supervised(tmp_path, model, **settings):
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    supervisor = FakeSupervisor("claude", reply=DIAGNOSIS)
    policy = SupervisionPolicy([supervisor], SupervisorLedger(tmp_path / "data" / "s.sqlite3"),
                               plan_complex_tasks=False, **settings)
    return Brain(repository.parent, tmp_path / "data", model, "img", supervision=policy), supervisor


def test_rejected_initial_proposals_escalate_within_budget(tmp_path):
    brain, supervisor = supervised(tmp_path, ScriptedModel(["x = (\n"], checks=""))
    task = asyncio.run(create_direct(brain))
    assert task["status"] == "proposed"
    assert task["proposal"]["changes"][0]["content"] == "x = 2\n"
    assert [call[0] for call in supervisor.calls] == ["diagnose"]
    assert supervisor.calls[0][1]["evidence"]["stage"] == "initial_proposal"
    assert task["failure_log"][0]["category"] == "proposal"
    kinds = [event["kind"] for event in task["events"]]
    assert kinds.count("validation_failed") == 3 and "proposal_failed" in kinds
    assert [call["ok"] for call in brain.supervision.ledger.calls()] == [1]


def test_initial_proposal_escalation_respects_an_empty_budget(tmp_path):
    brain, supervisor = supervised(tmp_path, ScriptedModel(["x = (\n"], checks=""), diagnose_budget=0)
    with pytest.raises(ValueError):
        asyncio.run(create_direct(brain))
    assert supervisor.calls == []
    assert [task["status"] for task in brain.store.tasks()] == ["blocked"]


# 3. Relevant knowledge retrieval ------------------------------------------------------------

def skill(root, name, description, body="Steps.\n"):
    (root / "skills" / name).mkdir(parents=True)
    (root / "skills" / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\n{body}")


def test_packet_keeps_relevant_skills_and_drops_unrelated_ones(tmp_path):
    from brain.intelligence import build_index
    from brain.knowledge import KnowledgeLibrary, KnowledgeRouter
    source = tmp_path / "src"
    skill(source, "systematic-debugging", "Find the root cause of a failing test before changing code",
          "Reproduce the failing test first, then fix the root cause.\n")
    # Unrelated skills whose bodies mention the goal's words (the pilot's noise pattern).
    skill(source, "python-mcp-server", "Build an MCP server in Python",
          "Systematic debugging of the root cause of a failing test, test driven development. " * 20)
    skill(source, "batches", "Submit message batches to the API",
          "Debugging root cause: fix the bug, write the test first. " * 20)
    (source / "docs").mkdir()
    (source / "docs" / "README.md").write_text("# Duration parser bug fix failing test\n\nGeneric text.\n")
    (source / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge")
    library = KnowledgeLibrary(tmp_path / "k.sqlite3")
    library.import_source("practices", str(source))
    index = build_index(make_repository(tmp_path / "repo"))
    goal = "Fix the bug: parse_duration returns the wrong total and a test is failing"
    packet = KnowledgeRouter(library).packet(goal, index, [], ["pytest"])
    names = [rule["name"] for rule in packet["engineering_rules"]]
    assert names == ["systematic-debugging"]
    assert all(rule["kind"] == "skill" and len(rule["guidance"]) <= 700 for rule in packet["engineering_rules"])
    # References stay available on demand, but out of the packet by default.
    assert library.search("duration parser", 5, kinds=("reference",))
    assert "README" not in json.dumps(packet["engineering_rules"])


def test_intent_queries_cover_the_pilot_goal_wording():
    from brain.knowledge import intents
    assert intents("The report shows the wrong count") and intents("Rename fetch_all and deprecate it")


# 4. Static analysis -------------------------------------------------------------------------

def proposal(*files):
    return Proposal(plan="p", changes=[{"path": path, "content": content} for path, content in files])


def test_undefined_name_is_rejected_before_review(tmp_path):
    workspace = make_repository(tmp_path / "repos" / "repo")
    (workspace / "money.py").write_text("class Money:\n    pass\n")
    brain = Brain(tmp_path / "repos", tmp_path / "data", ScriptedModel(["x = 1\n"]), "img")
    with pytest.raises(ProposalInvalid) as raised:
        brain.validate_proposal(workspace, proposal(("ledger.py", "def total():\n    return Money()\n")))
    assert "Undefined name" in str(raised.value) and "Money" in str(raised.value)


def test_missing_cross_file_import_is_rejected(tmp_path):
    workspace = make_repository(tmp_path / "repos" / "repo")
    (workspace / "money.py").write_text("class Cents:\n    pass\n")
    brain = Brain(tmp_path / "repos", tmp_path / "data", ScriptedModel(["x = 1\n"]), "img")
    with pytest.raises(ProposalInvalid) as raised:
        brain.validate_proposal(workspace, proposal(("ledger.py", "from money import Money\n\nM = Money\n")))
    assert "Missing import" in str(raised.value) and "money does not define Money" in str(raised.value)


def test_names_defined_in_the_same_proposal_are_accepted(tmp_path):
    workspace = make_repository(tmp_path / "repos" / "repo")
    (workspace / "money.py").write_text("class Cents:\n    pass\n")
    brain = Brain(tmp_path / "repos", tmp_path / "data", ScriptedModel(["x = 1\n"]), "img")
    accepted, _ = brain.validate_proposal(workspace, proposal(
        ("money.py", "class Cents:\n    pass\n\n\nclass Money:\n    pass\n"),
        ("ledger.py", "import os\nfrom money import Money\n\n\ndef total():\n    return Money(), os.sep\n")))
    assert [change.path for change in accepted.changes] == ["money.py", "ledger.py"]


def test_static_checks_allow_dynamic_modules_and_builtins():
    read = {"dyn": "def __getattr__(name):\n    return name\n", "star": "from os import *\n"}.get
    assert static_issues("a.py", "from dyn import anything\nprint(len(anything))\n", read) == []
    assert static_issues("a.py", "from star import path\n", read) == []
    assert static_issues("a.py", "from json import nothing_here\n", lambda name: None) == []


# 5. Failure classification: root cause first ------------------------------------------------

PILOT = Path(__file__).resolve().parents[1] / "benchmarks" / "results" / "2026-10-09-gauntlet-pilot-abc.json"


def failed_run(*kinds, **fields):
    return {"outcome": "failed", "agent_status": fields.pop("status", "attention_required"),
            "trajectory": [{"kind": kind, "detail": ""} for kind in kinds], **fields}


def test_upstream_model_failure_is_the_root_cause_not_orchestration():
    from brain.gauntlet import classify_failure, downstream_effects
    run = failed_run("delegated", "knowledge_packet", "validation_failed", "knowledge_packet",
                     "validation_failed", "validation_failed", "blocked", "blocked", "dependency_blocked")
    assert classify_failure(run) == "model" and downstream_effects(run) == ["dependency_blocked"]
    assert classify_failure(failed_run("delegated", "proposal_failed", "dependency_blocked")) == "model"
    assert classify_failure(failed_run("delegated", "test_finished", "attempts_exhausted",
                                       "dependency_blocked")) == "model"
    # Without model evidence, orchestration problems are still orchestration failures.
    assert classify_failure(failed_run("delegated", "dependency_blocked")) == "orchestration"
    assert classify_failure(failed_run("delegated", "integration_conflict",
                                       status="integration_conflict")) == "orchestration"
    # A proposal that was rejected and then corrected is not a failure.
    corrected = failed_run("validation_failed", "implementer", "integration_conflict")
    assert classify_failure(corrected) == "orchestration"


def test_pilot_rescoring_changes_only_the_mislabeled_record():
    from brain.gauntlet import classify_failure
    runs = json.loads(PILOT.read_text())["runs"]
    changed = [(run["condition"], run["task"], run["failure_cause"], classify_failure(run))
               for run in runs if classify_failure(run) != run.get("failure_cause")]
    assert changed == [("C_three_phase", "orchestration-money", "orchestration", "model")]


def test_orchestration_and_engineering_success_are_scored_separately():
    from brain.gauntlet import markdown_report, run_mode, summarize
    saved = json.loads(PILOT.read_text())
    summary = summarize(saved["runs"], saved["conditions"])["conditions"]
    assert summary["B_coding_brain"]["orchestration_success"] == "1/1"  # integrated, but wrong
    assert summary["B_coding_brain"]["engineering_success"] == "6/10"
    assert summary["C_three_phase"]["orchestration_success"] == "0/1"
    assert summary["C_three_phase"]["downstream_orchestration_effects"] == 1
    assert summary["A_free_alone"]["orchestration_success"] == "0/0"  # A has no orchestration engine
    assert {run_mode(run) for run in saved["runs"] if run["task"] == "orchestration-money"} == \
        {"task", "orchestration"}
    report = markdown_report({**saved, "summary": {"conditions": summary, "paired": {}}})
    assert "Orchestration success" in report and "Engineering success" in report


# 6. Model-level accounting ------------------------------------------------------------------

class ServedModel:
    """A reachable brain that reports usage the way the adapters do."""
    name = "served-brain"

    async def propose(self, root, goal, memories, repository_context=None, task_id=None):
        from brain import accounting
        accounting.record("inference", role="implementer", provider="ollama", model=self.name,
                          requested=self.name, prompt_tokens=10, output_tokens=5)
        return json.dumps({"plan": "p", "changes": [{"path": "main.py", "content": "x = 2\n"}]})

    async def review(self, goal, diff):
        from brain import accounting
        accounting.record("inference", role="reviewer", provider="ollama", model=self.name, prompt_tokens=4)
        return {"approved": True, "reason": "ok"}

    async def decompose(self, goal):
        return {"assignments": [{"name": "only", "goal": goal, "depends_on": []}]}


def test_every_inference_and_fallback_is_attributed_to_the_task(tmp_path, monkeypatch):
    from brain.accounting import summarize
    from brain.brains import FailoverModel
    from brain.model import OllamaModel
    from brain.routing import RoutedModel
    monkeypatch.setattr("brain.service.run_tests", lambda *a, **k: {"passed": True, "exit_code": 0, "output": ""})
    offline = OllamaModel("http://127.0.0.1:9", "offline-preferred-brain")
    preferred = FailoverModel([offline, ServedModel()])
    model = RoutedModel(preferred, preferred)
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    brain = Brain(repository.parent, tmp_path / "data", model, "img")
    task = run(brain)
    assert task["status"] == "passed"
    stored = brain.store.get(task["id"])
    summary = summarize(stored["inference_log"])
    assert summary["models"]["served-brain"]["roles"] == {"implementer": 1, "reviewer": 1}
    assert summary["models"]["served-brain"]["prompt_tokens"] == 14
    assert "offline-preferred-brain" not in summary["models"]  # it never served a request
    assert summary["fallbacks"] == 2  # propose and review each switched brains
    assert summary["fallback_detail"][0] == {"method": "propose", "from": ["offline-preferred-brain"],
                                             "to": "served-brain", "reason": "ConnectError"}
    assert summary["routes"] == {"implementer->offline-preferred-brain+served-brain": 1,
                                 "reviewer->offline-preferred-brain+served-brain": 1}


def test_adapters_record_the_model_that_actually_served():
    from types import SimpleNamespace
    from brain import accounting
    from brain.anthropic_model import AnthropicModel
    from brain.model import OllamaModel
    entries = []
    with accounting.collect(entries):
        OllamaModel("http://x", "qwen3:4b")._record({"model": "qwen3:4b", "prompt_eval_count": 7,
                                                     "eval_count": 3}, "implementer")
        anthropic = AnthropicModel.__new__(AnthropicModel)
        anthropic.name, anthropic.usage = "requested-model", []
        anthropic._record(SimpleNamespace(model="fallback-model",
                                          usage=SimpleNamespace(input_tokens=5, output_tokens=2)), "reviewer")
    accounting.record("inference", model="outside")  # not collected: no task scope
    summary = accounting.summarize(entries)
    assert summary["models"]["qwen3:4b"]["output_tokens"] == 3
    assert summary["models"]["fallback-model"]["served_instead_of"] == "requested-model"
    assert "outside" not in summary["models"] and len(entries) == 2


def test_escalations_record_each_supervisor_attempt_and_served_model(tmp_path):
    from brain import accounting
    from brain.subscriptions import ClaudeCodeSupervisor, SubscriptionError
    from tests.test_phase7 import Runner
    down = FakeSupervisor("claude", error=SubscriptionError("not signed in"))
    codex = FakeSupervisor("codex", reply=DIAGNOSIS)
    policy = SupervisionPolicy([down, codex], SupervisorLedger(tmp_path / "s.sqlite3"))
    entries = []
    with accounting.collect(entries):
        asyncio.run(policy.consult({"id": "t1"}, "diagnose", {}))
    assert [(entry["supervisor"], entry["ok"]) for entry in entries] == [("claude", False), ("codex", True)]

    class UsageRunner(Runner):
        async def __call__(self, arguments, stdin, cwd):
            code, out, err = await super().__call__(arguments, stdin, cwd)
            if arguments[1] == "-p":
                data = json.loads(out)
                data["modelUsage"] = {"served-premium-model": {"inputTokens": 120, "outputTokens": 40}}
                out = json.dumps(data)
            return code, out, err
    runner = UsageRunner((0, '{"loggedIn": true, "authMethod": "claude.ai"}', ""), DIAGNOSIS)
    entries = []
    with accounting.collect(entries):
        asyncio.run(ClaudeCodeSupervisor("claude", runner=runner).ask("diagnose", {"goal": "g"}))
    assert entries[0]["model"] == "served-premium-model" and entries[0]["output_tokens"] == 40


# No hidden-test leakage ---------------------------------------------------------------------

def test_requirement_prompts_carry_only_the_goal_for_every_benchmark_task():
    from brain.completion import REQUIREMENT_PROMPT, requirement_goal
    from brain.gauntlet import tasks_in
    root = Path(__file__).resolve().parents[1] / "gauntlet" / "tasks"
    for task in tasks_in(root):
        prompt = requirement_goal({"id": "0123456789abcdef", "goal": task["goal"]})
        assert prompt == REQUIREMENT_PROMPT.format(name="test_requirements_01234567.py", goal=task["goal"])
        for hidden in (task["path"] / "hidden").rglob("*.py"):
            assert hidden.name not in prompt
            names = [line.split("(")[0][4:] for line in hidden.read_text().splitlines() if line.startswith("def test")]
            assert not any(name in prompt for name in names)
            assert not (task["path"] / "repo" / hidden.name).exists()
