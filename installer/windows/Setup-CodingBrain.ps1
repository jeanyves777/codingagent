<#
.SYNOPSIS
  One-step Coding Brain setup, run by CodingBrain-Setup.exe after it installs the desktop.
.DESCRIPTION
  Uses the engine release files bundled in the installer (checked against their SHA256SUMS), so the
  engine always matches the desktop it ships with:
    - no engine yet        -> install.ps1 -From <bundle> -Full   (Python/Git if missing, the engine, then everything)
    - an older engine      -> codingbrain update --source <bundle>, then install --full
    - same version       -> verify the real typed conversational API; repair with a force update if needed
    - newer engine       -> verify compatibility; refuse unsafe downgrade or an unusable API
  The environment step is Coding Brain's own installer: it shows its plan, Windows still asks before
  any administrator change, and progress is checkpointed (a restart continues with install --resume).
  The installed engine must pass its real typed conversational API probe and doctor before
  setup may open the desktop. A failed setup never launches the desktop as if it succeeded.
  Nothing here stores or asks for credentials.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Bundle,
  [string]$Desktop,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'CodingBrain'),
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'Coding Brain setup'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Note([string]$Message) { Write-Host "    $Message" -ForegroundColor DarkGray }
function Finish([int]$Code, [string]$Message) {
  if ($Code -eq 0) { Write-Host "`n$Message" -ForegroundColor Green } else { Write-Host "`n$Message" -ForegroundColor Yellow }
  if ($Code -eq 0 -and $Desktop -and (Test-Path -LiteralPath $Desktop)) {
    Write-Note 'Opening Coding Brain Desktop...'
    Start-Process -FilePath $Desktop
  }
  Read-Host "`nPress Enter to close this window"
  exit $Code
}

function Test-Conversation([string]$PythonExe, [string]$Version) {
  if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    Write-Host "The installed engine interpreter is missing: $PythonExe" -ForegroundColor Yellow
    return $false
  }
  $probe = Join-Path $PSScriptRoot 'Verify-CodingBrainEngine.py'
  if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) {
    Write-Host 'Installer is missing its read-only conversational API verifier.' -ForegroundColor Yellow
    return $false
  }
  & $PythonExe -I $probe --python $PythonExe --expected-version $Version --timeout 55
  return ($LASTEXITCODE -eq 0)
}

Write-Host 'CODING BRAIN SETUP' -ForegroundColor Cyan
Write-Host 'Installs or updates the Coding Brain engine and prepares everything it needs:'
Write-Host 'Docker sandbox, local models (Ollama), OCR, vision, knowledge library and the optional Claude/Codex supervisors.'

# 1. The bundled engine files must match their checksums before anything uses them.
$manifestPath = Join-Path $Bundle 'release.json'
$sumsPath = Join-Path $Bundle 'SHA256SUMS'
if (-not (Test-Path -LiteralPath $manifestPath) -or -not (Test-Path -LiteralPath $sumsPath)) {
  Finish 1 'This installer does not contain the Coding Brain engine files. Install the engine with install.ps1, then reopen the desktop.'
}
Write-Step 'Verifying the bundled engine files'
foreach ($line in Get-Content -LiteralPath $sumsPath) {
  if (-not $line.Trim()) { continue }
  $hash, $name = $line.Trim() -split '\s+', 2
  $file = Join-Path $Bundle $name
  if (-not (Test-Path -LiteralPath $file)) { Finish 1 "Bundled file missing: $name. Reinstall from a verified CodingBrain-Setup.exe." }
  if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLowerInvariant() -ne $hash.ToLowerInvariant()) {
    Finish 1 "Checksum mismatch for $name. Nothing was installed; download the installer again."
  }
}
$bundled = (Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json).version
Write-Note "Engine in this installer: $bundled (all checksums match)"

$current = Join-Path $InstallDir 'app\current.json'
$installed = if (Test-Path -LiteralPath $current) { (Get-Content -Raw -LiteralPath $current | ConvertFrom-Json).version } else { $null }
Write-Note ("Engine on this PC: " + $(if ($installed) { $installed } else { 'not installed' }))

if (-not $Yes) {
  $answer = Read-Host "`nSet up everything now? Each component is listed first; Windows asks before administrator changes. [Y/n]"
  if ($answer -match '^(n|no)$') { Finish 0 'Setup skipped. You can run it later from the desktop: Setup > Install & verify full system.' }
}

# 2. Engine: fresh install, update, or keep.
if (-not $installed) {
  Write-Step "Installing the Coding Brain engine $bundled and preparing the full environment"
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Bundle 'install.ps1') -From $Bundle -InstallDir $InstallDir -Full -Yes
  $code = $LASTEXITCODE
  if ($code -ne 0) { Finish $code "Setup stopped (exit $code). Fix the step above, then run 'codingbrain install --resume' or re-run this installer." }
  $installed = (Get-Content -Raw -LiteralPath $current | ConvertFrom-Json).version
  $python = Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe"
  Write-Step 'Verifying the installed conversational engine'
  if (-not (Test-Conversation $python $bundled)) {
    Finish 1 "The installed engine is not ready for desktop Chat/Run/New project. Setup is incomplete; review the error above."
  }
  Write-Step 'Final engine diagnostics'
  & $python -I -m brain.local doctor
  if ($LASTEXITCODE -ne 0) { Finish 1 'The Coding Brain doctor failed. The desktop will not be opened as ready.' }
  Finish 0 'Coding Brain engine and desktop have matching verified capabilities.'
}

$python = Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe"
$env:CODINGBRAIN_HOME = $InstallDir
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
  Finish 1 "Installed engine interpreter missing: $python. Cannot safely update; run repair installation."
}

$newer = & $python -I -c "import sys; from packaging.version import Version as V; print(int(V(sys.argv[1]) > V(sys.argv[2])))" $bundled $installed
if ($LASTEXITCODE -ne 0 -or [string]$newer -notmatch '^[01]$') {
  Finish 1 'The installed engine could not compare versions. No update was attempted.'
}
$older = & $python -I -c "import sys; from packaging.version import Version as V; print(int(V(sys.argv[1]) < V(sys.argv[2])))" $bundled $installed
if ($LASTEXITCODE -ne 0 -or [string]$older -notmatch '^[01]$') {
  Finish 1 'The installed engine could not compare versions. No update was attempted.'
}
if ($older -eq '1') {
  # Do not silently downgrade the engine to a bundled, older preview.
  Write-Step "Installed engine $installed is newer than the bundled $bundled"
  if (-not (Test-Conversation $python $installed)) {
    Finish 1 'Your newer engine is not compatible with this desktop. Refusing downgrade. Install a matching newer desktop build.'
  }
  Write-Note 'Newer installed engine passed the conversational API probe; preserving it.'
} elseif ($newer -eq '1') {
  Write-Step "Updating the engine $installed -> $bundled (state is backed up; rollback with 'codingbrain rollback')"
  & $python -I -m brain.local update --source $Bundle --channel dev
  if ($LASTEXITCODE -ne 0) { Finish 1 'The bundled engine update failed. The desktop will not be opened as ready.' }
  $installed = (Get-Content -Raw -LiteralPath $current | ConvertFrom-Json).version
  $python = Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe"
  if (-not (Test-Conversation $python $bundled)) {
    Finish 1 'The newly installed engine does not expose the required conversational API. Review or rollback the update.'
  }
} else {
  Write-Step "Checking installed engine $installed capabilities, not just its version"
  if (-not (Test-Conversation $python $bundled)) {
    Write-Step "Repairing same-version engine $installed from checksum-verified bundled files"
    & $python -I -m brain.local update --source $Bundle --channel dev --force
    if ($LASTEXITCODE -ne 0) {
      Finish 1 'The same-version engine repair failed. The desktop will not be opened as ready.'
    }
    $installed = (Get-Content -Raw -LiteralPath $current | ConvertFrom-Json).version
    $python = Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe"
    if (-not (Test-Conversation $python $bundled)) {
      Finish 1 'Engine repair completed but conversational API is still unavailable. The desktop will not be opened.'
    }
  } else {
    Write-Note "Installed engine $installed is current and its conversational API is verified."
  }
}

# Prepare remaining components only after a compatible, verified engine is installed.
Write-Step 'Preparing the full environment (models, sandbox, OCR, vision, knowledge, supervisors)'
& $python -I -m brain.local install --full --yes
$code = $LASTEXITCODE
if ($code -ne 0) {
  Finish $code "Full environment setup failed (exit $code). Run 'codingbrain install --resume' to continue."
}

Write-Step 'Final checks: API capabilities and engine doctor'
if (-not (Test-Conversation $python $installed)) {
  Finish 1 'Final installed-engine API verification failed. Restart and repair before opening the desktop.'
}
& $python -I -m brain.local doctor
if ($LASTEXITCODE -ne 0) {
  Finish 1 'Coding Brain doctor failed. The desktop will not be opened as ready.'
}
Finish 0 'Coding Brain engine, conversation API and desktop are verified.'
