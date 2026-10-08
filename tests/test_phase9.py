"""Live web and API verification, exercised with recorded HTTP fixtures (no network)."""
import asyncio
import json
import shutil
import time
from pathlib import Path
import httpx
import pytest
from brain import api_intelligence as api
from brain.service import Brain
from brain.validators import ProposalInvalid
from brain.web_intelligence import WebIntelligence
from brain.web_verification import FixtureTransport, WebBroker, WebCache, WebPolicy, WebPolicyError
from tests.test_brain import create_direct, make_repository

PUBLIC = {"docs.example.com": ["93.184.215.14"], "api.example.com": ["93.184.215.15"],
          "pypi.org": ["151.101.0.223"], "internal.example.com": ["10.0.0.7"],
          "rebind.example.com": ["93.184.215.20", "127.0.0.1"]}

SPEC_V1 = {"openapi": "3.0.3", "info": {"title": "Items", "version": "1.0"},
           "servers": [{"url": "https://api.example.com/v1"}],
           "paths": {"/items": {"get": {"operationId": "listItems", "summary": "List items",
                                        "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                                        "responses": {"200": {"description": "ok", "content": {"application/json": {
                                            "schema": {"$ref": "#/components/schemas/ItemList"}}}}}}},
                     "/items/{id}": {"get": {"operationId": "getItem", "summary": "Get one item",
                                             "responses": {"200": {"description": "ok"}}},
                                     "delete": {"operationId": "deleteItem", "responses": {"204": {"description": "gone"}}}}},
           "components": {"schemas": {
               "Item": {"type": "object", "required": ["id", "name"],
                        "properties": {"id": {"type": "integer"}, "name": {"type": "string"},
                                       "updated_at": {"type": "string", "nullable": True}}},
               "ItemList": {"type": "object", "required": ["items"],
                            "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                                           "next": {"type": "string"}}}}}}


def spec_v2():
    spec = json.loads(json.dumps(SPEC_V1))
    spec["info"]["version"] = "2.0"
    del spec["paths"]["/items/{id}"]["delete"]
    spec["paths"]["/items"]["get"]["parameters"].append({"name": "tenant", "in": "query", "required": True,
                                                         "schema": {"type": "string"}})
    del spec["components"]["schemas"]["ItemList"]["properties"]["next"]
    return spec


class Server:
    """A recorded web: route table of URL -> (status, headers, body); counts requests."""
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, request: httpx.Request):
        self.requests.append((request.method, str(request.url), dict(request.headers)))
        status, headers, body = self.routes.get(str(request.url), (404, {}, "not found"))
        if request.headers.get("if-none-match") and request.headers["if-none-match"] == headers.get("etag"):
            return httpx.Response(304, headers=headers)
        content = json.dumps(body) if isinstance(body, (dict, list)) else body
        return httpx.Response(status, headers=headers, content=content.encode())


def broker(tmp_path, routes, allow=("*.example.com", "pypi.org"), **policy):
    server = Server(routes)
    instance = WebBroker(WebPolicy(list(allow), min_interval=0, resolver=lambda host: PUBLIC.get(host, ["93.184.215.99"]),
                                   **policy), WebCache(tmp_path / "web.sqlite3"),
                         transport=httpx.MockTransport(server))
    return instance, server


ROBOTS = {"https://docs.example.com/robots.txt": (200, {}, "User-agent: *\nDisallow: /private/\n"),
          "https://api.example.com/robots.txt": (404, {}, ""), "https://pypi.org/robots.txt": (404, {}, "")}


def test_internal_addresses_side_effects_and_unlisted_hosts_are_refused(tmp_path):
    instance, server = broker(tmp_path, {
        **ROBOTS, "https://docs.example.com/hop": (302, {"location": "https://internal.example.com/admin"}, "")})
    refused = ["http://docs.example.com/", "https://127.0.0.1/", "https://[::1]/", "https://10.1.2.3/",
               "https://169.254.169.254/latest/meta-data/", "https://metadata.google.internal/",
               "https://localhost/", "https://internal.example.com/", "https://rebind.example.com/",
               "https://user:secret@docs.example.com/", "https://docs.example.com:8443/", "https://evil.test/"]
    for url in refused:
        with pytest.raises(WebPolicyError):
            asyncio.run(instance.check(url))
    with pytest.raises(WebPolicyError, match="side effects"):
        asyncio.run(instance.fetch("https://docs.example.com/x", method="POST"))
    # A redirect to an internal host is refused at the hop, after the first request only.
    with pytest.raises(WebPolicyError, match="non-public"):
        asyncio.run(instance.check("https://docs.example.com/hop"))
    assert [url for _, url, _ in server.requests] == ["https://docs.example.com/robots.txt",
                                                      "https://docs.example.com/hop"]
    assert all("authorization" not in headers and "cookie" not in headers for _, _, headers in server.requests)
    assert instance.stats["refused"] == 13
    evidence = asyncio.run(instance.check("https://docs.example.com/private/notes"))
    assert evidence.outcome == "inconclusive" and "robots" in evidence.reason


def test_moved_and_removed_urls_are_reported_not_substituted(tmp_path):
    instance, _ = broker(tmp_path, {
        **ROBOTS,
        "https://docs.example.com/v1/auth": (301, {"location": "/v2/auth"}, ""),
        "https://docs.example.com/v2/auth": (200, {"content-type": "text/html"}, "<h1>Auth v2</h1>"),
        "https://docs.example.com/v1/old": (410, {}, "gone")})
    moved = asyncio.run(instance.check("https://docs.example.com/v1/auth"))
    assert moved.outcome == "verified" and moved.final_url == "https://docs.example.com/v2/auth"
    assert moved.redirects == [{"from": "https://docs.example.com/v1/auth", "status": 301}]
    assert "Moved" in moved.reason
    removed = asyncio.run(instance.check("https://docs.example.com/v1/old"))
    assert removed.outcome == "failed" and removed.status == 410
    missing = asyncio.run(instance.check("https://docs.example.com/nowhere"))
    assert missing.outcome == "failed" and missing.final_url == "https://docs.example.com/nowhere"


def test_cached_evidence_is_reused_and_revalidated_with_etag(tmp_path):
    instance, server = broker(tmp_path, {
        **ROBOTS, "https://docs.example.com/guide": (200, {"etag": '"v1"', "content-type": "text/html"},
                                                     "<title>Guide</title><nav>menu</nav><h2>Install</h2>"
                                                     "<p>pip install thing</p><script>x()</script>")})
    first = asyncio.run(instance.text("https://docs.example.com/guide"))
    assert first["title"] == "Guide" and "pip install thing" in first["text"]
    assert "menu" not in first["text"] and "x()" not in first["text"]
    requests = len(server.requests)
    again = asyncio.run(instance.check("https://docs.example.com/guide"))
    assert again.from_cache and len(server.requests) == requests
    stale = asyncio.run(instance.fetch("https://docs.example.com/guide", max_age=0))[0]
    assert stale.from_cache and stale.sha256 == again.sha256
    assert server.requests[-1][2]["if-none-match"] == '"v1"'
    # A fresh hit costs nothing; revalidation costs one conditional request answered with 304.
    assert instance.stats["cache_hits"] == 1 and len(server.requests) == requests + 1


def test_size_limit_makes_evidence_inconclusive(tmp_path):
    instance, _ = broker(tmp_path, {**ROBOTS, "https://docs.example.com/big": (200, {}, "x" * 5000)},
                         max_bytes=1000)
    evidence = asyncio.run(instance.check("https://docs.example.com/big"))
    assert evidence.outcome == "inconclusive" and "size" in evidence.reason


def test_openapi_change_detection_and_endpoint_usage(tmp_path):
    routes = {**ROBOTS, "https://api.example.com/openapi.json": (200, {"content-type": "application/json"}, SPEC_V1)}
    instance, _ = broker(tmp_path, routes)
    tracker = api.SpecTracker(tmp_path / "specs.sqlite3")
    first = asyncio.run(tracker.check(instance, "https://api.example.com/openapi.json"))
    assert first["first_snapshot"]
    routes["https://api.example.com/openapi.json"] = (200, {"content-type": "application/json"}, spec_v2())
    second = asyncio.run(tracker.check(instance, "https://api.example.com/openapi.json"))
    assert second["changed"] is True
    diff = second["diff"]
    assert diff["removed"] == ["DELETE /items/{id}"]
    notes = {item["endpoint"]: item["changes"] for item in diff["changed"]}
    assert "new required parameter: tenant" in notes["GET /items"]
    assert "response field removed: next" in notes["GET /items"]
    assert diff["version"] == ["1.0", "2.0"]
    required = json.loads(json.dumps(SPEC_V1))
    required["paths"]["/items"]["get"]["parameters"][0]["required"] = True
    assert api.diff_specs(SPEC_V1, required)["changed"] == [
        {"endpoint": "GET /items", "changes": ["parameter became required: limit"]}]
    usage = api.verify_endpoints(spec_v2(), [("GET", "/v1/items"), ("DELETE", "/v1/items/42"),
                                             ("GET", "https://api.example.com/v1/items/7"), ("GET", "/v1/orders")])
    assert [item["outcome"] for item in usage] == ["verified", "failed", "verified", "failed"]
    assert "not for DELETE" in usage[1]["reason"] and "moved or removed" in usage[3]["reason"]
    summary = api.summarize(SPEC_V1, "list items with a limit", 1)
    assert summary["endpoints"][0]["operation_id"] == "listItems"
    assert summary["endpoints"][0]["response_fields"] == ["items", "next"]


def test_discovery_tries_well_known_locations(tmp_path):
    instance, server = broker(tmp_path, {**ROBOTS, "https://api.example.com/v3/api-docs": (200, {}, SPEC_V1)})
    spec, evidence = asyncio.run(api.discover_openapi(instance, "https://api.example.com/docs"))
    assert spec["info"]["title"] == "Items" and evidence[-1]["url"].endswith("/v3/api-docs")
    assert all(method == "GET" for method, _, _ in server.requests)


def test_malformed_responses_and_integrity_violations_are_reported():
    good = {"items": [{"id": 1, "name": "a", "updated_at": None}]}
    assert api.validate_response(SPEC_V1, "GET", "/items", 200, good)["outcome"] == "verified"
    bad = api.validate_response(SPEC_V1, "GET", "/items", 200, {"items": [{"id": "1"}]})
    assert bad["outcome"] == "failed"
    assert any("'name' is a required property" in error for error in bad["errors"])
    assert any("items/0/id" in error for error in bad["errors"])
    assert api.validate_response(SPEC_V1, "GET", "/items", 500, good)["outcome"] == "failed"
    assert api.validate_response(SPEC_V1, "GET", "/items/3", 200, {})["outcome"] == "inconclusive"
    now = time.time()
    records = [{"id": 1, "name": "a", "updated_at": now - 10}, {"id": 1, "name": None, "updated_at": now - 99999},
               {"id": 2, "name": "c", "updated_at": "not-a-date"}]
    report = api.integrity(records, unique=["id"], not_null=["name"], max_age={"updated_at": 3600}, now=now,
                           schema={"type": "object", "properties": {"id": {"type": "integer"}}})
    assert report["outcome"] == "failed"
    text = " | ".join(report["issues"])
    assert "duplicate id=1" in text and "name is null" in text and "stale" in text and "not a timestamp" in text


def test_outdated_sdk_usage_is_identified_against_the_registry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("httpx==0.27.2\nrequests>=2\n")
    (repo / "client.py").write_text("import httpx\n\nclient = httpx.Client(proxies={'https://': 'http://p'})\n")
    (repo / "plain.py").write_text("proxies = {}\n")  # no httpx import: not flagged
    instance, _ = broker(tmp_path, {**ROBOTS, "https://pypi.org/pypi/httpx/json": (200, {}, {"info": {"version": "0.28.1"}})})
    findings = asyncio.run(api.sdk_findings(instance, repo))
    assert len(findings) == 1
    finding = findings[0]
    assert finding["locations"] == ["client.py:3"] and finding["latest"] == "0.28.1"
    assert finding["outcome"] == "failed" and finding["pinned_below_change"] is True
    assert "proxy=" in finding["replacement"]
    assert api.declared_dependencies(repo)["pypi"] == {"httpx": "==0.27.2", "requests": ">=2"}


def test_import_path_gate_rejects_modules_that_do_not_exist(tmp_path):
    from tests.test_phase8 import SequenceModel
    repository = make_repository(tmp_path / "repos" / "demo")
    (repository / "pkg").mkdir()
    (repository / "pkg" / "__init__.py").write_text("")
    content = "from pkg.helpers import tidy\n\nx = tidy(2)\n"
    brain = Brain(repository.parent, tmp_path / "data", SequenceModel([content]), "img", validation_retries=0)
    with pytest.raises(ProposalInvalid, match="Import path"):
        asyncio.run(create_direct(brain))
    ok = Brain(repository.parent, tmp_path / "data2", SequenceModel(["import os\nimport pkg\n\nx = 2\n"]), "img")
    assert asyncio.run(create_direct(ok))["status"] == "proposed"


def web_brain(tmp_path, routes, contents):
    from tests.test_phase8 import SequenceModel
    repository = make_repository(tmp_path / "repos" / "demo")
    (repository / "requirements.txt").write_text("httpx==0.27.2\n")
    (repository / "client.py").write_text("import httpx\n\nc = httpx.Client(proxies={})\n\n"
                                          "def items(c):\n    return c.get('/v1/items/{x}')\n")
    instance, server = broker(tmp_path, routes)
    web = WebIntelligence(instance, api.SpecTracker(tmp_path / "specs.sqlite3"))
    return Brain(repository.parent, tmp_path / "data", SequenceModel(contents), "img", web=web), server


def test_preflight_supplies_verified_contract_and_read_only_tools(tmp_path):
    routes = {**ROBOTS, "https://api.example.com/openapi.json": (200, {}, SPEC_V1),
              "https://pypi.org/pypi/httpx/json": (200, {}, {"info": {"version": "0.28.1"}}),
              "https://docs.example.com/gone": (404, {}, "")}
    seen = {}
    from tests.test_phase8 import SequenceModel

    class Observer(SequenceModel):
        async def propose(self, root, goal, memories, repository_context=None):
            from brain.capabilities import repository_capabilities
            seen["context"] = repository_context
            registry = repository_capabilities(root)
            seen["tools"] = [item["function"]["name"] for item in registry.schemas()]
            seen["fetch"] = json.loads(registry.invoke("check_url", {"url": "https://docs.example.com/gone"}))
            with pytest.raises(WebPolicyError):
                registry.invoke("web_fetch", {"url": "https://10.0.0.1/"})
            return await super().propose(root, goal, memories, repository_context)
    brain, server = web_brain(tmp_path, routes, ["x = 2\n"])
    brain.model = Observer(["x = 2\n"])
    task = asyncio.run(create_direct(brain, "Use the httpx client to call https://api.example.com/openapi.json "
                                            "endpoints, docs at https://docs.example.com/gone"))
    external = seen["context"]["verified_external"]
    outcomes = {check.get("url") or check.get("package"): check["outcome"] for check in external["checks"]}
    assert outcomes == {"https://api.example.com/openapi.json": "verified",
                        "https://docs.example.com/gone": "failed", "httpx": "verified"}
    assert external["sdk_findings"][0]["outcome"] == "failed"
    assert external["endpoint_usage"][0]["outcome"] == "verified"
    assert "do not guess" in external["notice"]
    assert {"web_fetch", "check_url", "package_info", "api_endpoints"} <= set(seen["tools"])
    assert seen["fetch"]["outcome"] == "failed"
    requests = len(server.requests)
    assert task["metrics"]["web_network_requests"] > 0
    brain.model.contents = ["x = 3\n"]
    asyncio.run(brain.plan(task, brain.workspace(task["id"]), task["goal"]))
    # The preflight runs once per task and the tool's evidence comes from cache: no new requests.
    assert len(server.requests) == requests


def test_upstream_check_runs_before_escalation(tmp_path, monkeypatch):
    routes = {**ROBOTS, "https://pypi.org/pypi/httpx/json": (200, {}, {"info": {"version": "0.28.1"}}),
              "https://pypi.org/pypi/leftpadx/json": (404, {}, {})}
    brain, _ = web_brain(tmp_path, routes, ["x = 'bad'\n", "x = 'worse'\n", "x = 2\n"])
    monkeypatch.setattr("brain.service.run_tests", lambda workspace, *a, **k: {
        "passed": (workspace / "main.py").read_text() == "x = 2\n", "exit_code": 1,
        "output": "E   ModuleNotFoundError: No module named 'leftpadx'\nFAILED test_main.py::test_ok"})
    task = asyncio.run(create_direct(brain))
    task = asyncio.run(brain.execute(task["id"], task["digest"]))
    assert task["status"] == "passed"
    upstream = task["failure_log"][0]["upstream"]
    module = next(item for item in upstream if item["kind"] == "module")
    assert module["module"] == "leftpadx" and module["outcome"] == "failed"
    assert any(item["kind"] == "sdk" and item["package"] == "httpx" for item in upstream)
    assert "Upstream verification" in brain.model.goals[1]


def test_fixture_transport_replays_deterministically(tmp_path):
    directory = tmp_path / "fixtures"
    directory.mkdir()
    transport = FixtureTransport(directory)
    request = httpx.Request("GET", "https://docs.example.com/page")
    (directory / (transport._path(request).name)).write_text(json.dumps(
        {"url": str(request.url), "status": 200, "headers": {"content-type": "text/plain"}, "body": "recorded"}))
    instance = WebBroker(WebPolicy(["docs.example.com"], min_interval=0, respect_robots=False,
                                   resolver=lambda host: ["93.184.215.14"]),
                         WebCache(tmp_path / "w.sqlite3"), transport=transport)
    evidence, body = asyncio.run(instance.fetch("https://docs.example.com/page"))
    assert evidence.outcome == "verified" and body == b"recorded"
    missing = asyncio.run(instance.check("https://docs.example.com/other"))
    assert missing.outcome == "inconclusive" and missing.status == 599


@pytest.mark.skipif(not (shutil.which("chromium") or Path("/opt/pw-browsers/chromium").exists()),
                    reason="no Chromium available")
def test_browser_inspects_javascript_rendered_page_and_blocks_internal_requests():
    from brain.browser import BrowserInspector
    page = ("<title>API Reference</title><div id=app>Loading</div><script>"
            "fetch('/api/endpoints.json').then(r => r.json()).then(d => {"
            "document.getElementById('app').textContent = d.map(e => e.method + ' ' + e.path).join(', ');"
            "console.error('deprecated: /v1/items');});"
            "fetch('https://169.254.169.254/latest/meta-data').catch(() => {});</script>")
    fixtures = {"https://docs.example.com/reference": (200, "text/html", page),
                "https://docs.example.com/api/endpoints.json": (200, "application/json",
                                                                json.dumps([{"method": "GET", "path": "/v2/items"}]))}
    inspector = BrowserInspector(WebPolicy(["docs.example.com"], resolver=lambda host: ["93.184.215.14"]),
                                 executable="/opt/pw-browsers/chromium", fixtures=fixtures)
    result = asyncio.run(inspector.inspect("https://docs.example.com/reference", settle_ms=800))
    assert result["evidence"]["outcome"] == "verified" and result["title"] == "API Reference"
    assert "GET /v2/items" in result["text"]
    assert any("deprecated: /v1/items" in line for line in result["console"])
    assert result["refused_requests"] == ["https://169.254.169.254/latest/meta-data"]
    with pytest.raises(WebPolicyError):
        asyncio.run(inspector.inspect("https://127.0.0.1/"))


def test_reviewer_sees_verified_facts_and_cannot_veto_passing_tests(tmp_path, monkeypatch):
    from tests.test_phase8 import SequenceModel
    routes = {**ROBOTS, "https://pypi.org/pypi/httpx/json": (200, {}, {"info": {"version": "0.28.1"}})}
    brain, _ = web_brain(tmp_path, routes, ["x = 2\n"])
    reviewed = []

    async def stale_reviewer(goal, diff):
        reviewed.append(goal)
        return {"approved": False, "reason": "proxies= is the correct httpx argument"}
    brain.model.review = stale_reviewer
    monkeypatch.setattr("brain.service.run_tests", lambda *a, **k: {"passed": True, "exit_code": 0, "output": "ok"})
    task = asyncio.run(create_direct(brain))
    task = asyncio.run(brain.execute(task["id"], task["digest"]))
    assert "Live-verified facts" in reviewed[0] and "proxy=" in reviewed[0]
    assert task["status"] == "passed" and task["review_disputed"] is True
    assert any(event["kind"] == "review_overruled_by_tests" for event in task["events"])
    gate, _ = web_brain(tmp_path / "gate", routes, ["x = 2\n", "x = 3\n", "x = 4\n"])
    gate.model.review, gate.review_mode = stale_reviewer, "gate"
    task = asyncio.run(create_direct(gate))
    assert asyncio.run(gate.execute(task["id"], task["digest"]))["status"] == "failed"
