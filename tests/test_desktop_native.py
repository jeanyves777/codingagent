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
from desktop_ui.core import wait_approval, execute_goal, chat
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
    task = {'id': 'a' * 32, 'proposal': {'plan': 'Fix the bug', 'changes': [{'path': 'a.py'}]},
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
    task = {'id': 'a' * 32, 'proposal': {'plan': 'Fix the bug', 'changes': [{'path': 'a.py'}]}}
    waiting = asyncio.create_task(wait_approval(j, 'proposal', task))
    for _ in range(100):
        if j.awaiting_approval:
            break
        await asyncio.sleep(.01)
    w = Workspace(executable='unused'); w.jobs[j.id] = j
    w.stop(j.id)
    assert await waiting is False
    assert not j.awaiting_approval


@pytest.mark.asyncio
async def test_real_core_api_flow_is_typed_and_never_changes_checkout(monkeypatch, tmp_path):
    # Exercise the real desktop core adapter against an isolated fake Brain API.
    # This tests proposal -> user approval -> execute -> tests -> accept -> branch.
    root = tmp_path / 'repo'; root.mkdir()
    calls = []
    class Store:
        def __init__(self): self.tasks = {}
        def get(self, name): return self.tasks[name]
    class Brain:
        def __init__(self): self.store = Store()
        def submit(self, repository, goal, launch):
            assert not launch
            task = {'id':'t'*32,'repository':repository,'goal':goal,'status':'waiting','events':[]}
            self.store.tasks[task['id']] = task; calls.append('submit'); return task
        async def create(self, task):
            task['status'] = 'proposed'; task['digest'] = 'digest1'
            task['proposal'] = {'plan':'Change function', 'changes':[{'path':'calc.py'}]}
            task['diff'] = '--- calc.py\n+++ calc.py\n'
            calls.append('create'); return task
        async def execute(self, task_id, digest):
            assert digest == 'digest1'
            t = self.store.get(task_id); t['status'] = 'passed'; t['test_evidence'] = {'passed':True}
            calls.append('execute'); return t
        async def accept(self, task_id, summary):
            t = self.store.get(task_id); t['status'] = 'accepted'; t['commit']='abc123'
            calls.append('accept'); return t
        def cancel(self, task_id):
            self.store.get(task_id)['status']='cancelled';calls.append('cancel')
    brain = Brain()
    class Context:
        def __init__(self, layout, start): self.root = Path(start); self.brain = brain
        def check(self): return None
    def branch_for(context, commit, label):
        assert commit == 'abc123'; calls.append('branch');return 'codingbrain/fix-calc'
    module_paths = types.ModuleType('brain.local.paths'); module_paths.Layout = types.SimpleNamespace(default=lambda: object())
    module_cli = types.ModuleType('brain.local.cli'); module_cli.Context=Context
    module_cli.branch_for=branch_for;module_cli.slug=lambda text:'fix-calc'
    pkg=types.ModuleType('brain');pkg.__path__=[]
    local=types.ModuleType('brain.local');local.__path__=[]
    for name, module in [('brain',pkg),('brain.local',local),('brain.local.cli',module_cli),('brain.local.paths',module_paths)]:
        monkeypatch.setitem(sys.modules,name,module)
    job = Job('run',str(root),['codingbrain','run','fix calculator']);job.core=True
    run = asyncio.create_task(execute_goal(job))
    for stage in range(2):
        for _ in range(100):
            if job.awaiting_approval and not job.decision_event.is_set(): break
            await asyncio.sleep(.01)
        assert job.awaiting_approval
        assert job.pending_kind == ['proposal','accept'][stage]
        job.decision_allow=True;job.decision_event.set()
        await asyncio.sleep(.05)
    assert await run == 'completed'
    assert calls == ['submit','create','execute','accept','branch']
    assert (root / 'calc.py').exists() is False  # core uses isolated worktrees
    assert any('codingbrain/fix-calc' in e['message'] for e in job.dump())


def test_no_browser_window_to_nonlocal_url(monkeypatch):
    fake = types.SimpleNamespace(create_window=lambda *a,**kw: pytest.fail('window must not open'), start=lambda **kw:None)
    monkeypatch.setitem(sys.modules, 'webview', fake)
    assert main(['--url', 'https://evil.example']) == 1
