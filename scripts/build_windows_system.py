"""Build this project's complete Windows distribution from the current checkout.

This is the project's build entry point, discoverable like any project's scripts.
The agent's workflow executor does not have a special route for this repository.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(command):
    print(' '.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=ROOT, check=True)


def build(output, version, compiler=None, python=None, constraints=None):
    if os.name != 'nt':
        raise ValueError('The native Windows distribution needs a Windows build machine')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    compiler = compiler or shutil.which('ISCC.exe')
    if not compiler:
        candidates = [Path(os.environ.get('LOCALAPPDATA', '')) / 'CodingBrain/build-tools/InnoSetup/ISCC.exe',
                      Path(os.environ.get('ProgramFiles(x86)', '')) / 'Inno Setup 6/ISCC.exe',
                      Path(os.environ.get('ProgramFiles', '')) / 'Inno Setup 6/ISCC.exe']
        compiler = next((p for p in candidates if p.is_file()), None)
    if not compiler or not Path(compiler).is_file():
        raise ValueError('Inno Setup compiler missing. Install Inno Setup 6 or pass --compiler <ISCC.exe>.')
    if python is None:
        environment = output / 'build-environment'
        run([sys.executable, '-m', 'venv', environment])
        python = environment / 'Scripts/python.exe'
        run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-e', ROOT,
             'pyinstaller>=6,<7', 'pywebview>=5,<7', 'packaging'])
    python = Path(python).resolve()
    run([python, '-c', 'import PyInstaller, webview, packaging'])
    run([python, ROOT / 'scripts/fetch_monaco.py'])
    assets = output / 'ui-assets/desktop_ui'
    shutil.copytree(ROOT / 'desktop_ui', assets, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    (assets / 'release-version.json').write_text(json.dumps({'version': version}), encoding='utf-8')
    engine = output / 'engine'
    command = [python, ROOT / 'scripts/build_release.py', '--out', engine, '--version', version]
    if constraints:
        command += ['--constraints', Path(constraints).resolve()]
    run(command)
    run([python, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed', '--name', 'CodingBrainDesktop',
         '--distpath', output / 'native', '--workpath', output / 'native-work', '--specpath', output / 'specs',
         '--collect-all', 'webview', '--add-data', str(assets) + os.pathsep + 'desktop_ui',
         ROOT / 'desktop_ui/launch_native.py'])
    desktop = output / 'native/CodingBrainDesktop'
    run([desktop / 'CodingBrainDesktop.exe', '--self-test'])
    run([compiler, '/Qp', '/O' + str(output), '/FCodingBrain-Setup', '/DCodingBrainVersion=' + version,
         '/DEngineBundle=' + str(engine), '/DDesktopBundle=' + str(desktop),
         ROOT / 'installer/windows/CodingBrainDesktop.iss'])
    installer = output / 'CodingBrain-Setup.exe'
    run([python, ROOT / 'scripts/package_windows_full_system.py', '--installer', installer,
         '--desktop', desktop, '--engine', engine, '--output', output / 'CodingBrain-Full-System.zip'])
    print('Full system built and verified at ' + str(output), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--build-python', type=Path)
    parser.add_argument('--constraints', type=Path)
    args = parser.parse_args()
    build(args.output, args.version, args.compiler, args.build_python, args.constraints)
