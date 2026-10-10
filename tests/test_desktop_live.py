"""Live desktop behavior: heartbeat, engine-switch detection and bridge supervision."""
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

from fastapi.testclient import TestClient

from desktop_ui import server
from desktop_ui.native import BridgeProcess, source_root
from desktop_ui.server import Workspace, create_app


def install(home: Path, version: str):
    current = home / ".local" / "CodingBrain" / "app" / "current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(json.dumps({"version": version}), encoding="utf-8")


def test_health_is_cheap_authenticated_and_reports_installed_version(tmp_path, monkeypatch):
    install(tmp_path, "0.11.0")
    workspace = Workspace(home=tmp_path, executable="not-a-real-binary")
    # A heartbeat must never start the engine or run a probe.
    monkeypatch.setattr(workspace, "engine_status", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    client = TestClient(create_app(workspace))
    assert client.get("/api/health").status_code == 403
    health = client.get("/api/health", headers={"X-CodingBrain-Token": workspace.secret}).json()
    assert health == {"ok": True, "boot": workspace.boot, "installed_version": "0.11.0", "active": None}


def test_bridge_restart_changes_boot_id(tmp_path):
    assert Workspace(home=tmp_path).boot != Workspace(home=tmp_path).boot


def test_engine_status_cache_is_invalidated_when_the_installed_version_switches(tmp_path, monkeypatch):
    install(tmp_path, "0.9.0")
    workspace = Workspace(home=tmp_path, executable="not-a-real-binary")
    probes = []

    def capabilities(command):
        probes.append(workspace.installed_version())
        return {"available": True, "conversation": False, "version": workspace.installed_version(),
                "typed_api": False, "detail": "probe"}

    monkeypatch.setattr(server.management, "engine_capabilities", capabilities)
    monkeypatch.setattr(server.management, "configured_model", lambda path: {"provider": "ollama", "model": "m"})
    monkeypatch.setattr(server.management, "local_ollama_models", lambda: {"models": ["m"], "running": True, "detail": ""})
    assert workspace.engine_status()["engine"]["version"] == "0.9.0"
    assert workspace.engine_status()["engine"]["version"] == "0.9.0"
    assert probes == ["0.9.0"]  # second read came from the cache
    install(tmp_path, "0.11.0")  # an update switched the engine
    assert workspace.engine_status()["engine"]["version"] == "0.11.0"
    assert probes == ["0.9.0", "0.11.0"]


def _get_state(address):
    import httpx
    url = urlsplit(address)
    token = parse_qs(url.fragment)["token"][0]
    for _ in range(60):
        try:
            return httpx.get(f"http://127.0.0.1:{url.port}/api/health",
                             headers={"X-CodingBrain-Token": token}, timeout=.5)
        except httpx.ConnectError:
            time.sleep(.05)
    raise AssertionError("bridge never answered")


def test_watchdog_restarts_a_crashed_bridge_on_a_new_address():
    bridge = BridgeProcess(python=Path(sys.executable), root=source_root())
    restarted = threading.Event()
    addresses = []
    try:
        first = bridge.start(timeout=20)
        boot = _get_state(first).json()["boot"]
        bridge.watch(on_restart=lambda address: (addresses.append(address), restarted.set()),
                     on_failure=lambda reason: addresses.append("failed: " + reason), poll=.1)
        bridge.child.kill()  # simulate a crash
        assert restarted.wait(30), addresses
        assert addresses[0] != first
        assert _get_state(addresses[0]).json()["boot"] != boot
    finally:
        bridge.stop()


def test_watchdog_does_not_restart_after_an_intentional_stop():
    bridge = BridgeProcess(python=Path(sys.executable), root=source_root())
    events = []
    bridge.start(timeout=20)
    bridge.watch(on_restart=events.append, on_failure=events.append, poll=.05)
    bridge.stop()
    time.sleep(.4)
    assert events == [] and bridge.child is None


def test_repeated_crashes_are_reported_instead_of_restarting_forever(monkeypatch):
    bridge = BridgeProcess(python=Path(sys.executable), root=source_root())
    monkeypatch.setattr(bridge, "start", lambda timeout=30: "http://127.0.0.1:1/#token=x")
    for _ in range(BridgeProcess.MAX_RESTARTS):
        bridge.restart()
    try:
        bridge.restart()
    except RuntimeError as error:
        assert "stopped repeatedly" in str(error)
    else:
        raise AssertionError("expected the restart budget to run out")


def test_bridge_output_after_the_handshake_is_drained(tmp_path, monkeypatch):
    # A child that writes far more than a pipe buffer after its handshake must not block.
    script = tmp_path / "noisy.py"
    script.write_text(
        "import json,sys\n"
        "print('CBUI_READY '+json.dumps({'port':40000,'token':'t'*40,'pid':1}),flush=True)\n"
        "for i in range(20000):\n"
        "    sys.stderr.write('warning line %d\\n' % i); sys.stdout.write('log line %d\\n' % i)\n"
        "sys.stderr.flush(); sys.stdout.flush(); print('done', file=sys.stderr, flush=True)\n",
        encoding="utf-8")
    bridge = BridgeProcess(python=Path(sys.executable), root=source_root())
    import subprocess
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: real([sys.executable, str(script)], **kwargs))
    try:
        bridge.start(timeout=20)
        bridge.child.wait(timeout=30)  # would hang on a full pipe without the drain threads
        for _ in range(50):
            if bridge.stderr_tail and bridge.stderr_tail[-1].strip() == "done":
                break
            time.sleep(.05)
        assert bridge.stderr_tail[-1].strip() == "done"
    finally:
        bridge.stop()


def test_uploaded_attachment_is_staged_privately_and_checked(tmp_path):
    workspace = Workspace(home=tmp_path, executable="not-a-real-binary")
    client = TestClient(create_app(workspace))
    headers = {"X-CodingBrain-Token": workspace.secret, "X-Filename": "..%2F..%2Fscreen%20shot.png"}
    assert client.post("/api/attachments/upload", content=b"png", headers={"X-Filename": "a.png"}).status_code == 403
    staged = client.post("/api/attachments/upload", content=b"\x89PNG data", headers=headers).json()
    path = Path(staged["path"])
    assert path.name == "screen shot.png" and path.read_bytes() == b"\x89PNG data"
    assert workspace._staging() in path.parents  # never outside the private staging folder
    refused = client.post("/api/attachments/upload", content=b"KEY", headers={**headers, "X-Filename": "server.key"})
    assert refused.status_code == 400 and "secret" in refused.json()["detail"]
    assert client.post("/api/attachments/upload", content=b"x", headers={**headers, "X-Filename": "tool.exe"}).status_code == 400


def test_attachments_are_refused_for_chat_links_and_missing_files(tmp_path):
    from desktop_ui.server import StartRequest
    workspace = Workspace(home=tmp_path, executable="not-a-real-binary")
    spec = tmp_path / "spec.md"
    spec.write_text("# spec")
    import pytest
    with pytest.raises(ValueError, match="Run task and New project"):
        workspace.start(StartRequest(message="hi", mode="chat", attachments=[str(spec)]))
    with pytest.raises(ValueError, match="file not found"):
        workspace.check_attachment(str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="absolute"):
        workspace.check_attachment("spec.md")
    assert workspace.check_attachment(str(spec)) == spec.resolve()
