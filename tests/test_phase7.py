import asyncio
import json
import subprocess
from pathlib import Path
import httpx
import pytest
from brain.brains import ImplementerUnavailable, load_brains
from brain.service import Brain
from brain.subscriptions import ClaudeCodeSupervisor, CodexSupervisor, SubscriptionError
from brain.supervision import SupervisionPolicy, SupervisorLedger
from tests.test_brain import create_direct, git, make_repository


class Runner:
    """Stands in for the claude/codex executables."""
    def __init__(self, status, reply):
        self.status, self.reply, self.calls = status, reply, []

    async def __call__(self, arguments, stdin, cwd):
        self.calls.append({"arguments": arguments, "stdin": stdin, "cwd": cwd})
        if arguments[1:] in (["auth", "status"], ["login", "status"]):
            return self.status
        if arguments[1] == "exec":
            Path(arguments[arguments.index("-o") + 1]).write_text(json.dumps(self.reply))
            return 0, "", ""
        return 0, json.dumps({"type": "result", "is_error": False, "result": "",
                              "structured_output": self.reply}), ""


DIAGNOSIS = {"diagnosis": "split(' ') keeps empty strings", "instructions": "Use text.split()",
             "affected_files": ["main.py"], "tests": ["test_main.py"], "changes": []}


def test_claude_cli_runs_read_only_without_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    runner = Runner((0, '{"loggedIn": true, "authMethod": "claude.ai"}', ""), DIAGNOSIS)
    supervisor = ClaudeCodeSupervisor("claude", model="opus", runner=runner)
    result = asyncio.run(supervisor.ask("diagnose", {"goal": "g"}, tmp_path))
    assert result["instructions"] == "Use text.split()"
    arguments = runner.calls[1]["arguments"]
    assert arguments[:2] == ["claude", "-p"] and "--json-schema" in arguments
    assert arguments[arguments.index("--tools") + 1] == "Read,Grep,Glob"
    assert arguments[arguments.index("--permission-mode") + 1] == "dontAsk"
    assert arguments[-2:] == ["--model", "opus"] and runner.calls[1]["cwd"] == tmp_path
    assert "untrusted" in runner.calls[1]["stdin"]
    assert "ANTHROPIC_API_KEY" not in supervisor.environment()


def test_cli_supervisors_refuse_api_key_billing_and_signed_out():
    api = Runner((0, '{"loggedIn": true, "authMethod": "api_key"}', ""), DIAGNOSIS)
    with pytest.raises(SubscriptionError, match="billed separately"):
        asyncio.run(ClaudeCodeSupervisor("claude", runner=api).ask("plan", {}))
    assert len(api.calls) == 1
    allowed = ClaudeCodeSupervisor("claude", runner=api, allow_api_billing=True)
    assert asyncio.run(allowed.ask("diagnose", {}))["diagnosis"]
    with pytest.raises(SubscriptionError, match="not signed in"):
        asyncio.run(CodexSupervisor("codex", runner=Runner((1, "Not logged in", ""), {})).ask("plan", {}))
    with pytest.raises(SubscriptionError, match="API key"):
        asyncio.run(CodexSupervisor("codex", runner=Runner(
            (0, "Logged in using an API key - sk-***", ""), {})).ask("plan", {}))


def test_codex_cli_uses_read_only_exec_and_output_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    runner = Runner((0, "Logged in using ChatGPT", ""), {"approved": False, "reason": "bug"})
    supervisor = CodexSupervisor("codex", runner=runner)
    assert asyncio.run(supervisor.review("goal", "diff")) == {"approved": False, "reason": "bug"}
    arguments = runner.calls[1]["arguments"]
    assert arguments[:4] == ["codex", "exec", "--sandbox", "read-only"]
    assert "--output-schema" in arguments and "--ephemeral" in arguments and arguments[-1] == "-"
    assert "OPENAI_API_KEY" not in supervisor.environment()


class FakeSupervisor:
    def __init__(self, name, reply=None, error=None):
        self.name, self.reply, self.error, self.calls = name, reply, error, []

    async def ask(self, kind, payload, workspace=None):
        self.calls.append((kind, payload))
        if self.error:
            raise self.error
        return self.reply


def test_policy_budgets_failover_and_grants(tmp_path):
    down = FakeSupervisor("claude", error=SubscriptionError("not signed in"))
    codex = FakeSupervisor("codex", reply=DIAGNOSIS)
    policy = SupervisionPolicy([down, codex], SupervisorLedger(tmp_path / "s.sqlite3"), daily_limit=2)
    task = {"id": "t1"}
    result = asyncio.run(policy.consult(task, "diagnose", {}))
    assert result["supervisor"] == "codex" and policy.remaining(task, "diagnose") == 0
    from brain.supervision import SupervisorBudgetExceeded
    with pytest.raises(SupervisorBudgetExceeded, match="budget"):
        asyncio.run(policy.consult(task, "diagnose", {}))
    task["supervisor_grants"] = {"diagnose": 1}
    with pytest.raises(SupervisorBudgetExceeded, match="Daily"):
        asyncio.run(policy.consult(task, "diagnose", {}))
    assert [call["ok"] for call in policy.ledger.calls()] == [1, 0]
    with pytest.raises(ValueError, match="Unknown supervision setting"):
        SupervisionPolicy([], policy.ledger, surprise=True)


class FlakyModel:
    """A free implementer that never fixes the bug until a supervisor tells it how."""
    def __init__(self):
        self.goals = []

    async def propose(self, root, goal, memories, repository_context=None):
        self.goals.append(goal)
        if "pull request feedback" in goal:
            content = "x = 2\ny = 3\n"
        else:
            content = "x = 2\n" if "Supervisor repair plan" in goal else "x = 'still wrong'\n"
        return json.dumps({"plan": "attempt", "changes": [{"path": "main.py", "content": content}]})

    async def review(self, goal, diff):
        return {"approved": True, "reason": "ok"}

    async def decompose(self, goal):
        return {"assignments": [{"name": "only", "goal": goal, "depends_on": []}]}


def fixed_when_x_is_two(workspace, image, **kwargs):
    fixed = (workspace / "main.py").read_text() == "x = 2\n"
    return {"passed": fixed, "exit_code": 0 if fixed else 1, "output": "ok" if fixed else "assert x == 2"}


def supervised_brain(tmp_path, supervisor, **settings):
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    policy = SupervisionPolicy([supervisor], SupervisorLedger(tmp_path / "data" / "s.sqlite3"), **settings)
    return Brain(repository.parent, tmp_path / "data", FlakyModel(), "img", supervision=policy)


def test_repeated_failures_escalate_once_and_free_worker_applies_fix(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", fixed_when_x_is_two)
    supervisor = FakeSupervisor("claude", reply=DIAGNOSIS)
    brain = supervised_brain(tmp_path, supervisor, plan_complex_tasks=False)

    async def flow():
        task = await create_direct(brain)
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    assert task["status"] == "passed"
    assert [call[0] for call in supervisor.calls] == ["diagnose"]
    assert supervisor.calls[0][1]["evidence"]["failures"] == 2
    assert "Use text.split()" in brain.model.goals[-1]
    assert task["proposal_author"] == "implementer"
    kinds = [event["kind"] for event in task["events"]]
    # The repeated, unchanged repair is rejected by the scope gate instead of re-running tests.
    assert kinds.count("test_finished") == 2 and "supervisor_diagnose" in kinds
    assert kinds.count("validation_failed") == 3
    assert [item["category"] for item in task["failure_log"]] == ["test_failure", "validation"]
    asyncio.run(brain.accept(task["id"], "Fixed with supervisor guidance"))
    assert brain.store.memory(task["id"])["supervision"] == [{"supervisor": "claude", "kind": "diagnose"}]


def test_budget_exhaustion_fails_then_human_escalation_reproposes(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", fixed_when_x_is_two)
    supervisor = FakeSupervisor("claude", reply=DIAGNOSIS)
    brain = supervised_brain(tmp_path, supervisor, diagnose_budget=0, plan_complex_tasks=False)

    async def flow():
        task = await create_direct(brain)
        task = await brain.execute(task["id"], task["digest"])
        assert task["status"] == "failed" and not supervisor.calls
        assert any(event["kind"] == "supervisor_budget_exhausted" for event in task["events"])
        brain.escalate(task["id"])
        while brain.jobs:
            await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
        return brain.store.get(task["id"])
    task = asyncio.run(flow())
    assert task["status"] == "proposed" and len(supervisor.calls) == 1
    assert task["proposal"]["changes"][0]["content"] == "x = 2\n"
    with pytest.raises(ValueError, match="failed or blocked"):
        brain.escalate(task["id"])


def test_takeover_supplies_changes_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.service.run_tests", fixed_when_x_is_two)
    reply = {**DIAGNOSIS, "changes": [{"path": "main.py", "content": "x = 2\n"}]}
    brain = supervised_brain(tmp_path, FakeSupervisor("codex", reply=reply), takeover=True,
                             plan_complex_tasks=False)

    async def flow():
        task = await create_direct(brain)
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    assert task["status"] == "passed" and task["proposal_author"] == "supervisor"
    assert any(event["kind"] == "supervisor_takeover" for event in task["events"])


def test_premium_plan_guides_free_worker(tmp_path):
    plan = {"plan": "Change x to 2", "steps": ["edit main.py"], "risks": []}
    supervisor = FakeSupervisor("claude", reply=plan)
    brain = supervised_brain(tmp_path, supervisor)

    async def flow():
        task = brain.submit("demo", "Fix x", launch=False, premium_plan=True)
        return await brain.create(task)
    asyncio.run(flow())
    assert supervisor.calls[0][0] == "plan"
    assert "Supervisor plan from claude" in brain.model.goals[0] and "edit main.py" in brain.model.goals[0]


def test_unreachable_free_model_pauses_without_escalating(tmp_path):
    supervisor = FakeSupervisor("claude", reply=DIAGNOSIS)
    brain = supervised_brain(tmp_path, supervisor, plan_complex_tasks=False)

    async def offline(*args, **kwargs):
        raise ImplementerUnavailable("No brain is reachable")
    brain.model.propose = offline

    async def flow():
        task = await create_direct(brain)
        assert task["status"] == "awaiting_implementer" and not supervisor.calls
        assert brain.retry(task["id"])["status"] == "queued"
        for job in list(brain.jobs.values()):
            job.cancel()
    asyncio.run(flow())


def test_supervisor_config_file_and_validation(tmp_path):
    path = tmp_path / "brains.json"
    path.write_text(json.dumps({
        "brains": {"local": {"provider": "ollama", "model": "m"}},
        "supervisors": {"claude": {"provider": "claude_cli", "model": "opus"},
                        "codex": {"provider": "codex_cli"}},
        "supervision": {"order": ["codex", "claude"], "escalate_after": 3}}))
    config = load_brains(path)
    assert list(config["supervisors"]) == ["codex", "claude"]
    assert config["supervision"] == {"escalate_after": 3}
    path.write_text(json.dumps({"brains": {"local": {"provider": "ollama", "model": "m"}},
                                "supervisors": {"x": {"provider": "anthropic"}}}))
    with pytest.raises(ValueError, match="claude_cli or codex_cli"):
        load_brains(path)


def github_transport(log):
    def handler(request: httpx.Request):
        log.append((request.method, request.url.path, request.content))
        path = request.url.path
        if request.method == "POST" and path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7"})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [
                {"name": "tests", "status": "completed", "conclusion": "failure",
                 "output": {"summary": "test_main.py::test_x failed"}},
                {"name": "lint", "status": "completed", "conclusion": "success", "output": {}}]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if path.endswith("/reviews"):
            return httpx.Response(200, json=[{"user": {"login": "me"}, "state": "CHANGES_REQUESTED",
                                              "body": "Handle the empty case"}])
        return httpx.Response(200, json=[])
    return httpx.MockTransport(handler)


def test_publish_feedback_and_follow_up_update_the_same_pull_request(tmp_path, monkeypatch):
    from brain.github import GitHubClient
    monkeypatch.setattr("brain.service.run_tests",
                        lambda *a, **k: {"passed": True, "exit_code": 0, "output": "passed"})
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repository = make_repository(tmp_path / "repos" / "demo", use_git=True)
    git(repository, "remote", "add", "origin", str(remote))
    log = []
    brain = Brain(repository.parent, tmp_path / "data", FlakyModel(), "img")
    brain.github_factory = lambda: GitHubClient("token", transport=github_transport(log))

    async def flow():
        task = await create_direct(brain)
        task = await brain.execute(task["id"], task["digest"])
        await brain.accept(task["id"], "done")
        with pytest.raises(ValueError, match="not a GitHub repository"):
            await brain.publish(task["id"])
        task = await brain.publish(task["id"], github_repository="o/r")
        assert task["pull_request"]["number"] == 7
        assert git(remote, "rev-parse", task["pull_request"]["branch"]) == task["commit"]
        body = json.loads(log[0][2])
        assert body["draft"] is True and body["head"] == task["pull_request"]["branch"]
        assert body["base"] in {"master", "main"}
        task = await brain.pr_feedback(task["id"])
        assert [check["name"] for check in task["pr_feedback"]["failing"]] == ["tests"]
        assert task["pr_feedback"]["changes_requested"]
        follow = brain.follow_up(task["id"])
        assert "Handle the empty case" in follow["goal"] and follow["base_commit"] == task["commit"]
        while brain.jobs:
            await asyncio.gather(*list(brain.jobs.values()), return_exceptions=True)
        follow = brain.store.get(follow["id"])
        follow = await brain.execute(follow["id"], follow["digest"])
        await brain.accept(follow["id"], "Addressed review")
        follow = await brain.publish(follow["id"])
        assert follow["pull_request"]["number"] == 7
        assert git(remote, "rev-parse", task["pull_request"]["branch"]) == follow["commit"]
    asyncio.run(flow())
    assert sum(1 for method, path, _ in log if method == "POST") == 1


def shared_state_suite(workspace, image, only=None, **kwargs):
    """A suite whose test passes alone but fails with the others (leaked module state)."""
    fixed = (workspace / "main.py").read_text() == "x = 2\n"
    if fixed or only:
        return {"passed": True, "exit_code": 0, "profile": "python", "output": "1 passed"}
    return {"passed": False, "exit_code": 1, "profile": "python",
            "output": "FAILED tasks.py::test_list - assert [{'id': 3}] == [{'id': 1}]\n1 failed, 2 passed in 0.02s"}


def test_shared_state_is_diagnosed_and_reaches_the_supervisor(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("brain.service.run_tests", lambda *a, **k: calls.append(k.get("only")) or shared_state_suite(*a, **k))
    supervisor = FakeSupervisor("claude", reply=DIAGNOSIS)
    brain = supervised_brain(tmp_path, supervisor, plan_complex_tasks=False)

    async def flow():
        task = brain.submit("demo", "Fix x", launch=False)
        task["tests_expected"] = True  # a project created by `codingbrain new`
        task = await brain.create(task)
        return await brain.execute(task["id"], task["digest"])
    task = asyncio.run(flow())
    assert ["tasks.py::test_list"] in calls  # the failing test was re-run alone
    assert any("pass(es) alone but fail(s)" in goal for goal in brain.model.goals)  # the free model was told
    diagnosis = supervisor.calls[0][1]["evidence"]
    assert "share state" in diagnosis["last_feedback"]  # and so was the stronger supervisor
    assert task["failure_log"][0]["diagnosis"] == "test_isolation"
    assert task["status"] == "passed"


def test_single_test_runs_accept_only_pytest_node_ids(tmp_path, monkeypatch):
    from brain.sandbox import run_tests
    (tmp_path / "coding-brain.json").write_text('{"test_profile": "python"}')
    launched = []
    monkeypatch.setattr("brain.sandbox.subprocess.run", lambda command, **k: launched.append(command) or
                        type("R", (), {"returncode": 0})())
    for bad in (["--rootdir=/"], ["tasks.py::test_x; rm -rf /"], ["-p", "evil"]):
        assert run_tests(tmp_path, "img", only=bad)["exit_code"] is None
    assert launched == []
    run_tests(tmp_path, "img", only=["tests/test_tasks.py::test_list[case-1]"])
    assert launched[0][-1] == "tests/test_tasks.py::test_list[case-1]" and "--network=none" in launched[0]
