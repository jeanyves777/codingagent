"""Goal-derived project workflows, bound to accepted code and explicit command approval.

No repository names or framework-specific deployment routes occur here. The model
supplies an inspected recipe; the worker executes it and records observed results.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from ..contracts import LifecyclePlan


def evidence(root: Path) -> dict:
    from .project import commands, frameworks
    names = ['README.md', 'BUILDING.md', 'RELEASING.md', 'CONTRIBUTING.md', 'pyproject.toml', 'package.json',
             'Cargo.toml', 'go.mod', 'pom.xml', 'Makefile', 'Dockerfile', 'coding-brain.json']
    for folder in ('scripts', 'installer', '.github/workflows'):
        if (root / folder).is_dir():
            names.extend(p.relative_to(root).as_posix() for p in sorted((root / folder).rglob('*'))
                         if p.is_file() and not p.is_symlink() and p.suffix in {'.py', '.ps1', '.cmd', '.yml', '.yaml', '.sh'})
    sources, remaining = {}, 24000
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            continue
        content = path.read_text(encoding='utf-8', errors='replace')[:min(remaining, 4000)]
        sources[name] = content
        remaining -= len(content)
        if remaining <= 0:
            break
    from .paths import Layout
    from .updater import read_current
    return {'commands': commands(root), 'frameworks': frameworks(root), 'sources': sources,
            'execution_context': {'python': sys.executable, 'engine_home': str(Layout.default().home),
                                  'installed_engine_version': read_current(Layout.default()).get('version')},
            'rule': 'Infer necessary build, verify, apply and launch steps from these files and the goal.'}


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr[-1000:] or 'Accepted workspace is not available')
    return result.stdout.strip()


def _fingerprint(root: Path, commit=None) -> dict:
    from .project import SKIP
    files = {}
    if commit:
        for name in _git(root, 'ls-files', '-z').split('\x00'):
            if not name:
                continue
            path = root / name
            files[name] = ('symlink:' + os.readlink(path)) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()
        return files
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name not in SKIP
                      and not (Path(directory) / name).is_symlink()]
        for name in sorted(names):
            path = Path(directory) / name
            if not path.is_symlink():
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _identity(root: Path, commit: str | None, files=None, fingerprint_commit=None):
    if commit and (_git(root, 'rev-parse', 'HEAD') != commit or
                   _git(root, 'status', '--porcelain', '--untracked-files=no')):
        raise ValueError('Accepted code changed. Test and accept a new task before building or applying it.')
    if files is not None and _fingerprint(root, fingerprint_commit or commit) != files:
        raise ValueError('Accepted project files changed. Test and accept the revised source.')


def prepare(task: dict, workspace: Path, layout, preparation=False) -> dict | None:
    recipe = (task.get('proposal') or {}).get('lifecycle')
    key = 'preparation' if preparation else 'steps'
    if not recipe or not recipe.get(key):
        return None
    if preparation and task.get('status') != 'proposed':
        raise ValueError('Only a reviewed proposal can request environment preparation')
    if not preparation and (task.get('status') != 'accepted' or not task.get('test_evidence', {}).get('passed')):
        raise ValueError('Only accepted, test-passed code can be built or applied')
    plan = LifecyclePlan.model_validate(recipe).model_dump()
    commit = task.get('commit')
    fingerprint_commit = commit
    if preparation:
        plan['steps'] = plan['preparation']
        plan['outcome'] = 'Prepare and verify the project environment before code testing'
        if task.get('workspace_kind') == 'git':
            fingerprint_commit = _git(workspace, 'rev-parse', 'HEAD')
        commit = None
    _identity(workspace, commit)
    for step in plan['steps']:
        if step['background'] and (step['stage'] != 'run' or step['environment'] != 'local'):
            raise ValueError('Only a local run step may launch in the background')
        if step['environment'] == 'sandbox' and step['stage'] in {'apply', 'run'}:
            raise ValueError('Applying or launching an installed application needs local command approval')
        if not step['evidence']:
            raise ValueError('Every lifecycle command needs inspected project-file evidence')
        for name in step['evidence']:
            path = workspace / name
            if Path(name).is_absolute() or not path.is_file() or path.is_symlink() or \
                    not path.resolve().is_relative_to(workspace.resolve()):
                raise ValueError('Lifecycle evidence is missing or outside the accepted project: ' + name)
    for index, step in enumerate(plan['steps']):
        if step['background'] and not any(s['stage'] == 'verify' and not s['background']
                                         for s in plan['steps'][index + 1:]):
            raise ValueError('A background launch needs a subsequent foreground readiness verification')
    from .updater import read_current
    from packaging.version import Version
    current = read_current(layout).get('version', '0.0.0')
    version = Version(current)
    release = list(version.release)
    if version.dev is None:
        release[-1] += 1
    next_version = '.'.join(map(str, release)) + f'.dev{version.dev + 1 if version.dev is not None else 0}+task{task["id"][:8]}'
    payload = {'task_id': task['id'], 'commit': commit, 'workspace': str(workspace.resolve()),
               'source_files': _fingerprint(workspace, fingerprint_commit), 'fingerprint_commit': fingerprint_commit,
               'preparation': preparation, 'proposal_digest': task.get('digest') if preparation else None,
               'plan': plan, 'variables': {'PROJECT_ROOT': str(workspace.resolve()),
                 'ARTIFACTS_DIR': str((workspace.parent / 'lifecycle-artifacts').resolve()),
                 'PYTHON': sys.executable, 'ENGINE_HOME': str(layout.home.resolve()), 'NEXT_VERSION': next_version}}
    payload['resolved_commands'] = []
    for step in plan['steps']:
        variables = payload['variables']
        if step['environment'] == 'sandbox':
            variables = {**variables, 'PROJECT_ROOT': '/code', 'ARTIFACTS_DIR': '/out', 'PYTHON': 'python'}
        payload['resolved_commands'].append({'stage': step['stage'], 'environment': step['environment'],
            'sandbox_image': step.get('sandbox_image') or plan.get('sandbox_image'),
            'command': [_expand(arg, variables) for arg in step['command']], 'reason': step['reason'],
            'evidence': step['evidence'], 'outputs': [_expand(p, payload['variables']) for p in step['outputs']]})
    payload['digest'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    for attempt in range(30):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.03)


def read_receipt(path):
    for attempt in range(30):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.03)


def _expand(value, variables):
    def replace(match):
        key = match.group(1)
        if key not in variables:
            raise ValueError('Unknown lifecycle variable: ' + key)
        return variables[key]
    return re.sub(r'\$\{([A-Z_]+)\}', replace, value)


def _outputs(paths, variables):
    found = {}
    roots = [Path(variables[key]).resolve() for key in ('PROJECT_ROOT', 'ARTIFACTS_DIR')]
    for name in paths:
        path = Path(_expand(name, variables))
        if not path.is_absolute():
            path = roots[0] / path
        if path.is_symlink() or not any(path.resolve().is_relative_to(root) for root in roots):
            raise ValueError('Artifact is outside the accepted workspace/output directory')
        files = [path] if path.is_file() else sorted(path.rglob('*')) if path.is_dir() else []
        if not files:
            raise ValueError('Expected build output missing: ' + str(path))
        for file in files:
            if file.is_symlink() or not any(file.resolve().is_relative_to(root) for root in roots):
                raise ValueError('Artifact follows an external link')
            if file.is_file():
                found[str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
    return found


def worker(request: Path, receipt: Path) -> int:
    payload = json.loads(request.read_text(encoding='utf-8'))
    digest = payload.pop('digest')
    if hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() != digest:
        raise ValueError('Approved lifecycle plan changed')
    root = Path(payload['workspace'])
    variables = payload['variables']
    Path(variables['ARTIFACTS_DIR']).mkdir(parents=True, exist_ok=True)
    report = {'task_id': payload['task_id'], 'digest': digest, 'status': 'running', 'steps': [], 'artifacts': {}}
    _write(receipt, report)
    try:
        _identity(root, payload['commit'], payload['source_files'], payload['fingerprint_commit'])
        for index, step in enumerate(payload['plan']['steps']):
            command = [_expand(arg, variables) for arg in step['command']]
            if any(not arg or '\x00' in arg for arg in command):
                raise ValueError('Invalid lifecycle command')
            if step['stage'] == 'apply':
                _identity(root, payload['commit'], payload['source_files'], payload['fingerprint_commit'])
                for file, expected in report['artifacts'].items():
                    if hashlib.sha256(Path(file).read_bytes()).hexdigest() != expected:
                        raise ValueError('Built artifact changed before application: ' + file)
            entry = {'stage': step['stage'], 'command': command, 'status': 'running', 'started': time.time()}
            report['steps'].append(entry)
            _write(receipt, report)
            print(f"{step['stage']}: {' '.join(command)}", flush=True)
            if step['environment'] == 'sandbox':
                from ..sandbox import run_build
                from .config import load
                from .paths import Layout
                settings = load(Layout(Path(variables['ENGINE_HOME'])))
                images = {'python': settings['sandbox']['python_image'], 'node': settings['sandbox']['node_image']}
                sandbox_vars = {**variables, 'PROJECT_ROOT': '/code', 'ARTIFACTS_DIR': '/out', 'PYTHON': 'python'}
                command = [_expand(arg, sandbox_vars) for arg in step['command']]
                image = step.get('sandbox_image') or payload['plan'].get('sandbox_image') or images
                result = run_build(root, image, command, Path(variables['ARTIFACTS_DIR']), timeout=step['timeout'])
                entry.update(exit_code=result.get('exit_code'), output=result.get('output', '')[-4000:])
                if not result['passed']:
                    raise RuntimeError('Sandbox lifecycle step failed: ' + entry['output'])
            else:
                log_path = receipt.parent / f'step-{index}.log'
                with log_path.open('w', encoding='utf-8') as log:
                    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                    child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, env={**os.environ, 'BUILD_OUT': variables['ARTIFACTS_DIR']}, **kwargs)
                    if step['background']:
                        entry.update(pid=child.pid, status='launched')
                    else:
                        try:
                            code = child.wait(timeout=step['timeout'])
                        except subprocess.TimeoutExpired:
                            child.kill()  # Only this Popen child; never processes selected by name.
                            child.wait(timeout=10)
                            raise RuntimeError('Lifecycle step timed out')
                        entry.update(exit_code=code)
                        if code:
                            raise RuntimeError(f'Lifecycle step failed (exit {code}); see {log_path}')
                entry['log'] = str(log_path)
            report['artifacts'].update(_outputs(step['outputs'], variables))
            if entry['status'] != 'launched':
                entry['status'] = 'passed'
            _write(receipt, report)
        if payload.get('preparation'):
            _identity(root, None, payload['source_files'], payload['fingerprint_commit'])
        report['status'] = 'completed'
        report['finished'] = time.time()
        _write(receipt, report)
        print('Project workflow completed. See the recorded step results and artifacts.', flush=True)
        return 0
    except Exception as error:
        report.update(status='failed', error=str(error)[:2000], finished=time.time())
        if report['steps'] and report['steps'][-1]['status'] == 'running':
            report['steps'][-1]['status'] = 'failed'
        _write(receipt, report)
        print(str(error), file=sys.stderr, flush=True)
        return 1


def launch(payload: dict, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    request, receipt = directory / 'approved-plan.json', directory / 'result.json'
    _write(request, payload)
    _write(receipt, {'status': 'starting', 'digest': payload['digest'], 'steps': []})
    # Clear PyInstaller's inherited DLL search directory before importing anything
    # that can load OpenSSL. A self-update must not lock the native app's DLLs.
    bootstrap = ("import os, ctypes; "
        "ctypes.windll.kernel32.SetDllDirectoryW(None) if os.name == 'nt' else None; "
        "import runpy; runpy.run_module('brain.local.lifecycle', run_name='__main__')")
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with (directory / 'worker.log').open('w', encoding='utf-8') as log:
        child = subprocess.Popen([sys.executable, '-I', '-c', bootstrap, '--request', str(request), '--receipt', str(receipt)],
            cwd=Path.home(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    return {'receipt': str(receipt), 'pid': child.pid, 'digest': payload['digest'], 'status': 'starting'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(worker(arguments.request, arguments.receipt))
