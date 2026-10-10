"""Safety and functionality of full-system setup and provider management."""
from pathlib import Path
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from desktop_ui import management
from desktop_ui.server import Workspace, create_app


@pytest.fixture(autouse=True)
def no_installed_engine(monkeypatch):
    """Hermetic: these tests cover the desktop's own provider probes, so the machine's installed
    engine (on CI `pip install -e .` puts a real `codingbrain` with the typed API on PATH, whose
    providers.list would rightly override the fakes) and its local Ollama are not consulted.
    Engine-sourced readiness is covered in test_desktop_engine_status.py."""
    monkeypatch.setattr(management, 'engine_capabilities', lambda cli: {
        'available': True, 'version': '0.9.0', 'conversation': False, 'typed_api': False,
        'task_command': True, 'detail': 'test engine without the typed API'})
    monkeypatch.setattr(management, 'local_ollama_models', lambda: {'running': False, 'models': [], 'detail': 'off'})
    monkeypatch.setattr(management, '_registry_path_dirs', lambda: [])  # never this machine's registry


def session(tmp_path):
    root = Workspace(home=tmp_path, executable='codingbrain')
    return TestClient(create_app(root)), root


def token(root):
    return {'X-CodingBrain-Token': root.secret}


def test_setup_and_provider_access_requires_auth(tmp_path):
    c, workspace = session(tmp_path)
    assert c.get('/api/setup').status_code == 403
    assert c.post('/api/setup/completed',json={'completed': True}).status_code == 403
    assert c.post('/api/system/action?action=install_full',json={'confirmed': True}).status_code == 403
    assert c.post('/api/providers/claude/signin',json={'confirmed': True}).status_code == 403
    assert c.post('/api/providers/claude/configure',json={'enabled':True}).status_code == 403
    status = c.get('/api/setup',headers=token(workspace))
    assert status.status_code == 200
    names = {p['id'] for p in status.json()['providers']}
    assert names == {'ollama','claude','codex','gemini','grok','meta','muse'}


def test_first_run_is_persistent_and_optional(tmp_path):
    c, w = session(tmp_path)
    assert not c.get('/api/setup',headers=token(w)).json()['readiness']['onboarding_completed']
    assert c.post('/api/setup/completed',headers=token(w),json={'completed': True}).status_code == 200
    assert Workspace(home=tmp_path,executable='codingbrain').first_run_completed()
    assert c.post('/api/setup/completed',headers=token(w),json={'completed': False}).status_code == 200
    assert not w.first_run_completed()


def test_no_install_or_login_without_explicit_confirmation(tmp_path,monkeypatch):
    c, w = session(tmp_path)
    called=[]
    monkeypatch.setattr(w,'maintenance', lambda action: called.append(action))
    monkeypatch.setattr(w,'sign_in', lambda name: called.append(name))
    assert c.post('/api/system/action?action=install_full',headers=token(w),json={'confirmed':False}).status_code == 409
    assert c.post('/api/providers/claude/signin',headers=token(w),json={'confirmed':False}).status_code == 409
    assert called == []


def test_maintenance_command_strict_allowlist():
    command=['C:/Program Files/Python/python.exe','-m','brain.local']
    assert management.maintenance_args('install_full',command)[-3:] == ['install','--profile','full']
    assert management.maintenance_args('update_engine',command)[-1:] == ['update']
    for unsafe in ['cmd.exe','install;rm -rf /','provider_auth','../../bin/sh']:
        with pytest.raises(ValueError): management.maintenance_args(unsafe,command)


def test_install_and_update_only_in_official_interactive_console(tmp_path,monkeypatch):
    c,w=session(tmp_path)
    commands=[]
    monkeypatch.setattr(management,'launch_interactive_windows',lambda command:commands.append(command))
    assert c.post('/api/system/action?action=install_full',headers=token(w),json={'confirmed':True}).status_code == 200
    assert c.post('/api/system/action?action=update_engine',headers=token(w),json={'confirmed':True}).status_code == 200
    assert commands == [['codingbrain','install','--profile','full'],['codingbrain','update']]


def test_provider_signin_uses_official_vendor_cli_only(tmp_path,monkeypatch):
    c,w=session(tmp_path)
    commands=[]
    monkeypatch.setattr(management,'installed_cli',lambda name: '/bin/'+name if name in {'claude','codex','gemini'} else None)
    monkeypatch.setattr(management,'launch_interactive_windows',lambda command:commands.append(command))
    for provider in ['claude','codex','gemini']:
        assert c.post(f'/api/providers/{provider}/signin',headers=token(w),json={'confirmed':True}).status_code == 200
    assert commands == [['/bin/claude'],['/bin/codex','login'],['/bin/gemini']]
    for unsupported in ['grok','meta','muse','other']:
        assert c.post(f'/api/providers/{unsupported}/signin',headers=token(w),json={'confirmed':True}).status_code == 409
    assert len(commands)==3


def test_credential_secrecy_and_supported_routing(monkeypatch):
    monkeypatch.setenv('XAI_API_KEY','super_private_xai_token')
    monkeypatch.setattr(management,'installed_cli',lambda name: '/bin/'+name if name in {'claude','codex'} else None)
    providers=management.providers_snapshot(probe=lambda cmd: True)
    assert providers[1]['status']=='authenticated'
    assert providers[2]['status']=='authenticated'
    grok=next(p for p in providers if p['id']=='grok')
    assert grok['status']=='api-key-configured'
    assert grok['core_enabled'] is False
    assert 'super_private_xai_token' not in json.dumps(providers)
    muse=next(p for p in providers if p['id']=='muse')
    assert muse['status']=='planned' and not muse['sign_in_available']


def test_provider_enable_only_when_supported_by_core(tmp_path,monkeypatch):
    c,w=session(tmp_path)
    calls=[]
    class Result:
        returncode=0
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs:(calls.append((args,kwargs)),Result())[1])
    assert c.post('/api/providers/claude/configure',headers=token(w),json={'enabled':True}).status_code == 200
    assert calls[0][0][0] == ['codingbrain','setup','--non-interactive','--enable-claude']
    assert c.post('/api/providers/codex/configure',headers=token(w),json={'enabled':False}).status_code == 200
    assert calls[1][0][0][-1] == '--no-enable-codex'
    assert c.post('/api/providers/grok/configure',headers=token(w),json={'enabled':True}).status_code == 409
    assert len(calls)==2


def test_probe_makes_no_network_calls(monkeypatch):
    monkeypatch.setattr(management,'installed_cli',lambda name:None)
    providers=management.providers_snapshot(probe=lambda c:pytest.fail('probe may not call missing CLI'))
    assert all(p['status'] not in ('authenticated','api-key-configured') for p in providers if p['id'] in {'claude','codex'})


def test_readiness_detects_installer_support_without_mutating_engine(tmp_path,monkeypatch):
    w=Workspace(home=tmp_path,executable='codingbrain')
    calls=[]
    monkeypatch.setattr(management,'safe_probe',lambda cmd,timeout=6:(calls.append(cmd),True)[1])
    data=w.installation_status()
    assert data['full_installer_available']
    assert calls==[['codingbrain','install','--help']]


def test_backend_ui_has_wizard_and_provider_management():
    root=Path(__file__).resolve().parents[1]/'desktop_ui'/'static'
    html=(root/'index.html').read_text(encoding='utf-8')
    js=(root/'app.js').read_text(encoding='utf-8')
    assert 'id="control-dialog"' in html
    assert 'id="control-providers"' in html
    assert 'id="control-updates"' in html
    assert "firstRun();" in js
    assert 'install_full' in js and '/api/setup' in js
    assert not ('eval(' in js or 'innerHTML =' in js)


def test_provider_cards_reflect_actual_core_enabled_state(tmp_path, monkeypatch):
    w=Workspace(home=tmp_path,executable='codingbrain')
    cfg=tmp_path/'.local'/'CodingBrain'/'config'
    cfg.mkdir(parents=True)
    (cfg/'config.json').write_text(json.dumps({'supervisors':{'claude':{'enabled':True},'codex':{'enabled':False}}}))
    flags={p['id']:p.get('supervisor_enabled') for p in w.configured_providers()}
    assert flags['claude'] is True
    assert flags['codex'] is False
    assert flags['grok'] is None


def test_doctor_option_detection_is_help_only_and_no_secret_output(monkeypatch):
    seen = []

    class FakeResult:
        returncode = 0
        stdout = 'usage: codingbrain doctor [-h] [--full] [--json]'

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        return FakeResult()

    monkeypatch.setattr(management.subprocess, 'run', fake_run)
    assert management.supports_cli_option(['codingbrain'], 'doctor', '--full')
    assert seen[0][0] == ['codingbrain', 'doctor', '--help']
    assert seen[0][1]['stdin'] is subprocess.DEVNULL
    assert management.maintenance_args('doctor', ['codingbrain'], deep_doctor=False) == ['codingbrain', 'doctor']
    assert management.maintenance_args('doctor', ['codingbrain'], deep_doctor=True) == ['codingbrain', 'doctor', '--full']
    with pytest.raises(ValueError):
        management.supports_cli_option(['codingbrain'], 'install', '--yes')


def test_legacy_doctor_uses_basic_only_and_labels_capabilities(tmp_path, monkeypatch):
    c, w = session(tmp_path)
    probes=[]
    monkeypatch.setattr(management, 'safe_probe', lambda args, timeout=6: True)
    monkeypatch.setattr(management, 'supports_cli_option', lambda cli, op, flag: (probes.append((cli, op, flag)), False)[1])
    r = c.get('/api/setup', headers=token(w)).json()['readiness']
    assert r['full_installer_available'] is True
    assert r['deep_doctor_available'] is False
    assert 'Basic check' in r['doctor_note']
    runs=[]
    monkeypatch.setattr(w, '_execute', lambda job: runs.append(job.command))
    action = c.post('/api/system/action?action=doctor', headers=token(w), json={'confirmed':True})
    assert action.status_code == 200
    assert action.json()['diagnostic_level'] == 'basic'
    assert w.jobs[action.json()['job']['id']].command == ['codingbrain', 'doctor']
    assert all(flag == '--full' and op == 'doctor' for _,op,flag in probes)


def test_current_doctor_uses_deep_when_supported(tmp_path, monkeypatch):
    c, w = session(tmp_path)
    monkeypatch.setattr(management, 'supports_cli_option', lambda *args: True)
    monkeypatch.setattr(w, '_execute', lambda job: None)
    r = c.post('/api/system/action?action=doctor', headers=token(w), json={'confirmed':True})
    assert r.status_code == 200
    assert r.json()['diagnostic_level'] == 'deep'
    assert w.jobs[r.json()['job']['id']].command == ['codingbrain', 'doctor', '--full']


def test_authentication_and_supervisor_routing_are_separate(tmp_path, monkeypatch):
    """A user is connected only if official CLI auth succeeded AND routing is enabled."""
    w = Workspace(home=tmp_path, executable='codingbrain')
    cfg = tmp_path / '.local' / 'CodingBrain' / 'config'
    cfg.mkdir(parents=True)
    (cfg / 'config.json').write_text(json.dumps({
        'supervisors': {'claude': {'enabled': True}, 'codex': {'enabled': False}}
    }), encoding='utf-8')
    fake = [
        {'id':'claude', 'name':'Claude Code','status':'authenticated', 'authenticated':True},
        {'id':'codex','name':'OpenAI Codex','status':'authenticated', 'authenticated':True},
    ]
    monkeypatch.setattr(management,'providers_snapshot',lambda: [p.copy() for p in fake])
    states={p['id']:p for p in w.configured_providers()}
    assert states['claude']['connection_state']=='connected'
    assert states['codex']['connection_state']=='signed-in'
    fake[0].update(status='auth-unverified',authenticated=False)
    assert w.configured_providers()[0]['connection_state']=='unverified'
    fake[0].update(status='not-installed')
    assert w.configured_providers()[0]['connection_state']=='not-installed'


def test_windows_user_cli_shims_can_be_detected_after_stale_path(tmp_path, monkeypatch):
    """A packaged desktop often lacks npm's late-added PATH; only safe fixed paths are checked."""
    monkeypatch.setattr(management, 'IS_WINDOWS', True)
    monkeypatch.setattr(management.shutil, 'which', lambda name: None)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    shim = tmp_path / 'Roaming' / 'npm' / 'codex.cmd'
    shim.parent.mkdir(parents=True)
    shim.write_text('@echo off\n')
    assert management.installed_cli('codex') == str(shim)
    assert management.installed_cli('claude') is None
    assert management.installed_cli('node') is None
    shim.unlink()
    winget = tmp_path / 'Local' / 'Microsoft' / 'WinGet' / 'Links' / 'codex.exe'
    winget.parent.mkdir(parents=True)
    winget.touch()
    assert management.installed_cli('codex') == str(winget)
