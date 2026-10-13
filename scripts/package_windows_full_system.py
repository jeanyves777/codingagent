"""Package the matching desktop, complete engine release, and full Windows setup together."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def package(installer: Path, desktop: Path, engine: Path, output: Path) -> None:
    info = json.loads((engine / 'release.json').read_text(encoding='utf-8'))
    metadata = json.loads((desktop / '_internal/desktop_ui/release-version.json').read_text(encoding='utf-8'))
    if info['version'] != metadata['version']:
        raise ValueError('Desktop and engine versions do not match')
    for line in (engine / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split(None, 1)
        if hashlib.sha256((engine / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Engine checksum mismatch: {name}')
    if not (desktop / 'CodingBrainDesktop.exe').is_file() or installer.stat().st_size < 1024:
        raise ValueError('Desktop or installer is missing')

    engine_files = {p.name: p.read_bytes() for p in engine.iterdir() if p.is_file() and p.name != 'SHA256SUMS'}
    for name in ('Setup-CodingBrain.ps1', 'Setup-CodingBrain.cmd', 'Verify-CodingBrainEngine.py'):
        engine_files[name] = (ROOT / 'installer/windows' / name).read_bytes()
    engine_files['SHA256SUMS'] = ''.join(
        f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(engine_files.items())).encode('ascii')
    files = {'CodingBrain-Setup.exe': installer.read_bytes()}
    for name, data in engine_files.items():
        files['engine/' + name] = data
        files['CodingBrainDesktop/engine/' + name] = data
    for path in desktop.rglob('*'):
        if path.is_file():
            files['CodingBrainDesktop/' + path.relative_to(desktop).as_posix()] = path.read_bytes()
    files['INSTALL-FULL-SYSTEM.cmd'] = b'@echo off\r\nstart "" /wait "%~dp0CodingBrain-Setup.exe"\r\nexit /b %errorlevel%\r\n'
    files['INSTALL.txt'] = f'''CODING BRAIN - FULL WINDOWS SYSTEM
Version: {info['version']} (tested local preview)

INSTALL THE FULL SYSTEM
1. Extract the entire ZIP.
2. Run CodingBrain-Setup.exe (or INSTALL-FULL-SYSTEM.cmd).
3. Keep "Set up Coding Brain engine and tools (recommended)" checked.
4. Follow the full setup window and review its installation plans.

This package contains the desktop, complete matching engine wheel,
pinned engine dependencies, verified release files, full setup scripts,
and a portable desktop with the same engine bundle. The engine includes
the coding/task system, conversational API, project tools, document and
image tools, memory, knowledge integration, and sandbox integration.

The full setup prepares Python, Git, WSL, Docker and sandbox images,
Ollama and local models, OCR, vision, knowledge, and optional supervisors.
An internet connection is needed to download dependencies, tools and
models. This ZIP does not contain an offline copy of those downloads.
Connect your optional provider accounts on each computer separately.
Personal credentials, settings, projects, and memory are not transferred.

STRICT PROCESS OWNERSHIP
Setup only stops verified Coding Brain processes for this installation
and Windows user. It checks process creation times and desktop ancestry
or ownership records. Loading a bundled DLL alone is not permission to
stop a process. Unverified holders are reported and left running.
If an older version left services with no ownership records and no live
desktop parent, restart Windows once before upgrading. New desktop
versions record their service ownership and clean up their own services.

WINDOWS SETUP
Use Windows 10 or 11, 64-bit. A full environment can use about 25 GB.
If setup confirms that Windows needs a restart, save your work and restart.
Setup resumes after sign-in if enabled; otherwise run:
  codingbrain install --resume
For a WSL error, inspect the Windows error in administrator PowerShell:
  wsl --install --no-distribution
Official WSL help: https://learn.microsoft.com/en-us/windows/wsl/install

CONTENTS
CodingBrain-Setup.exe - native installer with the matching engine bundled
engine/ - complete engine release and full setup scripts
CodingBrainDesktop/ - portable desktop plus the same full engine bundle
SHA256SUMS - checksums for every packaged file
'''.encode('utf-8')
    files['SHA256SUMS'] = ''.join(
        f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(files.items())).encode('ascii')
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = 'CodingBrain-Full-System/'
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for name, data in files.items():
            bundle.writestr(prefix + name, data)
    with zipfile.ZipFile(output) as bundle:
        if bundle.testzip() is not None:
            raise ValueError('ZIP integrity failure')
        for line in bundle.read(prefix + 'SHA256SUMS').decode('ascii').splitlines():
            expected, name = line.split('  ', 1)
            if hashlib.sha256(bundle.read(prefix + name)).hexdigest() != expected:
                raise ValueError(f'ZIP checksum mismatch: {name}')
    output.with_suffix('.zip.sha256').write_text(
        f'{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n', encoding='ascii')
    print(f'Created and verified full system: {output} ({output.stat().st_size / 1024 / 1024:.1f} MB)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('installer', 'desktop', 'engine', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    package(args.installer, args.desktop, args.engine, args.output)
