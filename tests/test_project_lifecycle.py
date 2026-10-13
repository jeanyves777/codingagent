import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from brain.local.lifecycle import prepare, worker, launch, evidence, read_receipt
from brain.local.paths import Layout
from brain.contracts import Proposal
from test_engine import world, with_fake_brain, git  # noqa: F401


@pytest.fixture
def tmp_path():
    # Keep the nested Windows task/receipt paths below MAX_PATH, like desktop integration tests.
    import tempfile
    import shutil
    root = Path(tempfile.mkdtemp(prefix='cbl-')).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


def recipe():
    return {'outcome': 'Build a package, apply it, and verify the updated runnable program', 'target': 'installed', 'steps': [
        {'stage': 'build', 'environment': 'local', 'command': ['${PYTHON}', 'workflow.py', 'build', '${ARTIFACTS_DIR}'],
         'reason': 'Use the existing build script', 'evidence': ['workflow.py'], 'outputs': ['${ARTIFACTS_DIR}/package.py']},
        {'stage': 'apply', 'environment': 'local', 'command': ['${PYTHON}', 'workflow.py', 'apply', '${ARTIFACTS_DIR}'],
         'reason': 'Apply the generated package to its local installation', 'evidence': ['workflow.py']},
        {'stage': 'verify', 'environment': 'local', 'command': ['${PYTHON}', 'workflow.py', 'verify', '${ARTIFACTS_DIR}'],
         'reason': 'Run the installed result and check the goal', 'evidence': ['workflow.py']}]}


SCRIPT = '''
import sys, shutil, subprocess
from pathlib import Path
stage, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(exist_ok=True)
if stage == 'build':
    (out/'package.py').write_text('print("updated-and-working")\\n')
elif stage == 'apply':
    shutil.copyfile(out/'package.py', out/'installed.py')
elif stage == 'verify':
    result = subprocess.run([sys.executable, str(out/'installed.py')], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == 'updated-and-working'
'''


def accepted(root, plan=None, commit=None):
    return {'id': 'abc12345', 'status': 'accepted', 'commit': commit,
            'test_evidence': {'passed': True}, 'proposal': {'lifecycle': plan or recipe()}}


def save_request(tmp_path, payload):
    request, receipt = tmp_path/'request.json', tmp_path/'receipt.json'
    request.write_text(json.dumps(payload))
    return request, receipt


@pytest.mark.parametrize('name', ['inventory-tool', 'notes-editor', 'unknown-framework'])
def test_workflow_builds_applies_and_runs_for_arbitrary_projects(tmp_path, name):
    root = tmp_path/name
    root.mkdir()
    (root/'workflow.py').write_text(SCRIPT)
    layout = Layout(tmp_path/'state').ensure()
    payload = prepare(accepted(root), root, layout)
    request, receipt = save_request(tmp_path, payload)
    assert worker(request, receipt) == 0
    report = json.loads(receipt.read_text())
    assert report['status'] == 'completed'
    assert [s['status'] for s in report['steps']] == ['passed']*3
    assert report['artifacts']
    assert (Path(payload['variables']['ARTIFACTS_DIR'])/'installed.py').exists()


def test_failed_build_never_applies_or_reports_success(tmp_path):
    (tmp_path/'workflow.py').write_text(SCRIPT)
    plan = recipe()
    plan['steps'][0]['command'] = ['${PYTHON}', '-c', 'raise SystemExit(8)']
    payload = prepare(accepted(tmp_path, plan), tmp_path, Layout(tmp_path/'state').ensure())
    # Store control files outside the accepted source tree.
    controls = tmp_path.parent / (tmp_path.name+'-controls')
    controls.mkdir()
    request, receipt = save_request(controls, payload)
    assert worker(request, receipt) == 1
    assert json.loads(receipt.read_text())['status'] == 'failed'
    assert len(json.loads(receipt.read_text())['steps']) == 1


def test_changed_source_and_changed_plan_are_rejected(tmp_path):
    root=tmp_path/'project'; root.mkdir()
    (root/'workflow.py').write_text(SCRIPT)
    payload=prepare(accepted(root),root,Layout(tmp_path/'state').ensure())
    request,receipt=save_request(tmp_path,payload)
    (root/'workflow.py').write_text('print("changed")')
    assert worker(request,receipt)==1
    assert 'files changed' in json.loads(receipt.read_text())['error']
    payload['plan']['steps'][0]['command']=['malicious']
    request.write_text(json.dumps(payload))
    with pytest.raises(ValueError,match='plan changed'):
        worker(request,receipt)


def test_launch_without_readiness_check_is_rejected(tmp_path):
    (tmp_path/'workflow.py').write_text(SCRIPT)
    plan=recipe()
    plan['steps']=[{'stage':'run','environment':'local','command':['${PYTHON}','workflow.py'],
                    'reason':'launch','evidence':['workflow.py'],'background':True}]
    with pytest.raises(ValueError,match='verification'):
        plan['target']='running'
        prepare(accepted(tmp_path,plan),tmp_path,Layout(tmp_path/'state').ensure())


def test_tampered_artifact_cannot_be_applied(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'workflow.py').write_text(SCRIPT)
    plan=recipe()
    plan['steps'].insert(1, {'stage':'verify','environment':'local',
        'command':['${PYTHON}','-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("tampered")',
                   '${ARTIFACTS_DIR}/package.py'], 'reason':'simulate changed artifact', 'evidence':['workflow.py']})
    payload=prepare(accepted(root,plan),root,Layout(tmp_path/'state').ensure())
    request,receipt=save_request(tmp_path,payload)
    assert worker(request,receipt)==1
    report=read_receipt(receipt)
    assert 'artifact changed' in report['error']
    assert not (Path(payload['variables']['ARTIFACTS_DIR'])/'installed.py').exists()


def test_workflow_inspection_reads_build_files_without_executing_them(tmp_path):
    (tmp_path/'Makefile').write_text('build:\n\tthis-must-not-run\n')
    (tmp_path/'README.md').write_text('See Makefile for the build workflow')
    result=evidence(tmp_path)
    assert result['commands']['build']=='make build'
    assert 'this-must-not-run' in result['sources']['Makefile']


def test_invalid_lifecycle_gets_focused_model_correction(world, monkeypatch):
    brain = with_fake_brain(world, monkeypatch)
    goals = []
    async def propose(repository, goal, memories, context):
        goals.append(goal)
        assert 'project_workflow' in context
        plan = recipe()
        if len(goals) == 1:
            plan['steps'] = plan['steps'][:1]  # Installed target without apply/verify.
        for step in plan['steps']:
            step['evidence'] = ['main.py']
        return json.dumps({'plan': 'Fix x', 'changes': [{'path': 'main.py', 'content': 'x = 2\n'}],
                           'lifecycle': plan})
    monkeypatch.setattr(brain.model, 'propose', propose)
    task = world['engine'].call('tasks.start', {'project_id': world['project']['id'], 'goal': 'Fix x'})
    assert task['status'] == 'proposed'
    assert len(goals) == 2 and 'apply' in goals[1] and 'rejected' in goals[1]
    assert task['lifecycle']['target'] == 'installed'


def test_preparation_is_separately_approved_before_code_tests(world, monkeypatch):
    engine,pid=world['engine'],world['project']['id']
    brain=with_fake_brain(world,monkeypatch)
    task=engine.call('tasks.start',{'project_id':pid,'goal':'Fix x'})
    stored=brain.store.get(task['id'])
    plan=recipe()
    plan['preparation']=[{'stage':'prepare','environment':'local',
        'command':['${PYTHON}','-c','print("prepared")'],'reason':'Install test dependencies',
        'evidence':['main.py']}]
    stored['proposal']['lifecycle']=plan
    # The service's real digest includes the revised recipe.
    import hashlib
    stored['digest']=hashlib.sha256(json.dumps(stored['proposal'],sort_keys=True).encode()).hexdigest()
    brain.store.save(stored)
    params={'project_id':pid,'task_id':task['id'],'digest':stored['digest'],'decision':'approve'}
    with pytest.raises(Exception,match='environment preparation'):
        engine.call('tasks.approve',params)
    payload=engine.call('tasks.preparation_plan',{'project_id':pid,'task_id':task['id']})
    def run_now(payload,directory):
        directory.mkdir(parents=True)
        request,receipt=save_request(directory,payload)
        assert worker(request,receipt)==0
        return {'receipt':str(receipt),'status':'starting','digest':payload['digest']}
    monkeypatch.setattr('brain.local.lifecycle.launch',run_now)
    engine.call('tasks.preparation_run',{'project_id':pid,'task_id':task['id'],'digest':payload['digest']})
    assert engine.call('tasks.preparation_status',{'project_id':pid,'task_id':task['id']})['status']=='completed'
    assert engine.call('tasks.approve',params)['status']=='passed'


def test_detected_toolchain_is_not_mistaken_for_python(tmp_path):
    from brain.sandbox import profile, run_tests
    (tmp_path/'Cargo.toml').write_text('[package]\nname="something"\nversion="0.1.0"\n')
    assert profile(tmp_path)=={'name':'rust','command':['cargo','test']}
    report=run_tests(tmp_path,{'python':'existing-image'})
    assert report['infrastructure'] and report['exit_code'] is None
    assert 'rust' in report['output']


def test_existing_code_can_be_tested_and_accepted_for_a_build_only_goal(world, monkeypatch):
    brain = with_fake_brain(world, monkeypatch)
    async def propose(repository, goal, memories, context):
        return json.dumps({'plan': 'Build the existing code', 'changes': [], 'lifecycle': {
            'outcome': 'Build package', 'target': 'artifact', 'steps': [{
                'stage': 'build', 'environment': 'local', 'command': ['${PYTHON}', '-c', 'print("build")'],
                'reason': 'Build existing code', 'evidence': ['main.py'],
                'outputs': ['${ARTIFACTS_DIR}/package']} ]}})
    monkeypatch.setattr(brain.model, 'propose', propose)
    engine, pid = world['engine'], world['project']['id']
    task = engine.call('tasks.start', {'project_id': pid, 'goal': 'Build existing code'})
    task = engine.call('tasks.approve', {'project_id': pid, 'task_id': task['id'],
                                      'digest': task['digest'], 'decision': 'approve'})
    assert task['status'] == 'passed'
    task = engine.call('tasks.accept', {'project_id': pid, 'task_id': task['id']})
    assert task['status'] == 'accepted' and task['commit']
    assert engine.call('tasks.lifecycle_plan', {'project_id': pid, 'task_id': task['id']})['digest']


def test_engine_requires_acceptance_digest_and_prevents_replay(world, monkeypatch):
    brain=with_fake_brain(world,monkeypatch)
    engine,pid=world['engine'],world['project']['id']
    task=engine.call('tasks.start',{'project_id':pid,'goal':'Fix x'})
    with pytest.raises(Exception,match='accepted'):
        prepare({**brain.store.get(task['id']), 'proposal':{'lifecycle':recipe()}},brain.workspace(task['id']),world['layout'])
    task=engine.call('tasks.approve',{'project_id':pid,'task_id':task['id'],'digest':task['digest'],'decision':'approve'})
    task=engine.call('tasks.accept',{'project_id':pid,'task_id':task['id']})
    stored=brain.store.get(task['id'])
    plan=recipe()
    for step in plan['steps']:
        step['evidence']=['main.py']
    stored['proposal']['lifecycle']=plan
    brain.store.save(stored)
    payload=engine.call('tasks.lifecycle_plan',{'project_id':pid,'task_id':task['id']})
    called=[]
    def fake_launch(payload,directory):
        called.append(payload)
        return {'receipt':str(directory/'result.json'),'digest':payload['digest'],'status':'starting'}
    monkeypatch.setattr('brain.local.lifecycle.launch',fake_launch)
    with pytest.raises(Exception,match='approval does not match'):
        engine.call('tasks.lifecycle_run',{'project_id':pid,'task_id':task['id'],'digest':'wrong'})
    assert not called
    engine.call('tasks.lifecycle_run',{'project_id':pid,'task_id':task['id'],'digest':payload['digest']})
    with pytest.raises(Exception,match='already authorized'):
        engine.call('tasks.lifecycle_run',{'project_id':pid,'task_id':task['id'],'digest':payload['digest']})
    assert len(called)==1


def test_detached_worker_runs_using_the_installed_package(tmp_path):
    root=tmp_path/'project'; root.mkdir()
    (root/'workflow.py').write_text(SCRIPT)
    payload=prepare(accepted(root),root,Layout(tmp_path/'state').ensure())
    application=launch(payload,tmp_path/'handoff')
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        report=read_receipt(Path(application['receipt']))
        if report['status'] in {'completed','failed'}:
            break
        time.sleep(.1)
    assert report['status']=='completed', (tmp_path/'handoff/worker.log').read_text()
