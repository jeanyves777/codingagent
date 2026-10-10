"""Native desktop bootstrap and typed core bridge tests (no subscription required)."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import types
from urllib.parse import urlsplit, parse_qs

import pytest
from fastapi.testclient import TestClient

from desktop_ui.native import BridgeProcess, installed_brain_python, main, source_root
from desktop_ui.core import wait_approval
from desktop_ui.server import Job, Workspace, create_app


def test_native_packaging_selftest(capsys):
    assert main(['--self-test']) == 0
    info = json.loads(capsys.readouterr().out)
    assert info['native_assets'] is True
    assert (Path(info['root']) / 'desktop_ui' / 'static' / 'index.html').is_file()


def test_native_python_override_rejected_if_missing(monkeypatch, tmp_path):
    monkeypatch.setenv('CODINGBRAIN_PYTHON', str(tmp_path / 'missing.exe'))
    with pytest.raises(RuntimeError, match='does not exist'):
        installed_brain_python()


def test_native_server_launch_and_shutdown():
    # End-to-end loopback handshake with the real uvicorn server on an ephemeral port.
    import httpx
    bridge = BridgeProcess(python=Path(sys.executable), root=source_root())
    try:
        address = bridge.start(timeout=20)
        url = urlsplit(address)
        assert url.hostname == '127.0.0.1'
        token = parse_qs(url.fragment)['token'][0]
        for attempt in range(40):
            try:
                response = httpx.get(f'http://127.0.0.1:{url.port}/api/state',
                                     headers={'X-CodingBrain-Token': token}, timeout=.5)
                break
            except httpx.ConnectError:
                import time
                time.sleep(.05)
        assert response.status_code == 200
        assert response.json()['project'] is None
        assert httpx.get(f'http://127.0.0.1:{url.port}/api/state').status_code == 403
    finally:
        child = bridge.child
        bridge.stop()
        assert child is not None and child.poll() is not None


@pytest.mark.asyncio
async def test_approval_requires_explicit_single_use_decision():
    j = Job('run', '/tmp/project', ['codingbrain', 'run', 'fix bug'])
    j.core = True
    # The engine API's task summary (tasks.start), as the desktop receives it.
    task = {'id': 'a' * 32, 'plan': 'Fix the bug', 'files': ['a.py'], 'digest': 'd' * 64,
            'diff': '--- a.py\n+++ a.py\n'}
    waiting = asyncio.create_task(wait_approval(j, 'proposal', task))
    for _ in range(100):
        if j.awaiting_approval:
            break
        await asyncio.sleep(.01)
    assert j.awaiting_approval and j.pending_kind == 'proposal'
    assert j.pending_payload['files'] == ['a.py']
    assert j.pending_payload['diff'].startswith('---')
    w = Workspace(executable='unused')
    w.jobs[j.id] = j
    with pytest.raises(ValueError, match='stale'):
        w.decide(j.id, True, approval_id='incorrect')
    w.decide(j.id, True, approval_id=j.pending_id)
    assert await waiting is True
    with pytest.raises(ValueError, match='No approval'):
        w.decide(j.id, True, approval_id=j.pending_id)


@pytest.mark.asyncio
async def test_stop_rejects_pending_approval():
    j = Job('run', '/tmp/project', ['codingbrain', 'run', 'fix bug'])
    j.core = True
    task = {'id': 'a' * 32, 'plan': 'Fix the bug', 'files': ['a.py'], 'digest': 'd' * 64}
    waiting = asyncio.create_task(wait_approval(j, 'proposal', task))
    for _ in range(100):
        if j.awaiting_approval:
            break
        await asyncio.sleep(.01)
    w = Workspace(executable='unused'); w.jobs[j.id] = j
    w.stop(j.id)
    assert await waiting is False
    assert not j.awaiting_approval


def test_no_browser_window_to_nonlocal_url(monkeypatch):
    fake = types.SimpleNamespace(create_window=lambda *a,**kw: pytest.fail('window must not open'), start=lambda **kw:None)
    monkeypatch.setitem(sys.modules, 'webview', fake)
    assert main(['--url', 'https://evil.example']) == 1
