<#
.SYNOPSIS
  Install Coding Brain for the current Windows user from a verified GitHub release.

.DESCRIPTION
  Installs a published, tagged release of jeanyves777/codingagent (never a development branch head):
  downloads the release wheel, constraints and release.json, verifies each against the release's
  SHA256SUMS, installs it into its own virtual environment under
  %LOCALAPPDATA%\CodingBrain\app\versions\<version>, and puts `codingbrain` on your user PATH.
  Configuration, project memory and sessions live in %LOCALAPPDATA%\CodingBrain\config and \data,
  separate from application versions, so later `codingbrain update` runs keep them.
  No credentials are created, copied or stored.

  With -Full or -Local it then prepares the rest of the environment with your permission for each
  change (see docs/installation.md): Local adds Ollama and a local model sized for this computer;
  Full also adds WSL 2, Docker Desktop, the test sandbox images, Claude Code, Codex, the knowledge
  library and the multimodal extras. Everything comes from winget or the vendor's official channel;
  progress is checkpointed so a restart or interruption continues with -Resume.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Full
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Resume
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Version 0.9.0
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -From C:\Downloads\codingbrain-0.9.0
#>
[CmdletBinding()]
param(
  [string]$Version,
  [ValidateSet('stable', 'dev')][string]$Channel = 'stable',
  [string]$From,
  [string]$Python,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'CodingBrain'),
  [string]$Repository = 'jeanyves777/codingagent',
  [switch]$Yes,
  [switch]$Force,
  [switch]$NoPath,
  [switch]$Full,       # Coding Brain plus WSL 2, Docker Desktop, sandbox, Claude Code, Codex, knowledge, extras
  [switch]$Local,      # Coding Brain plus Ollama and a local model
  [switch]$Resume,     # continue a provisioning run after a restart or interruption
  [switch]$PlanOnly    # show the preflight and the plan; change nothing
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function Confirm-Action([string]$Question) {
  if ($Yes) { return $true }
  $answer = Read-Host "$Question [y/N]"
  return $answer -match '^(y|yes)$'
}

function Update-SessionPath {
  $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $user = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Find-Python {
  $candidates = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($minor in '3.13', '3.12', '3.11') { $candidates += , @('py', "-$minor") }
  }
  $candidates += , @('python')
  foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $arguments = @($candidate | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.executable); print(int(sys.version_info >= (3, 11)))')
    try {
      $output = @(& $exe @arguments 2>$null)
      if ($LASTEXITCODE -eq 0 -and $output -and $output.Count -ge 2 -and $output[1] -eq '1') {
        # Skips the Microsoft Store alias, which prints nothing and exits non-zero.
        return $output[0].Trim()
      }
    } catch { }
  }
  return $null
}

function Install-WithWinget([string]$Id, [string]$Name) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "$Name is required. Install it (winget is not available here), then run this installer again."
  }
  if (-not (Confirm-Action "$Name is required but was not found. Install it now with winget ($Id)?")) {
    throw "$Name is required. Install it, then run this installer again."
  }
  winget install --exact --id $Id --scope user --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) {
    winget install --exact --id $Id --accept-package-agreements --accept-source-agreements
  }
  Update-SessionPath
}

function Get-Release {
  if ($From) {
    $info = Get-Content -Raw (Join-Path $From 'release.json') | ConvertFrom-Json
    return [pscustomobject]@{ Version = $info.version; Tag = "v$($info.version)"; Assets = @{}; Local = $true }
  }
  $api = "https://api.github.com/repos/$Repository/releases"
  $headers = @{ 'User-Agent' = 'codingbrain-installer'; 'Accept' = 'application/vnd.github+json' }
  if ($Version) {
    $payload = Invoke-RestMethod -Uri "$api/tags/v$($Version.TrimStart('v'))" -Headers $headers
  } elseif ($Channel -eq 'stable') {
    $payload = Invoke-RestMethod -Uri "$api/latest" -Headers $headers
  } else {
    $payload = Invoke-RestMethod -Uri "$api`?per_page=30" -Headers $headers |
      Where-Object { -not $_.draft } | Sort-Object { [datetime]$_.published_at } -Descending | Select-Object -First 1
    if (-not $payload) { throw 'No releases are published yet.' }
  }
  $assets = @{}
  foreach ($asset in $payload.assets) { $assets[$asset.name] = $asset.browser_download_url }
  return [pscustomobject]@{ Version = $payload.tag_name.TrimStart('v'); Tag = $payload.tag_name; Assets = $assets; Local = $false }
}

function Get-Asset($Release, [string]$Name, [string]$Destination) {
  $target = Join-Path $Destination $Name
  if ($Release.Local) {
    Copy-Item -LiteralPath (Join-Path $From $Name) -Destination $target
  } else {
    if (-not $Release.Assets.ContainsKey($Name)) { throw "Release $($Release.Tag) has no asset $Name" }
    Invoke-WebRequest -Uri $Release.Assets[$Name] -OutFile $target -UseBasicParsing -Headers @{ 'User-Agent' = 'codingbrain-installer' }
  }
  return $target
}

function Show-Preflight {
  # Facts only; the detailed checks (virtualization signals, network, components) run in Python next.
  $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
  $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
  $drive = Get-PSDrive -Name ($InstallDir.Substring(0, 1)) -ErrorAction SilentlyContinue
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
  Write-Step 'This computer'
  Write-Host ("  {0} (build {1}), {2}" -f $os.Caption, $os.BuildNumber, $env:PROCESSOR_ARCHITECTURE)
  if ($cs) { Write-Host ("  memory {0:N1} GB" -f ($cs.TotalPhysicalMemory / 1GB)) }
  if ($drive) { Write-Host ("  free disk {0:N1} GB on {1}:" -f ($drive.Free / 1GB), $drive.Name) }
  Write-Host ("  administrator: {0}; winget: {1}; PowerShell {2}; execution policy {3}" -f $(if ($admin) { 'yes' } else { 'no (Windows asks when a step needs it)' }),
    $(if (Get-Command winget -ErrorAction SilentlyContinue) { 'yes' } else { 'no' }), $PSVersionTable.PSVersion, (Get-ExecutionPolicy))
  if ($os -and [int]$os.BuildNumber -lt 19041 -and $Full) {
    Write-Warning 'WSL 2 and Docker Desktop need Windows 10 version 2004 (build 19041) or newer.'
  }
}

function Get-ProvisioningArguments {
  # The Python installer is then run at script level (not inside a function), so its output and
  # prompts stay attached to this console and its exit code is the script's exit code.
  $arguments = @('-I', '-m', 'brain.local', 'install')
  if ($Full) { $arguments += '--full' } elseif ($Local) { $arguments += @('--profile', 'local') }
  if ($Resume) { $arguments += '--resume' }
  if ($PlanOnly) { $arguments += '--plan' }
  if ($Yes) { $arguments += '--yes' }
  Write-Step 'Preparing the environment (each change is listed and asked first)'
  return , $arguments
}

$provision = $Full -or $Local -or $Resume -or $PlanOnly
Write-Step "Coding Brain installer ($InstallDir)"
Show-Preflight
$python = if ($Python) { $Python } else { Find-Python }
if (-not $python) {
  Install-WithWinget 'Python.Python.3.12' 'Python 3.11 or newer'
  $python = Find-Python
  if (-not $python) { throw 'Python 3.11+ is still not available. Open a new terminal and run the installer again.' }
}
Write-Step "Python: $python"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Install-WithWinget 'Git.Git' 'Git'
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git is still not on PATH. Open a new terminal and run the installer again.' }
}
Write-Step ('Git: ' + (git --version))

$current = Join-Path $InstallDir 'app\current.json'
$release = Get-Release
if ((Test-Path $current) -and -not $Force) {
  $installed = (Get-Content -Raw $current | ConvertFrom-Json).version
  if ($provision) {
    # The application is already here; only the environment is prepared (or resumed).
    $env:CODINGBRAIN_HOME = $InstallDir
    $provisionArguments = Get-ProvisioningArguments
    & (Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe") @provisionArguments
    exit $LASTEXITCODE
  }
  Write-Host "Coding Brain $installed is already installed. Use 'codingbrain update' to update it, -Full or -Local to prepare its environment, or -Force to repair."
  exit 0
}

$download = Join-Path ([IO.Path]::GetTempPath()) ("codingbrain-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $download | Out-Null
$versionDir = Join-Path $InstallDir "app\versions\$($release.Version)"
try {
  Write-Step "Downloading $($release.Tag) and verifying checksums"
  $wheelName = "coding_brain-$($release.Version)-py3-none-any.whl"
  $sumsFile = Get-Asset $release 'SHA256SUMS' $download
  $sums = @{}
  foreach ($line in Get-Content $sumsFile) {
    if ($line.Trim()) {
      $parts = $line.Trim() -split '\s+', 2
      $sums[$parts[1].TrimStart('*').Trim()] = $parts[0].ToLowerInvariant()
    }
  }
  $files = @{}
  foreach ($name in @($wheelName, 'constraints.txt', 'release.json')) {
    if (-not $sums.ContainsKey($name)) { throw "SHA256SUMS does not cover $name" }
    $path = Get-Asset $release $name $download
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $sums[$name]) { throw "Checksum mismatch for $name. The download is corrupt or was altered; nothing was installed." }
    $files[$name] = $path
  }
  $info = Get-Content -Raw $files['release.json'] | ConvertFrom-Json
  if ($info.version -ne $release.Version) { throw "release.json says $($info.version), expected $($release.Version)" }

  Write-Step "Installing Coding Brain $($release.Version)"
  if (Test-Path $versionDir) { Remove-Item -Recurse -Force $versionDir }
  New-Item -ItemType Directory -Path $versionDir -Force | Out-Null
  & $python -m venv (Join-Path $versionDir 'venv')
  if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
  $venvPython = Join-Path $versionDir 'venv\Scripts\python.exe'
  & $venvPython -m pip install --disable-pip-version-check --no-input --constraint $files['constraints.txt'] $files[$wheelName]
  if ($LASTEXITCODE -ne 0) { throw 'Installing the release into its virtual environment failed.' }

  if ($InstallDir -ne (Join-Path $env:LOCALAPPDATA 'CodingBrain')) {
    [Environment]::SetEnvironmentVariable('CODINGBRAIN_HOME', $InstallDir, 'User')
  }
  $env:CODINGBRAIN_HOME = $InstallDir
  & $venvPython -I -m brain.local post-install --version $release.Version --base-python $python
  if ($LASTEXITCODE -ne 0) { throw 'The installation health check failed (see above).' }
} catch {
  if (-not (Test-Path $current) -or ((Get-Content -Raw $current | ConvertFrom-Json).version -ne $release.Version)) {
    Remove-Item -Recurse -Force $versionDir -ErrorAction SilentlyContinue
  }
  throw
} finally {
  Remove-Item -Recurse -Force $download -ErrorAction SilentlyContinue
}

$uninstaller = Join-Path $versionDir 'venv\Lib\site-packages\brain\local\assets\uninstall.ps1'
if (Test-Path $uninstaller) { Copy-Item $uninstaller (Join-Path $InstallDir 'uninstall.ps1') -Force }

$bin = Join-Path $InstallDir 'bin'
if (-not $NoPath) {
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  $entries = @($userPath -split ';' | Where-Object { $_ })
  if ($entries -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', (($entries + $bin) -join ';'), 'User')
    Write-Step "Added $bin to your user PATH"
  }
  Update-SessionPath
}

Write-Host ''
Write-Host "Coding Brain $($release.Version) is installed." -ForegroundColor Green
if ($provision) {
  $provisionArguments = Get-ProvisioningArguments
  & (Join-Path $versionDir 'venv\Scripts\python.exe') @provisionArguments
  $code = $LASTEXITCODE
  Write-Host ''
  Write-Host 'Open a new terminal, then: codingbrain doctor --full   (readiness level), cd C:\path\to\project; codingbrain'
  exit $code
}
Write-Host 'Coding Brain itself is ready. To prepare the rest (Ollama and a model; or everything, including WSL 2,'
Write-Host 'Docker Desktop and the sandbox), open a new terminal and run:  codingbrain install   or   codingbrain install --full'
Write-Host 'Nothing is installed without asking you first. Then:'
Write-Host '  codingbrain doctor --full   # readiness level, with real checks'
Write-Host '  cd C:\path\to\project; codingbrain'
exit 0
