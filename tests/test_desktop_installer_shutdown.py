"""Exercise the installer's shutdown preflight with real Windows PowerShell."""
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class DesktopInstallerShutdownTests(unittest.TestCase):
    def test_real_external_python_dll_holder_is_stopped_and_the_file_is_released(self):
        script = Path(__file__).resolve().parents[1] / "installer/windows/Close-CodingBrainDesktop.ps1"
        with tempfile.TemporaryDirectory() as folder:
            install = Path(folder) / "installed"
            install.mkdir()
            dll = install / "version.dll"
            shutil.copyfile(Path(os.environ["WINDIR"]) / "System32/version.dll", dll)
            holder = subprocess.Popen([sys.executable, "-u", "-c",
                "import ctypes,sys,time; dll=ctypes.WinDLL(sys.argv[1]); print('LOADED',flush=True); time.sleep(120)",
                str(dll)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "LOADED")
                with self.assertRaises(PermissionError):
                    dll.unlink()
                result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script), "-InstallDir", str(install),
                    "-ReportPath", str(Path(folder) / "report.txt")], capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                holder.wait(timeout=10)
                dll.unlink()  # The same operation that failed before the preflight now succeeds.
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=10)
                holder.stdout.close()
                holder.stderr.close()

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

    def test_process_that_cannot_be_stopped_still_blocks_installation(self):
        result, report = self.run_preflight(r"""
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Apps\Brain\worker.exe'; MainWindowHandle=0;
        ProcessName='worker'; Id=123}
    $p | Add-Member ScriptMethod Kill { throw 'Access denied' }
    $p
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('worker (PID 123)', report)

    def test_process_query_failure_blocks_installation(self):
        result, report = self.run_preflight("function Get-Process { throw 'Process query failed' }")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Process query failed', report)

    def test_external_python_loading_installed_dll_is_stopped_automatically(self):
        result, report = self.run_preflight(r"""
$global:stopped = $false
function Get-Process {
    if ($global:stopped) { return }
    $p = [pscustomobject]@{Path='C:\Python\python.exe'; MainWindowHandle=0;
        ProcessName='python'; Id=456;
        Modules=@([pscustomobject]@{FileName='C:\Apps\Brain\_internal\libcrypto-3.dll'})}
    $p | Add-Member ScriptMethod Kill { $global:stopped = $true }
    $p
}
""")
        self.assertEqual(result.returncode, 0, result.stderr + report)
