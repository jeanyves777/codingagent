import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from brain.local.paths import Layout
from brain.local.session import pid_alive, recover_interrupted_tasks, running_tasks
from brain.local.updater import _guard, UpdateError
from test_engine import world, with_fake_brain  # noqa: F401


def database(tmp_path, tasks):
    layout = Layout(tmp_path / 'home')
    layout.ensure()
    folder = layout.projects / 'project'
    folder.mkdir()
    path = folder / 'brain.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        db.executemany('INSERT INTO tasks VALUES (?, ?)', [(t['id'], json.dumps(t)) for t in tasks])
    return layout, path


def test_dead_task_is_preserved_and_no_longer_blocks_update(tmp_path):
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    assert not pid_alive(dead.pid)
    task = {'id': 'crashed', 'status': 'planning', 'goal': 'add history', 'workspace': 'keep-this',
            'owner': {'pid': dead.pid, 'host': socket.gethostname()}, 'events': []}
    layout, path = database(tmp_path, [task])
    _guard(layout)
    assert running_tasks(layout) == []
    with sqlite3.connect(path) as db:
        updated = json.loads(db.execute('SELECT body FROM tasks').fetchone()[0])
    assert updated['status'] == 'blocked'
    assert updated['goal'] == task['goal'] and updated['workspace'] == task['workspace']
    assert json.loads(open(updated['interruption']['backup'], encoding='utf-8').read()) == task
    assert recover_interrupted_tasks(layout) == []


@pytest.mark.parametrize('owner', [None, {'pid': 123, 'host': 'another-computer'},
                                  {'pid': 0, 'host': socket.gethostname()}])
def test_unknown_ownership_still_blocks_update(tmp_path, owner):
    layout, _ = database(tmp_path, [{'id': 'unknown', 'status': 'planning', 'owner': owner}])
    with pytest.raises(UpdateError, match='mid-execution'):
        _guard(layout)


def test_live_task_still_blocks_without_being_changed(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        task = {'id': 'live', 'status': 'testing', 'owner': {'pid': child.pid, 'host': socket.gethostname()}}
        layout, path = database(tmp_path, [task])
        assert pid_alive(child.pid)
        with pytest.raises(UpdateError, match='mid-execution'):
            _guard(layout)
        with sqlite3.connect(path) as db:
            assert json.loads(db.execute('SELECT body FROM tasks').fetchone()[0]) == task
        assert not list(layout.backups.rglob('*.json'))
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_planning_owner_is_saved_before_workspace_preparation(world, monkeypatch):
    brain = with_fake_brain(world, monkeypatch)
    observed = []

    def fail_prepare(*args):
        task = brain.store.tasks()[-1]
        observed.append(task)
        assert task['status'] == 'planning' and task['owner'] == brain.owner
        raise RuntimeError('simulated preparation failure')

    monkeypatch.setattr(brain.manager, 'prepare', fail_prepare)
    with pytest.raises(RuntimeError, match='simulated preparation failure'):
        world['engine'].call('tasks.start', {'project_id': world['project']['id'], 'goal': 'Fix x'})
    assert observed


@pytest.mark.skipif(not os.environ.get('CB_RECOVERY_ENGINE_WHEEL'), reason='requires built target wheel')
def test_target_wheel_recovery_unblocks_an_old_installed_updater(tmp_path):
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    task = {'id': 'legacy-crash', 'status': 'planning', 'owner': {'pid': dead.pid, 'host': socket.gethostname()}}
    layout, _ = database(tmp_path, [task])
    python = os.environ.get('CB_LEGACY_PYTHON', sys.executable)
    script = Path(__file__).resolve().parents[1] / 'desktop_ui/Recover-CodingBrainTasks.py'
    result = subprocess.run([python, '-I', str(script), '--wheel', os.environ['CB_RECOVERY_ENGINE_WHEEL'],
                             '--home', str(layout.home)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Recovered interrupted task' in result.stdout
    result = subprocess.run([python, '-I', '-c',
        'from pathlib import Path; from brain.local.paths import Layout; from brain.local.updater import _guard; '
        'import sys; _guard(Layout(Path(sys.argv[1])))', str(layout.home)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
