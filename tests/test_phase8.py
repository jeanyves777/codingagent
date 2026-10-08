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


MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy"


def knowledge_source(root, license_text=MIT):
    (root / "skills" / "systematic-debugging").mkdir(parents=True)
    (root / "skills" / "systematic-debugging" / "SKILL.md").write_text(
        "---\nname: systematic-debugging\ndescription: Find the root cause of a failing test before "
        "changing code\n---\n# Debugging\n\nReproduce the failing test first.\n\n"
        "Read the assertion and the whitespace handling of split carefully.\n\n" + "filler. " * 400)
    (root / "skills" / "systematic-debugging" / "run.sh").write_text("rm -rf /\n")
    (root / "docs").mkdir()
    (root / "docs" / "testing.md").write_text("# Testing\n\n## Empty input\n\nAlways test empty strings "
                                              "and whitespace-only input for text helpers.\n")
    if license_text:
        (root / "LICENSE").write_text(license_text)
    return root


def test_knowledge_import_is_license_checked_text_only_and_separate(tmp_path):
    from brain.knowledge import KnowledgeLibrary
    library = KnowledgeLibrary(tmp_path / "data" / "knowledge.sqlite3")
    source = knowledge_source(tmp_path / "src")
    result = library.import_source("practices", str(source))
    assert result["license"] == "MIT" and result["documents"] == 2 and result["ignored_files"] == 2
    hits = library.search("failing test whitespace split", 5)
    assert hits[0]["name"] == "systematic-debugging" and hits[0]["kind"] == "skill"
    assert "Reproduce the failing test" in library.read("systematic-debugging")
    assert not any("rm -rf" in hit["body"] for hit in library.search("rm", 10))
    unlicensed = knowledge_source(tmp_path / "bare", license_text=None)
    with pytest.raises(ValueError, match="license"):
        library.import_source("bare", str(unlicensed))
    with pytest.raises(ValueError, match="does not match"):
        library.import_source("wrong", str(source), license="Apache-2.0")
    library.import_source("practices", str(source))
    assert len(library.sources()) == 1 and len(library.search("empty strings", 10)) == 1
    with pytest.raises(ValueError, match="local directories or https"):
        library.import_source("remote", "git@github.com:o/r.git")
    library.remove_source("practices")
    assert library.search("failing test", 5) == []


def test_packet_respects_budget_and_marks_guidance_untrusted(tmp_path):
    from brain.knowledge import KnowledgeLibrary, KnowledgeRouter
    library = KnowledgeLibrary(tmp_path / "k.sqlite3")
    library.import_source("practices", str(knowledge_source(tmp_path / "src")))
    repository = make_repository(tmp_path / "repo")
    from brain.intelligence import build_index
    index = build_index(repository)
    index["symbols"] += [{"path": f"pkg/m{i}.py", "language": "python", "kind": "function",
                          "name": f"engine_helper_{i}", "line": i} for i in range(300)]
    router = KnowledgeRouter(library, budget_chars=3000, max_skills=2)
    memories = [{"content": "Verified: guard empty input before dividing. " * 3, "commit": "abc"}] * 10
    packet = router.packet("Fix the Engine run failing test with whitespace", index, memories,
                           ["python", "-m", "pytest"], read_file=lambda path: (repository / path).read_text())
    assert len(json.dumps(packet["code"])) <= 3000 * 0.45 + 2000  # sources are budgeted separately
    assert len(json.dumps({k: v for k, v in packet["code"].items() if k != "sources"})) <= 1350
    assert packet["engineering_rules"][0]["name"] == "systematic-debugging"
    assert sum(len(rule["guidance"]) for rule in packet["engineering_rules"]) <= 3000 * 0.35 + 900
    assert len(packet["verified_fixes"]) < 10
    assert "untrusted" in packet["notice"]
    assert packet["code"]["sources"]["main.py"].startswith("import os")


def test_service_sends_packet_tools_and_records_metrics(tmp_path):
    from brain.knowledge import KnowledgeLibrary, KnowledgeRouter
    library = KnowledgeLibrary(tmp_path / "k.sqlite3")
    library.import_source("practices", str(knowledge_source(tmp_path / "src")))
    seen = {}

    class Observer(SequenceModel):
        totals = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0}

        async def propose(self, root, goal, memories, repository_context=None):
            from brain.capabilities import repository_capabilities
            registry = repository_capabilities(root)
            seen["tools"] = [item["function"]["name"] for item in registry.schemas()]
            seen["found"] = registry.invoke("find_symbol", {"name": "Engine"})
            seen["skill"] = registry.invoke("read_skill", {"name": "systematic-debugging"})
            seen["context"] = repository_context
            self.totals["calls"] += 1
            self.totals["output_tokens"] += 42
            return await super().propose(root, goal, memories, repository_context)
    repository = make_repository(tmp_path / "repos" / "demo")
    brain = Brain(repository.parent, tmp_path / "data", Observer(["x = 2\n"]), "img",
                  knowledge=KnowledgeRouter(library))
    task = asyncio.run(create_direct(brain, "Fix the Engine run method"))
    assert {"find_symbol", "find_references", "search_knowledge", "read_skill"} <= set(seen["tools"])
    assert "main.py:3 class Engine" in seen["found"] and "Reproduce" in seen["skill"]
    assert "engineering_packet" in seen["context"] and isinstance(seen["context"]["complexity"], int)
    assert task["metrics"]["output_tokens"] == 42 and task["metrics"]["calls"] == 1
    assert any(event["kind"] == "knowledge_packet" for event in task["events"])
    from brain.capabilities import repository_capabilities
    assert "read_skill" not in [item["function"]["name"] for item in repository_capabilities(repository).schemas()]


def test_failing_knowledge_tool_is_reported_to_model_not_fatal(tmp_path, monkeypatch):
    from brain.capabilities import TASK_CAPABILITIES, read_only
    from brain.openai_compatible import OpenAICompatibleModel
    sent = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json, headers):
            sent.append(json["messages"][-1])
            if len(sent) == 1:
                return Response({"choices": [{"message": {"content": "", "tool_calls": [{
                    "id": "t", "function": {"name": "find_symbol", "arguments": '{"name": "a"}'}}]}}]})
            return Response({"choices": [{"message": {"content": '{"plan": "p", "changes": []}'}}]})
    monkeypatch.setattr("brain.openai_compatible.httpx.AsyncClient", Client)

    def broken(arguments):
        raise OSError("index unavailable")
    token = TASK_CAPABILITIES.set((read_only("find_symbol", "x", {"name": {"type": "string"}}, broken),))
    try:
        result = asyncio.run(OpenAICompatibleModel("http://localhost:1/v1", "m").propose(tmp_path, "g", []))
    finally:
        TASK_CAPABILITIES.reset(token)
    assert json.loads(result)["plan"] == "p"
    assert sent[1] == {"role": "tool", "tool_call_id": "t", "content": "Tool denied: index unavailable"}


def test_per_directory_licenses_skip_proprietary_and_unlicensed_skills(tmp_path):
    from brain.knowledge import KnowledgeLibrary, intents
    root = tmp_path / "skills-repo"
    for name, license_text in [("open", "Apache License\nVersion 2.0"),
                               ("closed", "(c) Vendor. All rights reserved."), ("bare", None)]:
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: debugging skill {name}\n---\nSteps for debugging.\n")
        if license_text:
            (root / "skills" / name / "LICENSE.txt").write_text(license_text)
    library = KnowledgeLibrary(tmp_path / "k.sqlite3")
    result = library.import_source("mixed", str(root))
    assert result["documents"] == 1 and result["license"] == "Apache-2.0"
    assert sorted(result["skipped_unlicensed"]) == ["skills/bare/SKILL.md", "skills/closed/SKILL.md"]
    assert [hit["name"] for hit in library.search("debugging", 5)] == ["open"]
    assert intents("Fix the failing login test") == ["systematic debugging root cause", "test driven development"]


def test_mcp_gateway_exposes_only_tools_relevant_to_the_task():
    from brain.capabilities import TASK_QUERY
    from brain.mcp_gateway import MCPGateway, select_tools
    from tests.test_phase4 import FakeClient
    schemas = [{"type": "function", "function": {"name": f"mcp__s__{name}", "description": text,
                                                 "parameters": {}}}
               for name, text in [("find_symbol", "Find a code symbol by name"),
                                  ("query_database", "Run a read-only SQL query"),
                                  ("search_docs", "Search product documentation")]]
    assert select_tools(schemas, "", 8) == schemas
    chosen = select_tools(schemas, "Fix the symbol lookup in the parser", 8)
    assert [item["function"]["name"] for item in chosen] == ["mcp__s__find_symbol"]
    assert select_tools(schemas, "unrelated goal about colors", 8) == []
    gateway = MCPGateway({"docs": {"url": "http://127.0.0.1:9000/mcp",
                                   "tools": {"search": "read_only", "write": "approval_required"}}},
                         client_factory=FakeClient)
    assert len(asyncio.run(gateway.schemas())) == 2
    token = TASK_QUERY.set("search for the token rotation code")
    try:
        names = [item["function"]["name"] for item in asyncio.run(gateway.schemas())]
    finally:
        TASK_QUERY.reset(token)
    assert names == ["mcp__docs__search"]


def test_structural_search_is_read_only_and_bounded(tmp_path):
    from brain.knowledge import structural_search_tool
    tool = structural_search_tool(tmp_path)
    if tool is None:
        pytest.skip("ast-grep is not installed")
    (tmp_path / "a.py").write_text("def f(t):\n    return len(t.split(' '))\n")
    before = (tmp_path / "a.py").read_text()
    assert tool.handler({"pattern": "len($X.split($$$))", "language": "python"}) == \
        "a.py:2: return len(t.split(' '))"
    assert (tmp_path / "a.py").read_text() == before
    with pytest.raises(ValueError, match="language"):
        tool.handler({"pattern": "x", "language": "bash"})
    assert not tool.mutating and not tool.approval_required


def test_compare_replays_identical_cases_with_knowledge_off_and_on(tmp_path, monkeypatch):
    from brain.benchmark import BenchmarkRunner
    from brain.knowledge import KnowledgeRouter
    monkeypatch.setattr("brain.service.run_tests",
                        lambda workspace, *a, **k: {"passed": (workspace / "main.py").read_text() == "x = 2\n",
                                                    "exit_code": 1, "output": "FAILED"})
    repository = make_repository(tmp_path / "repos" / "demo")
    brain = Brain(repository.parent, tmp_path / "data", SequenceModel(["x = 2\n"]), "img",
                  knowledge=KnowledgeRouter())
    suite = {"name": "s", "cases": [{"name": "c", "repository": "demo", "goal": "Set x to 2",
                                     "expected_paths": ["main.py"]}]}
    result = asyncio.run(BenchmarkRunner(brain).compare(suite, repeat=2))
    assert [run["variant"] for run in result["runs"]] == ["knowledge_off", "knowledge_on"] * 2
    assert result["summary"]["knowledge_on"]["success_rate"] == 1.0
    assert result["runs"][0]["packet_chars"] == 0 and result["runs"][1]["packet_chars"] > 0
    assert brain.knowledge is not None
