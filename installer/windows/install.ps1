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

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
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
  [switch]$NoPath
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

Write-Step "Coding Brain installer ($InstallDir)"
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
  Write-Host "Coding Brain $installed is already installed. Use 'codingbrain update' to update it, or rerun with -Force to repair."
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
  & $venvPython -m brain.local post-install --version $release.Version --base-python $python
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

Write-Step 'Optional components'
if (Get-Command docker -ErrorAction SilentlyContinue) { Write-Host '  Docker: found (tests run in its offline sandbox)' }
else { Write-Host '  Docker Desktop: not found. Coding Brain runs project tests in a Docker sandbox; install Docker Desktop to enable testing.' }
if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Host '  Ollama: found' }
else { Write-Host '  Ollama: not found. Install it from https://ollama.com (or: winget install Ollama.Ollama) and pull a model, e.g. ollama pull qwen2.5-coder:7b' }

Write-Host ''
Write-Host "Coding Brain $($release.Version) is installed." -ForegroundColor Green
Write-Host 'Open a new terminal (PowerShell, Windows Terminal or VS Code), then run:'
Write-Host '  codingbrain setup      # models, Claude/Codex, budgets, approvals, sandbox'
Write-Host '  codingbrain doctor     # verify everything'
Write-Host '  cd C:\path\to\project; codingbrain'
