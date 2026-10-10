"""Desktop readiness must not mistake the UI bridge for the installed brain or installed CLIs for models."""
from pathlib import Path
import json
import subprocess
from fastapi.testclient import TestClient

from desktop_ui import management
from desktop_ui.server import Workspace, create_app


class FakeResult:
    def __init__(self, code=0, output=""):
        self.returncode = code
        self.stdout = output


def test_engine_probe_reports_legacy_release_truthfully(monkeypatch):
    commands = []
    def fake_run(argv, **kwargs):
        commands.append(argv)
        if argv[-1] == 'version':
            return FakeResult(output='codingbrain 0.9.0\n')
        return FakeResult(2, 'usage: codingbrain [init,run,doctor]')
    monkeypatch.setattr(management.subprocess, 'run', fake_run)
    status = management.engine_capabilities(['fake-codingbrain'])
    assert status['available'] is True
    assert status['version'] == '0.9.0'
    assert status['conversation'] is False
    assert status['typed_api'] is False
    assert status['new_project'] is False
    assert status['task_command'] is True
    assert all('--yes' not in command for command in commands)
    assert all(cmd[-1] in ('version', '--help') for cmd in commands)


def test_engine_probe_detects_new_chat_and_api_without_exec(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[-1] == 'version':
            return FakeResult(output='codingbrain 0.12.0\n')
        return FakeResult(output='usage: codingbrain chat/api [-h]\n')
    monkeypatch.setattr(management.subprocess, 'run', fake_run)
    status = management.engine_capabilities(['fake-codingbrain'])
    assert status['available'] and status['conversation'] and status['typed_api']
    assert status['new_project'] is True


def test_missing_engine_is_reported_as_unavailable_not_ready(monkeypatch):
    monkeypatch.setattr(management.subprocess, 'run', lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert not management.engine_capabilities(['fake-codingbrain'])['available']
    assert not management.engine_capabilities(None)['available']


def test_api_status_is_authenticated_and_model_is_checked_not_guessed(tmp_path, monkeypatch):
    w = Workspace(home=tmp_path, executable='fake-codingbrain')
    config_dir = tmp_path / '.local' / 'CodingBrain' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.json').write_text(json.dumps({
        'models': {'provider': 'ollama', 'model': 'qwen2.5-coder:7b', 'api_key': 'DO_NOT_ECHO'},
        'supervisors': {'claude': {'enabled': True}}
    }), encoding='utf-8')
    monkeypatch.setattr(management, 'engine_capabilities', lambda cli: {
        'available': True, 'version': '0.9.0', 'conversation': False, 'typed_api': False,
        'task_command': True, 'detail': 'Engine v0.9.0'
    })
    monkeypatch.setattr(management, 'local_ollama_models', lambda: {
        'running': True, 'models': ['qwen2.5-coder:7b'], 'detail': 'Ollama tags checked'
    })
    web = TestClient(create_app(w))
    assert web.get('/api/engine/status').status_code == 403
    result = web.get('/api/engine/status', headers={'X-CodingBrain-Token': w.secret})
    assert result.status_code == 200
    data = result.json()
    assert data['bridge_online'] is True
    assert data['chat_available'] is False
    assert data['next_action'] == 'updates'
    assert data['engine']['version'] == '0.9.0'
    assert data['model']['name'] == 'qwen2.5-coder:7b'
    assert data['model']['ready'] is True
    assert 'DO_NOT_ECHO' not in result.text
    report = w.configured_providers()
    ollama = next(x for x in report if x['id'] == 'ollama')
    assert ollama['model_ready'] is True
    assert 'qwen2.5-coder:7b' in ollama['detail']


class TypedEngine:
    """The installed engine's typed API (engine.info, providers.list), as EngineClient returns it."""
    # engine.info's operation list from brain.local.engine (API 1.0).
    OPERATIONS = ('conversation.history', 'conversation.send', 'engine.describe', 'engine.info', 'events.read',
                  'projects.create', 'projects.list', 'projects.register', 'providers.list', 'tasks.accept',
                  'tasks.approve', 'tasks.get', 'tasks.list', 'tasks.start', 'tasks.stop', 'tasks.tool_decision')

    def __init__(self, providers=(), fail=False, operations=OPERATIONS):
        self.providers, self.fail, self.calls = list(providers), fail, []
        self.operations = list(operations)

    def call(self, op, params=None, on_event=None, timeout=None):
        from desktop_ui.engine_client import EngineError
        self.calls.append(op)
        if self.fail:
            raise EngineError('unavailable', 'the engine did not start')
        if op == 'engine.info':
            return {'api_version': '1.0', 'engine_version': '0.12.0', 'operations': self.operations}
        if op == 'providers.list':
            return self.providers
        raise AssertionError(f'readiness must not call {op}')


def test_stopped_ollama_never_counts_as_ready(tmp_path, monkeypatch):
    w = Workspace(home=tmp_path, executable='fake-codingbrain')
    monkeypatch.setattr(Workspace, 'engine_client', lambda self: TypedEngine())
    config_dir = tmp_path / '.local' / 'CodingBrain' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.json').write_text(json.dumps({'models': {'provider': 'ollama', 'model': 'qwen2.5-coder:7b'}}))
    monkeypatch.setattr(management, 'engine_capabilities', lambda cli: {
        'available': True, 'version': '0.13.0', 'conversation': True, 'typed_api': True,
        'task_command': True, 'detail': 'Engine online'
    })
    monkeypatch.setattr(management, 'local_ollama_models', lambda: {
        'running': False, 'models': [], 'detail': 'Offline'
    })
    result = w.engine_status()
    assert result['chat_available'] is True
    assert result['model']['ready'] is False
    assert result['next_action'] == 'setup'
    assert 'model not ready' in result['title'].lower()


def test_model_configuration_probe_never_returns_secrets(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'models': {'model': 'qwen2.5-coder:7b',
                           'provider': 'ollama', 'api_key_env': 'SECRET_VAR', 'token': 'PRIVATE_VALUE'}}))
    info = management.configured_model(config)
    assert info == {'provider': 'ollama', 'model': 'qwen2.5-coder:7b'}


def typed_workspace(tmp_path, monkeypatch, engine):
    w = Workspace(home=tmp_path, executable='fake-codingbrain')
    config_dir = tmp_path / '.local' / 'CodingBrain' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.json').write_text(json.dumps({'models': {'provider': 'ollama', 'model': 'qwen2.5-coder:7b'}}))
    monkeypatch.setattr(management, 'engine_capabilities', lambda cli: {
        'available': True, 'version': '0.12.0', 'conversation': True, 'typed_api': True,
        'task_command': True, 'detail': 'probe'})
    monkeypatch.setattr(management, 'local_ollama_models', lambda: {
        'running': True, 'models': ['qwen2.5-coder:7b'], 'detail': 'tags'})
    monkeypatch.setattr(management, 'providers_snapshot', lambda: [
        {'id': 'ollama', 'status': 'installed', 'authenticated': False, 'detail': ''},
        {'id': 'claude', 'status': 'auth-unverified', 'authenticated': False, 'detail': 'ui probe'},
        {'id': 'codex', 'status': 'authenticated', 'authenticated': True, 'detail': 'ui probe'}])
    monkeypatch.setattr(Workspace, 'engine_client', lambda self: engine)
    return w


def test_typed_engine_reports_versions_and_provider_readiness_from_the_installed_engine(tmp_path, monkeypatch):
    engine = TypedEngine(providers=[
        {'id': 'claude', 'installed': True, 'authentication': 'signed_in', 'enabled': True, 'readiness': 'ready',
         'last_error': None},
        {'id': 'codex', 'installed': False, 'authentication': 'not_applicable', 'enabled': False,
         'readiness': 'needs_setup', 'last_error': 'codex CLI not installed'}])
    w = typed_workspace(tmp_path, monkeypatch, engine)
    status = w.engine_status(refresh=True)
    assert status['chat_available'] and status['engine']['api_version'] == '1.0'
    assert status['engine']['version'] == '0.12.0'
    assert engine.calls == ['engine.info', 'providers.list']  # read operations only
    providers = {item['id']: item for item in w.configured_providers()}
    assert providers['claude']['connection_state'] == 'connected' and providers['claude']['source'] == 'engine'
    # The UI's own probe said codex was signed in; the installed engine says it is not installed.
    assert providers['codex']['connection_state'] == 'not-installed'
    assert 'codex CLI not installed' in providers['codex']['detail']


def test_typed_api_that_does_not_start_is_not_chat(tmp_path, monkeypatch):
    w = typed_workspace(tmp_path, monkeypatch, TypedEngine(fail=True))
    status = w.engine_status(refresh=True)
    assert status['chat_available'] is False and 'did not start' in status['engine']['detail']
    from desktop_ui.server import StartRequest
    import pytest
    with pytest.raises(ValueError, match='Nothing was started'):
        w.start(StartRequest(message='hello', mode='chat'))
    assert not w.jobs  # no job, no canned greeting


def test_legacy_engine_chat_is_refused_with_the_update_hint(tmp_path, monkeypatch):
    w = Workspace(home=tmp_path, executable='fake-codingbrain')
    monkeypatch.setattr(management, 'engine_capabilities', lambda cli: {
        'available': True, 'version': '0.9.0', 'conversation': False, 'typed_api': False,
        'task_command': True, 'detail': 'Engine v0.9.0'})
    monkeypatch.setattr(management, 'local_ollama_models', lambda: {'running': False, 'models': [], 'detail': 'off'})
    from desktop_ui.server import StartRequest
    import pytest
    with pytest.raises(ValueError, match='conversational release'):
        w.start(StartRequest(message='hello', mode='chat'))
    assert not w.jobs


def test_chat_update_does_not_automatically_enable_new_project(monkeypatch):
    """An installed conversational engine without 'codingbrain new' cannot create apps."""
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        if argv[-1] == 'version':
            return FakeResult(output='codingbrain 0.12.0')
        if argv[-2:] == ['new', '--help']:
            return FakeResult(2, 'error: invalid choice: new')
        return FakeResult(output='usage: codingbrain chat/api [-h]')

    monkeypatch.setattr(management.subprocess, 'run', fake_run)
    capability = management.engine_capabilities(['fake-codingbrain'])
    assert capability['available'] is True
    assert capability['conversation'] is True
    assert capability['typed_api'] is True
    assert capability['new_project'] is False
    assert ['fake-codingbrain', 'new', '--help'] in commands
    assert not any('run' in argv or '--yes' in argv for argv in commands)


def test_typed_operations_not_cli_help_decide_chat_and_new_project(tmp_path, monkeypatch):
    """With API 1.0 the installed engine's engine.info is authoritative: a CLI help probe that
    missed `new` does not hide project creation, and one that saw it does not enable it."""
    from desktop_ui.server import StartRequest
    import pytest
    w = typed_workspace(tmp_path, monkeypatch, TypedEngine())  # the probe reports no new_project
    status = w.engine_status(refresh=True)
    assert status['engine']['new_project'] is True and status['engine']['conversation'] is True

    without = [op for op in TypedEngine.OPERATIONS if op not in {'projects.create', 'conversation.send'}]
    w = typed_workspace(tmp_path / 'b', monkeypatch, TypedEngine(operations=without))
    monkeypatch.setattr(management, 'engine_capabilities', lambda cli: {
        'available': True, 'version': '0.12.0', 'conversation': True, 'typed_api': True,
        'new_project': True, 'task_command': True, 'detail': 'probe'})
    status = w.engine_status(refresh=True)
    assert status['engine']['new_project'] is False and status['chat_available'] is False
    for mode in ('new', 'chat'):
        with pytest.raises(ValueError, match='(?i)conversation.send.*missing.*nothing was started'):
            w.start(StartRequest(message='Create a task management application', mode=mode))
    assert not w.jobs
