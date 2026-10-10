<#
.SYNOPSIS
  One-step Coding Brain setup, run by CodingBrain-Setup.exe after it installs the desktop.
.DESCRIPTION
  Uses the engine release files bundled in the installer (checked against their SHA256SUMS), so the
  engine always matches the desktop it ships with:
    - no engine yet        -> install.ps1 -From <bundle> -Full   (Python/Git if missing, the engine, then everything)
    - an older engine      -> codingbrain update --source <bundle>, then install --full
    - same or newer engine -> install --full only (adds whatever is still missing; safe to re-run)
  The environment step is Coding Brain's own installer: it shows its plan, Windows still asks before
  any administrator change, and progress is checkpointed (a restart continues with install --resume).
  Finally runs doctor and opens the desktop. Nothing here stores or asks for credentials.
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
  if ($Desktop -and (Test-Path -LiteralPath $Desktop)) {
    Write-Note 'Opening Coding Brain Desktop...'
    Start-Process -FilePath $Desktop
  }
  Read-Host "`nPress Enter to close this window"
  exit $Code
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
  Finish 0 'Coding Brain is installed and set up.'
}

$python = Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe"
$env:CODINGBRAIN_HOME = $InstallDir
$newer = & $python -I -c "import sys; from packaging.version import Version as V; print(int(V(sys.argv[1]) > V(sys.argv[2])))" $bundled $installed
if ($newer -eq '1') {
  Write-Step "Updating the engine $installed -> $bundled (state is backed up; rollback with 'codingbrain rollback')"
  & $python -I -m brain.local update --source $Bundle --channel dev
  if ($LASTEXITCODE -ne 0) {
    Write-Host "The engine update was not applied (see the reason above). Continuing with $installed." -ForegroundColor Yellow
  } else {
    $installed = (Get-Content -Raw -LiteralPath $current | ConvertFrom-Json).version
    $python = Join-Path $InstallDir "app\versions\$installed\venv\Scripts\python.exe"
  }
} else {
  Write-Note "The installed engine is current; keeping $installed."
}

# 3. Environment: Coding Brain's own installer adds whatever is still missing.
Write-Step 'Preparing the full environment (models, sandbox, OCR, vision, knowledge, supervisors)'
& $python -I -m brain.local install --full --yes
$code = $LASTEXITCODE

Write-Step 'Final check'
& $python -I -m brain.local doctor
if ($code -ne 0) { Finish $code "Some steps need attention (exit $code). Run 'codingbrain install --resume' after fixing them." }
Finish 0 'Coding Brain is set up.'
