"""Exercise the installer's shutdown preflight with real Windows PowerShell."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class DesktopInstallerShutdownTests(unittest.TestCase):
    def run_preflight(self, process_setup):
        script = Path(__file__).resolve().parents[1] / "installer/windows/Close-CodingBrainDesktop.ps1"
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.txt"
            command = process_setup + (
                f"; & '{str(script).replace(chr(39), chr(39) * 2)}' "
                f"-InstallDir 'C:/Apps/Brain' -ReportPath '{report}'; exit $LASTEXITCODE")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30)
            return result, report.read_text() if report.exists() else ""

    def test_other_installations_are_not_closed(self):
        result, report = self.run_preflight(r"""
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Apps\Brain-other\app.exe'; MainWindowHandle=1}
    $p | Add-Member ScriptMethod CloseMainWindow { throw 'Closed unrelated app' }
    $p
}
""")
        self.assertEqual(result.returncode, 0, result.stderr + report)

    def test_normal_shutdown_is_requested_and_waited_for(self):
        result, report = self.run_preflight(r"""
$global:closed = $false
function Get-Process {
    if (-not $global:closed) {
        $p = [pscustomobject]@{Path='C:\Apps\Brain\app.exe'; MainWindowHandle=1}
        $p | Add-Member ScriptMethod CloseMainWindow { $global:closed = $true; return $true }
        $p
    }
}
""")
        self.assertEqual(result.returncode, 0, result.stderr + report)

    def test_headless_blocker_is_reported_without_termination(self):
        result, report = self.run_preflight(r"""
function Get-Process {
    [pscustomobject]@{Path='C:\Apps\Brain\worker.exe'; MainWindowHandle=0;
        ProcessName='worker'; Id=123}
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('worker (PID 123)', report)

    def test_process_query_failure_blocks_installation(self):
        result, report = self.run_preflight("function Get-Process { throw 'Process query failed' }")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Process query failed', report)

    def test_external_python_loading_installed_dll_blocks_installation(self):
        result, report = self.run_preflight(r"""
function Get-Process {
    [pscustomobject]@{Path='C:\Python\python.exe'; MainWindowHandle=0;
        ProcessName='python'; Id=456;
        Modules=@([pscustomobject]@{FileName='C:\Apps\Brain\_internal\libcrypto-3.dll'})}
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('python (PID 456)', report)
