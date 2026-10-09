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
    # Inconclusive checks never count as verified completion.
    assert task["completion_verified"] is False and task["completion"]["status"] == "inconclusive"
    assert task["requirement_tests"] == {}


def local_pytest(workspace, image, **kwargs):
    """The real pytest output format, run locally instead of in Docker."""
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=workspace,
                            capture_output=True, text=True, timeout=120)
    return {"passed": result.returncode == 0, "exit_code": result.returncode,
            "output": (result.stdout + result.stderr)[:16_000]}


MONEY_GOAL = "Set x to 2 and add a Money class with integer cents and a currency code"
MONEY = "x = 2\n\n\nclass Money:\n    def __init__(self, cents, currency):\n        self.cents, self.currency = cents, currency\n"
# Seen live: a check that invents an interface the goal never states, and one that forgets an import.
INVALID = ("def test_invented_keyword():\n    import main\n    main.Money(amount=1, currency='EUR')\n\n\n"
           "def test_invented_attribute():\n    import main\n    assert main.Money(1, 'EUR').amount == 1\n\n\n"
           "def test_forgot_import():\n    import main\n    assert helper(main) == 1\n\n\n")
VALID = "def test_cents():\n    import main\n    assert main.Money(250, 'USD').cents == 250\n"


def test_invalid_checks_are_rejected_without_using_the_repair_budget(tmp_path, monkeypatch):
    from brain import completion
    monkeypatch.setattr("brain.service.run_tests", local_pytest)
    # helper() is undefined: the generation-time gate would drop the file, so admit it here to
    # exercise the runtime safeguard as well.
    monkeypatch.setattr(completion, "accept_requirement_tests", lambda task, raw: {
        "path": completion.requirement_file_name(task["id"]),
        "content": json.loads(raw)["changes"][0]["content"], "tests": 4})
    monkeypatch.setattr("brain.service.accept_requirement_tests", completion.accept_requirement_tests)
    model = ScriptedModel([MONEY], checks=INVALID + VALID)
    task = run(brain_for(tmp_path, model), MONEY_GOAL)
    rejected = json.loads(next(event["detail"] for event in task["events"]
                               if event["kind"] == "requirement_checks_rejected"))
    assert sorted(rejected) == ["test_forgot_import", "test_invented_attribute", "test_invented_keyword"]
    assert "amount" in rejected["test_invented_keyword"]
    assert task["status"] == "passed" and task["completion"]["status"] == "verified"
    assert task.get("failure_log", []) == [] and len(model.goals) == 2  # checks + one implementation
    assert task["metrics"]["requirement_tests_rejected"] == 3


def test_a_check_named_in_the_goal_is_a_real_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", local_pytest)
    checks = "def test_amount():\n    import main\n    assert main.Money(1, 'EUR').amount == 1\n"
    model = ScriptedModel([MONEY], checks=checks)
    task = run(brain_for(tmp_path, model), MONEY_GOAL + "; expose the value as amount")
    assert "requirement_checks_rejected" not in [event["kind"] for event in task["events"]]
    assert task["failure_log"][0]["category"] == "requirement"


def test_no_repeated_repairs_against_an_unchanged_failing_check(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", local_pytest)
    checks = "def test_cents():\n    import main\n    assert main.Money(250, 'USD').cents == 250\n"
    wrong = MONEY.replace("self.cents, self.currency = cents, currency", "self.cents, self.currency = 0, currency")
    model = ScriptedModel([wrong, wrong.replace("x = 2", "x = 2  # tried")], checks=checks)
    task = run(brain_for(tmp_path, model, max_free_attempts=5), MONEY_GOAL)
    kinds = [event["kind"] for event in task["events"]]
    assert task["metrics"]["requirement_repairs"] == 1 and kinds.count("requirement_checks_failed") == 2
    assert task["status"] == "passed" and task["completion"]["status"] == "unverified"
    assert task["completion_verified"] is False and len(model.goals) == 3  # checks + 2 implementations
    workspace = tmp_path / "data" / "tasks" / task["id"] / "workspace"
    assert (workspace / "main.py").read_text() == wrong  # the first version, no worse than the second


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


# Audit preservation of generated checks -----------------------------------------------------

def run_invalid_scenario(tmp_path, monkeypatch, audit=True):
    from brain import completion
    from brain.service import Brain as BrainClass
    monkeypatch.setattr("brain.service.run_tests", local_pytest)
    admit = lambda task, raw: {"path": completion.requirement_file_name(task["id"]),
                               "content": json.loads(raw)["changes"][0]["content"], "tests": 4}
    monkeypatch.setattr("brain.service.accept_requirement_tests", admit)
    if not audit:
        monkeypatch.setattr(BrainClass, "_audit", lambda self, task, stage, content, **fields: None)
    return run(brain_for(tmp_path, ScriptedModel([MONEY], checks=INVALID + VALID)), MONEY_GOAL)


def test_rejected_checks_are_preserved_with_reasons_and_checksums(tmp_path, monkeypatch):
    import hashlib
    import stat
    task = run_invalid_scenario(tmp_path, monkeypatch)
    written, rejected = task["requirement_audit"]
    assert (written["stage"], rejected["stage"]) == ("written", "rejected")
    assert written["content"] == INVALID + VALID == rejected["content"]
    assert written["sha256"] == hashlib.sha256((INVALID + VALID).encode()).hexdigest()
    assert sorted(rejected["reasons"]) == ["test_forgot_import", "test_invented_attribute", "test_invented_keyword"]
    assert rejected["remaining_tests"] == 1
    assert rejected["remaining_sha256"] == hashlib.sha256(task["requirement_tests"]["content"].encode()).hexdigest()
    folder = tmp_path / "data" / "tasks" / task["id"] / "audit"
    files = sorted(folder.iterdir())
    assert [path.name for path in files] == ["requirement-001-written.json", "requirement-002-rejected.json"]
    for path, entry in zip(files, (written, rejected)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
        body = path.read_text().strip()
        assert hashlib.sha256(body.encode()).hexdigest() == entry["record_sha256"]
        assert json.loads(body)["content"] == entry["content"]


def test_audit_records_do_not_change_execution(tmp_path, monkeypatch):
    with_audit = run_invalid_scenario(tmp_path / "a", monkeypatch)
    without = run_invalid_scenario(tmp_path / "b", monkeypatch, audit=False)
    assert [event["kind"] for event in with_audit["events"]] == [event["kind"] for event in without["events"]]
    keep = lambda task: {key: value for key, value in task["metrics"].items() if "seconds" not in key}
    assert keep(with_audit) == keep(without)
    assert with_audit["completion"] == without["completion"] and with_audit["status"] == without["status"]
    assert "requirement_audit" not in without


def test_unusable_and_discarded_checks_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", sandbox)
    task = run(brain_for(tmp_path, ScriptedModel(["x = 3\n"], checks="def broken(:\n")))
    assert [entry["stage"] for entry in task["requirement_audit"]] == ["unusable"]
    assert "def broken(:" in task["requirement_audit"][0]["content"]
    checks = "def broken():\n    pass\n\ndef test_x():\n    assert 1\n"
    task = run(brain_for(tmp_path / "second", ScriptedModel(["x = 3\n"], checks=checks)))
    assert [entry["stage"] for entry in task["requirement_audit"]] == ["written", "discarded"]
    assert task["requirement_audit"][1]["category"] == "collection"


# Round Two measurements ---------------------------------------------------------------------

def test_premium_interventions_record_trigger_and_recovery():
    from brain.gauntlet import premium_interventions, run_cost
    events = [{"kind": "test_finished", "detail": '{"passed": false}'},
              {"kind": "test_finished", "detail": '{"passed": false}'},
              {"kind": "supervisor_diagnose", "detail": "claude: ..."},
              {"kind": "test_finished", "detail": '{"passed": true}'}]
    found = premium_interventions(events)
    assert found == [{"kind": "diagnose", "trigger": "repeated failures: test_finished", "after_failures": 2,
                      "followed_by_passing_tests": True}]
    assert premium_interventions(events[:3])[0]["followed_by_passing_tests"] is False
    run = {"free_prompt_tokens": 10, "free_output_tokens": 2, "premium_calls": 1,
           "requirement_metrics": {"requirement_generation_prompt_tokens": 4, "requirement_generation_output_tokens": 1},
           "models": {"models": {"claude-x": {"provider": "claude_cli", "prompt_tokens": 300, "output_tokens": 40},
                                 "qwen": {"provider": "ollama", "prompt_tokens": 10, "output_tokens": 2}}}}
    cost = run_cost(run)
    assert (cost["premium_input_tokens"], cost["premium_output_tokens"], cost["requirement_generation_tokens"]) == \
        (300, 40, 5)


def test_verification_errors_are_scored_against_hidden_tests():
    from brain.gauntlet import summarize
    runs = [{"condition": "B", "task": t, "iteration": 0, "category": "bug", "outcome": outcome,
             "hidden_tests_passed": outcome == "passed", "completion": completion}
            for t, outcome, completion in [("a", "passed", "verified"), ("b", "passed", "unverified"),
                                           ("c", "passed", "inconclusive"), ("d", "failed", "verified"),
                                           ("e", "passed", None)]]
    summary = summarize(runs, ["B"])["conditions"]["B"]
    assert summary["engineering_success"] == "4/5"  # hidden tests decide, whatever the agent reported
    assert summary["verification_false_negatives"] == 2 and summary["verification_false_positives"] == 1
    assert summary["verification_false_negative_rate"] == round(2 / 3, 3)
