"""Integration tests for the one-step Windows installer's real engine capability gate.

The native desktop must never treat a version string, installed model or Python
availability as proof that conversational chat is supported. Probe the actual
installed engine stdio process. No model inference or network calls are needed.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "installer" / "windows" / "Verify-CodingBrainEngine.py"
SETUP = ROOT / "installer" / "windows" / "Setup-CodingBrain.ps1"
ISS = ROOT / "installer" / "windows" / "CodingBrainDesktop.iss"


@pytest.mark.skipif(sys.platform != 'win32', reason='requires Windows PowerShell')
@pytest.mark.parametrize('missing', [None, 'wheel', 'recovery'])
def test_update_selects_manifest_wheel_among_old_installer_files(tmp_path, missing):
    version = '0.13.0.dev5+desktop44'
    for older in ('desktop24', 'desktop43', 'desktop999'):
        (tmp_path / f'coding_brain-0.13.0.dev5+{older}-py3-none-any.whl').write_bytes(b'old')
    wheel = tmp_path / f'coding_brain-{version}-py3-none-any.whl'
    recovery = tmp_path / 'Recover-CodingBrainTasks.py'
    if missing != 'wheel':
        wheel.write_bytes(b'verified target')
    if missing != 'recovery':
        recovery.write_text('# recovery')
    source = SETUP.read_text(encoding='utf-8')
    block = source[source.index('  $wheel = Join-Path'):source.index('  & $python -I $recovery')]
    escaped = str(tmp_path).replace("'", "''")
    script = tmp_path / 'selection.ps1'
    script.write_text("$ErrorActionPreference='Stop'\nfunction Finish($code,$message) { Write-Output $message; exit $code }\n"
        f"$Bundle='{escaped}'\n$bundled='{version}'\n"
        "$recovery=Join-Path $Bundle 'Recover-CodingBrainTasks.py'\n" + block + '\nWrite-Output $wheel\n')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                            capture_output=True, text=True, timeout=20)
    if missing:
        assert result.returncode == 1
        assert ('engine wheel missing' if missing == 'wheel' else 'recovery script missing') in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(wheel)


def _probe_module():
    spec = importlib.util.spec_from_file_location("installer_engine_probe", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_engine_api_probe_requires_conversation_and_task_operations():
    from brain.local.cli import version
    check = _probe_module().probe
    ok, message = check(sys.executable, expected_version=version(), timeout=55)
    assert ok, message
    assert "conversational" in message


def test_probe_rejects_a_mismatched_engine_version():
    check = _probe_module().probe
    ok, message = check(sys.executable, expected_version="99999.0.0", timeout=55)
    assert not ok and "expected" in message


def test_probe_rejects_missing_interpreter(tmp_path):
    check = _probe_module().probe
    ok, message = check(str(tmp_path / "absent-python"), timeout=5)
    assert not ok
    assert "could not complete" in message


def test_failed_engine_setup_must_not_launch_the_desktop():
    source = SETUP.read_text(encoding="utf-8")
    assert "if ($Code -eq 0 -and $Desktop" in source
    assert "Verify-CodingBrainEngine.py" in source
    assert "if (-not (Test-Conversation $python $bundled))" in source
    assert "--source $Bundle --channel dev --force" in source
    assert "Finish 1 'Engine repair completed but conversational API is still unavailable" in source
    assert "if ($LASTEXITCODE -ne 0)" in source
    assert "Write-Step 'Final checks: API capabilities and engine doctor'" in source
    assert "Refusing downgrade" in source


def test_verification_script_is_bundled_in_native_installer():
    source = ISS.read_text(encoding="utf-8")
    assert 'Source: "Verify-CodingBrainEngine.py"; DestDir: "{app}\\engine"' in source
    assert 'Source: "Setup-CodingBrain.ps1"; DestDir: "{app}\\engine"' in source
    assert "Set up Coding Brain engine and tools (recommended)" in source


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser only")
def test_installer_engine_setup_powershell_syntax():
    for script in [SETUP]:
        escaped = str(script).replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            f"$null=[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
            "[ref]$tokens,[ref]$errors);"
            "if($errors.Count){$errors|ForEach-Object{Write-Error $_.Message};exit 1}"
        )
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                              text=True, capture_output=True, timeout=25)
        assert proc.returncode == 0, proc.stderr
