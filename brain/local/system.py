"""The computer Coding Brain is being installed on: probes and the few actions the installer takes.

Everything the installer knows about the machine comes through `System`, so the installer's
decisions can be exercised against simulated machines in tests (tests/test_installer.py), while
real runs use the real commands. A simulated machine is never reported as a tested one.

Probes are read-only. Actions are limited to running official installers (winget, npm, ollama,
docker build), starting an installed program, asking Windows for elevation through its own UAC
prompt, and registering a one-time resume after a restart, each only after the user agreed.
Nothing here changes BIOS/UEFI settings, deletes WSL distributions or Docker data, or reads
credential stores.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200 if WINDOWS else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

WINDOWS_FACTS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-CimInstance Win32_Processor | Select-Object -First 1
$c = Get-CimInstance Win32_ComputerSystem
$o = Get-CimInstance Win32_OperatingSystem
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
[pscustomobject]@{
  caption = $o.Caption; version = $o.Version; build = [int]$o.BuildNumber
  arch = $env:PROCESSOR_ARCHITECTURE; cpu = $p.Name; cores = $p.NumberOfLogicalProcessors
  ram = [double]$c.TotalPhysicalMemory
  admin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  firmware_virtualization = $p.VirtualizationFirmwareEnabled
  vm_monitor_extensions = $p.VMMonitorModeExtensions
  slat = $p.SecondLevelAddressTranslationExtensions
  hypervisor_present = $c.HypervisorPresent
  execution_policy = (Get-ExecutionPolicy).ToString()
  model = $c.Model
} | ConvertTo-Json -Compress
"""


def decode(data: bytes) -> str:
    """Command output; wsl.exe writes UTF-16 on many Windows versions."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(data) > 4 and data[1:4:2] == b"\x00\x00"):
        try:
            return data.decode("utf-16").lstrip("﻿")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


class System:
    """The real machine."""
    simulated = False

    def __init__(self):
        self.platform = "windows" if WINDOWS else ("darwin" if sys.platform == "darwin" else "linux")
        self._facts = None

    # probes ---------------------------------------------------------------------------------
    def which(self, name: str) -> str | None:
        return self.locate(name)[0]

    def locate(self, name: str) -> tuple[str | None, list[str]]:
        """Find a command without running anything: this process's PATH, then (Windows) the PATH
        saved in the registry (an installer may have changed it after this process started),
        npm's configured global prefix (read from NPM_CONFIG_PREFIX and .npmrc files, never by
        running npm) and the official default locations. Returns the path and every place checked."""
        found = shutil.which(name)
        if found or not WINDOWS:
            return found, ([] if found else ["PATH"])
        checked = ["PATH"]
        extensions = (".exe", ".cmd")
        directories = [*self._registry_path_dirs(), *npm_prefixes()]
        for directory in directories:
            for extension in extensions:
                candidate = Path(directory) / (name + extension)
                checked.append(str(candidate))
                if candidate.is_file():
                    return str(candidate), checked
        for candidate in KNOWN_LOCATIONS.get(name, ()):  # installed, but this session's PATH predates it
            path = Path(os.path.expandvars(candidate))
            checked.append(str(path))
            if path.is_file():
                return str(path), checked
        return None, list(dict.fromkeys(checked))

    def _registry_path_dirs(self) -> list[str]:
        """The user and machine PATH as saved in the registry (not this process's copy)."""
        if not WINDOWS:
            return []
        import winreg
        directories = []
        for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, key) as handle:
                    directories += [os.path.expandvars(part) for part in winreg.QueryValueEx(handle, "Path")[0].split(";")
                                    if part.strip()]
            except OSError:
                pass
        return directories

    def exists(self, path: str) -> bool:
        return Path(os.path.expandvars(path)).exists()

    def run(self, arguments: list[str], timeout: float = 60, env: dict | None = None, on_line=None,
            stdin: str | None = None) -> tuple[int, str]:
        """Run a program; returns (exit code, combined output). A missing program is 127, a
        timeout 124. `on_line` receives each output line as it arrives."""
        executable = self.which(arguments[0]) or arguments[0]
        try:
            process = subprocess.Popen([executable, *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                       env=env, creationflags=NO_WINDOW)
        except (FileNotFoundError, PermissionError, OSError) as error:
            return 127, f"{arguments[0]}: {error}"
        chunks = []

        def reader():
            for raw in iter(process.stdout.readline, b""):
                chunks.append(raw)
                if on_line:
                    line = decode(raw).replace("\x00", "").strip()
                    if line:
                        try:
                            on_line(line)
                        except Exception:
                            pass
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        if stdin is not None:
            try:
                process.stdin.write(stdin.encode())
                process.stdin.close()
            except OSError:
                pass
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = 124
        thread.join(timeout=5)
        return code, decode(b"".join(chunks)).replace("\x00", "")

    def run_interactive(self, arguments: list[str], env: dict | None = None) -> int:
        """Run with this terminal attached (official sign-in flows open a browser and may ask questions)."""
        try:
            return subprocess.call([self.which(arguments[0]) or arguments[0], *arguments[1:]], env=env)
        except OSError:
            return 127

    def powershell(self, script: str, timeout: float = 60) -> tuple[int, str]:
        shell = self.which("powershell") or self.which("pwsh")
        if not shell:
            return 127, "PowerShell not found"
        return self.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)

    def facts(self) -> dict:
        if self._facts is None:
            facts = {"platform": self.platform, "arch": platform.machine(), "python": sys.version.split()[0],
                     "cores": os.cpu_count()}
            if WINDOWS:
                code, out = self.powershell(WINDOWS_FACTS, timeout=60)
                try:
                    facts.update(json.loads(out.strip().splitlines()[-1]) if code == 0 else {})
                except (ValueError, IndexError):
                    pass
            else:
                facts.update({"caption": f"{platform.system()} {platform.release()}", "build": None,
                              "admin": hasattr(os, "geteuid") and os.geteuid() == 0})
                try:
                    pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
                    facts["ram"] = float(pages * size)
                except (ValueError, OSError, AttributeError):
                    pass
                facts["kvm"] = Path("/dev/kvm").exists()
                try:
                    flags = Path("/proc/cpuinfo").read_text()
                    facts["cpu_virtualization_flags"] = " vmx" in flags or " svm" in flags
                except OSError:
                    pass
            self._facts = facts
        return self._facts

    def disk_free(self, path: Path) -> float:
        path = Path(path)
        while not path.exists() and path.parent != path:
            path = path.parent
        return float(shutil.disk_usage(path).free)

    def gpu(self) -> dict | None:
        if not self.which("nvidia-smi"):
            return None
        code, out = self.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=20)
        if code or not out.strip():
            return None
        name, _, memory = out.strip().splitlines()[0].rpartition(",")
        try:
            return {"name": name.strip(), "vram": float(memory) * 1024 * 1024}
        except ValueError:
            return None

    def http(self, method: str, url: str, payload: dict | None = None, timeout: float = 10,
             on_json_line=None) -> tuple[int, object]:
        """(status, parsed JSON body or text); status 0 when the server could not be reached."""
        import httpx
        trust = not url.startswith(("http://localhost", "http://127.0.0.1"))
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout), trust_env=trust) as client:
                if on_json_line:
                    with client.stream(method, url, json=payload) as response:
                        last = None
                        for line in response.iter_lines():
                            if line.strip():
                                try:
                                    last = json.loads(line)
                                except ValueError:
                                    continue
                                on_json_line(last)
                        return response.status_code, last
                response = client.request(method, url, json=payload)
                try:
                    return response.status_code, response.json()
                except ValueError:
                    return response.status_code, response.text
        except httpx.HTTPError as error:
            return 0, f"{type(error).__name__}: {error}"

    def environ(self) -> dict:
        return dict(os.environ)

    # actions --------------------------------------------------------------------------------
    def refresh_path(self):
        """Pick up PATH changes an installer just made (Windows keeps them in the registry)."""
        if not WINDOWS:
            return
        import winreg
        parts = []
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(root, key) as handle:
                    parts.append(os.path.expandvars(winreg.QueryValueEx(handle, "Path")[0]))
            except OSError:
                pass
        if parts:
            os.environ["PATH"] = ";".join(parts + [os.environ.get("PATH", "")])

    def run_elevated(self, arguments: list[str], timeout: float = 3600) -> int:
        """Ask Windows for administrator rights through its own UAC prompt (never bypassed)."""
        quoted = ",".join("'" + argument.replace("'", "''") + "'" for argument in arguments[1:])
        script = (f"$p = Start-Process -FilePath '{arguments[0]}' "
                  + (f"-ArgumentList {quoted} " if quoted else "") + "-Verb RunAs -Wait -PassThru; exit $p.ExitCode")
        code, _ = self.powershell(script, timeout=timeout)
        return code

    def start_detached(self, arguments: list[str]) -> bool:
        try:
            subprocess.Popen([self.which(arguments[0]) or arguments[0], *arguments[1:]], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=DETACHED, start_new_session=not WINDOWS, close_fds=True)
            return True
        except OSError:
            return False

    def register_resume(self, command: str) -> bool:
        """Continue the installation once after the next sign-in (Windows RunOnce, current user)."""
        if not WINDOWS:
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "CodingBrainSetup", 0, winreg.REG_SZ, command)
            return True
        except OSError:
            return False

    def clear_resume(self):
        if not WINDOWS:
            return
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "CodingBrainSetup")
        except OSError:
            pass

    def signature(self, path: str) -> dict:
        """Authenticode status and signer of an installed program (Windows)."""
        if not WINDOWS:
            return {"status": "not_checked"}
        script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path.replace(chr(39), chr(39) * 2)}'; "
                  "[pscustomobject]@{status = $s.Status.ToString(); signer = $s.SignerCertificate.Subject} | "
                  "ConvertTo-Json -Compress")
        code, out = self.powershell(script, timeout=60)
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": "unknown"}

    def sleep(self, seconds: float):
        time.sleep(seconds)

    def time(self) -> float:
        return time.time()


def npm_prefixes() -> list[str]:
    """npm's global bin folders on Windows, from configuration files only (npm is never run):
    NPM_CONFIG_PREFIX, then `prefix=` in the user's and npm's global .npmrc, then the default."""
    prefixes = []
    if os.environ.get("NPM_CONFIG_PREFIX"):
        prefixes.append(os.environ["NPM_CONFIG_PREFIX"])
    candidates = [Path(os.environ.get("USERPROFILE") or Path.home()) / ".npmrc"]
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "npm" / "etc" / "npmrc")
    for rc in candidates:
        try:
            for line in rc.read_text(encoding="utf-8", errors="replace").splitlines()[:200]:
                key, _, value = line.partition("=")
                if key.strip().lower() == "prefix" and value.strip():
                    prefixes.append(os.path.expandvars(value.strip().strip('"')))
        except OSError:
            continue
    if os.environ.get("APPDATA"):
        prefixes.append(str(Path(os.environ["APPDATA"]) / "npm"))
    return list(dict.fromkeys(prefixes))


# Default install locations, used when an installer has updated PATH only for new terminals.
KNOWN_LOCATIONS = {
    "git": (r"%ProgramFiles%\Git\cmd\git.exe", r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
    "docker": (r"%ProgramFiles%\Docker\Docker\resources\bin\docker.exe",),
    "ollama": (r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",),
    "winget": (r"%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe",),
    "wsl": (r"%SystemRoot%\System32\wsl.exe",),
    "tesseract": (r"%ProgramFiles%\Tesseract-OCR\tesseract.exe",),
    "node": (r"%ProgramFiles%\nodejs\node.exe",),
    "npm": (r"%ProgramFiles%\nodejs\npm.cmd",),
    "claude": (r"%USERPROFILE%\.local\bin\claude.exe", r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe"),
    "codex": (r"%APPDATA%\npm\codex.cmd", r"%LOCALAPPDATA%\Microsoft\WinGet\Links\codex.exe",
              r"%USERPROFILE%\.local\bin\codex.exe"),
}


def assess_virtualization(facts: dict, wsl_working: bool = False, docker_linux: bool = False) -> dict:
    """Whether hardware virtualization is available, from several signals.

    Windows reports VirtualizationFirmwareEnabled = False whenever a hypervisor (Hyper-V, WSL 2,
    Virtual Machine Platform, Docker Desktop) is already running, so that flag alone never
    proves virtualization is disabled; at most it makes it possible.
    """
    evidence = []
    if docker_linux:
        evidence.append("the Docker engine runs Linux containers")
    if wsl_working:
        evidence.append("WSL 2 is working")
    if facts.get("hypervisor_present"):
        evidence.append("a hypervisor is running (Windows then reports the firmware flag as off; that is expected)")
    if evidence:
        return {"state": "enabled", "evidence": evidence}
    if facts.get("firmware_virtualization") is True:
        return {"state": "enabled", "evidence": ["the processor reports virtualization enabled in firmware"]}
    if facts.get("kvm") or facts.get("cpu_virtualization_flags"):
        return {"state": "enabled", "evidence": ["the processor advertises virtualization (vmx/svm or /dev/kvm)"]}
    if facts.get("firmware_virtualization") is False:
        return {"state": "possibly_disabled",
                "evidence": ["Windows reports firmware virtualization as off and no hypervisor is running"],
                "advice": "This flag can be wrong. Check Task Manager > Performance > CPU > 'Virtualization'. "
                          "If it says Disabled, turn on Intel VT-x or AMD-V/SVM in your BIOS/UEFI settings "
                          "yourself (Coding Brain never changes firmware settings), then run "
                          "`codingbrain install --resume`."}
    return {"state": "unknown", "evidence": ["no reliable signal was available"],
            "advice": "Installing WSL 2 will show whether virtualization works."}
