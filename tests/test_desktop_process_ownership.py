"""Compiled Windows ownership registration, including a service left after desktop exit."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'nt' or not os.environ.get('CB_DESKTOP_NATIVE_EXE'),
                    reason='requires the compiled Windows desktop')
def test_compiled_desktop_registers_owned_orphan_and_preflight_stops_it(tmp_path):
    exe = Path(os.environ['CB_DESKTOP_NATIVE_EXE']).resolve()
    script = Path(__file__).resolve().parents[1] / 'installer/windows/Close-CodingBrainDesktop.ps1'
    marker = tmp_path / 'owned.json'
    result = subprocess.run([str(exe), '--self-test', '--ownership-probe-python',
        getattr(sys, '_base_executable', sys.executable), '--ownership-probe-dll',
        str(exe.parent / '_internal/libcrypto-3.dll'), '--ownership-probe-ready', str(marker)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    child_pid = json.loads(marker.read_text())['pid']
    key = hashlib.sha256(str(exe.parent).lower().encode('utf-8')).hexdigest()
    record = json.loads((Path(os.environ['LOCALAPPDATA']) / 'CodingBrainDesktop/process-ownership' /
                         key / f'{child_pid}.json').read_text())
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(script), '-InstallDir', str(exe.parent), '-ReportPath', str(tmp_path / 'report.txt')],
            capture_output=True, text=True, timeout=90)
        report = (tmp_path / 'report.txt').read_text() if (tmp_path / 'report.txt').exists() else ''
        assert result.returncode == 0, result.stderr + report
        remaining = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            f'if (Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) {{ exit 1 }}'], timeout=10)
        assert remaining.returncode == 0
    finally:
        # Verify its recorded creation time as well, so even test cleanup rejects PID reuse.
        subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={child_pid}'; "
            f"if ($p -and $p.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString() -eq '{record['creation_time']}') "
            f"{{ Stop-Process -Id {child_pid} -ErrorAction SilentlyContinue }}"], capture_output=True, timeout=10)
