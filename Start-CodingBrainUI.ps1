# Launch the optional Coding Brain UI using the existing installed Python environment.
# No new dependencies and no modification to the Coding Brain installation.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$homeDir = if ($env:CODINGBRAIN_HOME) { $env:CODINGBRAIN_HOME } else { Join-Path $env:LOCALAPPDATA 'CodingBrain' }
$current = Join-Path $homeDir 'app\current.json'
if (-not (Test-Path -LiteralPath $current)) { throw "Coding Brain is not installed in $homeDir. Install it first." }
$active = Get-Content -Raw -LiteralPath $current | ConvertFrom-Json
if (-not $active.version) { throw 'Coding Brain installation has no active version.' }
$python = Join-Path $homeDir "app\versions\$($active.version)\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Coding Brain Python executable not found: $python" }
Write-Host 'Starting Coding Brain Workspace UI...' -ForegroundColor Cyan
Push-Location $root
try { & $python -m desktop_ui } finally { Pop-Location }
