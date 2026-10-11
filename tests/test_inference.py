"""Measuring simultaneous generation: the probe against a model server that answers one request at
a time and one that answers several at once, the remembered measurement, and agent slots."""
import argparse
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brain.conductor import Resources
from brain.local import inference


def fake_server(parallel: int, tokens_per_second: float = 200.0):
    """An Ollama-shaped server: /api/version and streaming /api/chat. At most `parallel`
    generations run at once; the rest wait, as Ollama queues beyond OLLAMA_NUM_PARALLEL."""
    gate = threading.Semaphore(parallel)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"version": "9.9.9"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            with gate:
                for _ in range(request["options"]["num_predict"]):
                    time.sleep(1 / tokens_per_second)
                    self.wfile.write(b'{"message":{"content":"x"},"done":false}\n')
                    self.wfile.flush()
                self.wfile.write(b'{"done":true}\n')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.mark.parametrize("parallel, expected", [(1, 1), (2, 2)])
def test_the_probe_tells_queued_from_simultaneous_generation(parallel, expected):
    server, url = fake_server(parallel)
    try:
        result = asyncio.run(inference.probe(url, "m", streams=2, num_predict=40))
    finally:
        server.shutdown()
    assert result["concurrent"] is (parallel > 1)
    assert (result["overlap"] > 0.8) if parallel > 1 else (result["overlap"] < 0.2)
    assert inference.parallel_slots([result]) == expected
    if parallel == 1:  # the second request waited for the first one to finish
        assert result["first_token_seconds"][1] > 0.15


def test_overlap_and_slots_from_recorded_results():
    serial = [{"first": 0.0, "last": 1.0}, {"first": 1.0, "last": 2.0}]
    together = [{"first": 0.0, "last": 1.0}, {"first": 0.1, "last": 1.1}]
    assert inference.overlap(serial) == 0.0 and inference.overlap(together) > 0.8
    slow = {"streams": 4, "concurrent": True, "aggregate_tokens_per_second": 5.0, "single_tokens_per_second": 8.0}
    fast = {"streams": 2, "concurrent": True, "aggregate_tokens_per_second": 8.4, "single_tokens_per_second": 8.5}
    # simultaneous but slower in total than one alone: more agents would not help
    assert inference.parallel_slots([slow]) == 1 and inference.parallel_slots([fast, slow]) == 2


def test_the_measurement_is_remembered_per_model_and_server_version(tmp_path):
    server, url = fake_server(2)
    cache = tmp_path / "inference.json"
    try:
        entry = inference.calibrate(cache, url, "m", streams=2, num_predict=30)
        assert entry["slots"] == 2 and entry["version"] == "9.9.9"
        assert inference.cached_slots(cache, url, "m") == 2
        assert inference.cached_slots(cache, url, "other") is None
    finally:
        server.shutdown()
    data = json.loads(cache.read_text())
    data[f"{url}|m"]["version"] = "1.0.0"  # Ollama was updated since
    cache.write_text(json.dumps(data))
    assert inference.cached_slots(cache, url, "m", check_server=False) == 2  # status views never contact it
    data[f"{url}|m"]["at"] -= inference.MAX_AGE + 1  # a week old: settings may have changed
    cache.write_text(json.dumps(data))
    assert inference.cached_slots(cache, url, "m", check_server=False) is None
    cache.write_text("not json")
    assert inference.cached_slots(cache, url, "m", check_server=False) is None


def test_agent_slots_never_exceed_what_the_model_server_runs_at_once():
    facts = lambda: {"ram": 64e9, "cores": 16}  # noqa: E731
    assert Resources(facts, max_local=4).local_slots() == 4
    assert Resources(facts, max_local=4, inference=1).local_slots() == 1
    assert Resources(facts, max_local=4, inference=2).local_slots() == 2
    assert Resources(lambda: {"ram": 8e9, "cores": 16}, max_local=4, inference=4).local_slots() == 1  # memory


def test_backlog_run_measures_once_and_explains_a_one_at_a_time_server(tmp_path, monkeypatch, capsys):
    from brain.local import cli
    from brain.local import config as settings
    from brain.local.paths import Layout
    monkeypatch.setattr(os, "environ", dict(os.environ))
    layout = Layout(tmp_path / "home").ensure()
    config = settings.load(layout)
    config["models"]["model"] = "qwen"
    settings.save(layout, config)
    project = tmp_path / "project"
    project.mkdir()
    context = cli.Context(layout, project)
    monkeypatch.setattr("brain.local.system.System.facts", lambda self: {"ram": 64e9, "cores": 16})
    calls = []

    def calibrate(cache, url, model, streams, num_predict=64):
        calls.append(streams)
        entry = {"slots": 1, "version": "v", "at": time.time(), "result": {"overlap": 0.0, "aggregate_tokens_per_second": 4,
                                                                          "single_tokens_per_second": 8}}
        cache.write_text(json.dumps({f"{url}|{model}": entry}))
        return entry
    monkeypatch.setattr(inference, "calibrate", calibrate)
    monkeypatch.setattr(inference, "_server_version", lambda url: "v")
    args = argparse.Namespace(max_agents=4, remeasure=False)
    assert cli._backlog_resources(args, context, measure=True).local_slots() == 1
    out = capsys.readouterr().out
    assert calls == [4] and "OLLAMA_NUM_PARALLEL=4" in out
    assert cli._backlog_resources(args, context, measure=True).local_slots() == 1 and calls == [4]  # remembered
    assert cli._backlog_resources(argparse.Namespace(max_agents=4, remeasure=True), context, measure=True)
    assert calls == [4, 4]
    assert cli._backlog_resources(args, context).local_slots() == 1  # status: read only, never measures
    assert calls == [4, 4]


@pytest.mark.skipif(not os.environ.get("CODINGBRAIN_TEST_CODING_MODEL"), reason="needs a real Ollama model")
def test_real_ollama_simultaneous_generation_is_measured():
    """Reported, not asserted: whether this server generates for two agents at once depends on
    OLLAMA_NUM_PARALLEL and the machine. The probe must finish and be consistent."""
    url = os.environ.get("CODINGBRAIN_TEST_OLLAMA_URL", "http://localhost:11434")
    result = asyncio.run(inference.probe(url, os.environ["CODINGBRAIN_TEST_CODING_MODEL"], streams=2, num_predict=48))
    print(json.dumps(result, indent=2))
    if os.environ.get("CODINGBRAIN_INFERENCE_REPORT"):
        with open(os.environ["CODINGBRAIN_INFERENCE_REPORT"], "w") as report:
            json.dump(result, report, indent=2)
    assert result["single_tokens_per_second"] > 0 and len(result["first_token_seconds"]) == 2
    assert result["concurrent"] == (result["overlap"] >= 0.5)
