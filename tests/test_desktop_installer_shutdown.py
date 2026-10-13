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
    def test_real_unrelated_python_dll_holder_is_not_stopped(self):
        script = Path(__file__).resolve().parents[1] / "installer/windows/Close-CodingBrainDesktop.ps1"
        with tempfile.TemporaryDirectory() as folder:
            install = Path(folder) / "installed"
            install.mkdir()
            dll = install / "version.dll"
            shutil.copyfile(Path(os.environ["WINDIR"]) / "System32/version.dll", dll)
            holder = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), "-u", "-c",
                "import ctypes,sys,time; dll=ctypes.WinDLL(sys.argv[1]); print('LOADED',flush=True); time.sleep(120)",
                str(dll)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "LOADED")
                with self.assertRaises(PermissionError):
                    dll.unlink()
                result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script), "-InstallDir", str(install),
                    "-ReportPath", str(Path(folder) / "report.txt")], capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIsNone(holder.poll(), 'An unrelated DLL holder must be left running')
                with self.assertRaises(PermissionError):
                    dll.unlink()
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
            ownership_setup = r"""
$global:testBirth = [DateTime]::UtcNow
function Get-CimInstance {
    [pscustomobject]@{ProcessId=999; ParentProcessId=0; ExecutablePath='C:\Apps\Brain\CodingBrainDesktop.exe';
        CreationDate=$global:testBirth.AddSeconds(-10)}
    foreach ($p in @(Get-Process)) {
        if (-not $p.Id) { $p | Add-Member NoteProperty Id 123 }
        if (-not $p.StartTime) { $p | Add-Member NoteProperty StartTime $global:testBirth }
        [pscustomobject]@{ProcessId=$p.Id; ParentProcessId=$(if ($global:unowned) { 0 } else { 999 });
            ExecutablePath=$p.Path; CreationDate=$global:testBirth;
            CommandLine=$(if ($global:invalidService) { '"C:\Python\python.exe" -c "print(-m desktop_ui.server)"' }
                else { '"' + $p.Path + '" -u -m desktop_ui.server --no-open --port 0 --handshake-stdout' })}
    }
}
function Invoke-CimMethod {
    param($InputObject, $MethodName)
    [pscustomobject]@{ReturnValue=0; Sid=$(if ($global:wrongSid -and $InputObject.ProcessId -ne 999) {
        'S-1-0-0'
    } else { [Security.Principal.WindowsIdentity]::GetCurrent().User.Value })}
}
"""
            identity_setup = r"""
$global:mockProcessBody = (Get-Command Get-Process).ScriptBlock
function Get-Process {
    foreach ($p in @(& $global:mockProcessBody)) {
        if (-not $p.Id) { $p | Add-Member NoteProperty Id 123 }
        if (-not $p.StartTime) { $p | Add-Member NoteProperty StartTime $global:testBirth }
        $p
    }
}
"""
            command = ownership_setup + process_setup + identity_setup + (
                f"; & '{str(script).replace(chr(39), chr(39) * 2)}' "
                f"-InstallDir 'C:/Apps/Brain' -ReportPath '{report}'; if ($global:touches -gt 0) {{ exit 91 }}; exit $LASTEXITCODE")
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

    def test_an_unowned_dll_holder_is_not_stopped(self):
        result, report = self.run_preflight(r"""
$global:unowned = $true
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Python\python.exe'; MainWindowHandle=1; ProcessName='python'; Id=456;
        Modules=@([pscustomobject]@{FileName='C:\Apps\Brain\_internal\libcrypto-3.dll'})}
    $p | Add-Member ScriptMethod CloseMainWindow { throw 'Unrelated window was closed' }
    $p | Add-Member ScriptMethod Kill { $global:touches++; throw 'Unrelated process was killed' }
    $p
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ownership not verified; not stopped', report)

    def test_another_windows_account_is_not_stopped(self):
        result, report = self.run_preflight(r"""
$global:wrongSid = $true
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Apps\Brain\worker.exe'; MainWindowHandle=1; ProcessName='worker'; Id=123}
    $p | Add-Member ScriptMethod CloseMainWindow { throw 'Another user window was closed' }
    $p | Add-Member ScriptMethod Kill { $global:touches++; throw 'Another user process was killed' }
    $p
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ownership not verified; not stopped', report)

    def test_a_reused_pid_cannot_inherit_ownership(self):
        result, report = self.run_preflight(r"""
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Apps\Brain\worker.exe'; MainWindowHandle=1; ProcessName='worker'; Id=123;
        StartTime=$global:testBirth.AddMinutes(1)}
    $p | Add-Member ScriptMethod CloseMainWindow { throw 'Reused PID window was closed' }
    $p | Add-Member ScriptMethod Kill { $global:touches++; throw 'Reused PID was killed' }
    $p
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ownership not verified; not stopped', report)

    def test_a_different_python_command_is_not_stopped_by_desktop_ancestry(self):
        result, report = self.run_preflight(r"""
$global:invalidService = $true
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Python\python.exe'; MainWindowHandle=0; ProcessName='python'; Id=456;
        Modules=@([pscustomobject]@{FileName='C:\Apps\Brain\_internal\libcrypto-3.dll'})}
    $p | Add-Member ScriptMethod Kill { $global:touches++; throw 'Unrelated child program was killed' }
    $p
}
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ownership not verified; not stopped', report)

    def test_normal_shutdown_is_requested_and_waited_for(self):
        result, report = self.run_preflight(r"""
$global:closed = $false
function Get-Process {
    if (-not $global:closed) {
        $p = [pscustomobject]@{Path='C:\Apps\Brain\CodingBrainDesktop.exe'; MainWindowHandle=1}
        $p | Add-Member ScriptMethod CloseMainWindow { $global:closed = $true; return $true }
        $p
    }
}
""")
        self.assertEqual(result.returncode, 0, result.stderr + report)

    def test_process_that_cannot_be_stopped_still_blocks_installation(self):
        result, report = self.run_preflight(r"""
function Get-Process {
    $p = [pscustomobject]@{Path='C:\Apps\Brain\CodingBrainDesktop.exe'; MainWindowHandle=0;
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
