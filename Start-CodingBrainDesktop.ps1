# Launch the native Coding Brain desktop window. Does not modify the core agent.
[CmdletBinding()]
param([switch]$InstallDesktopDependency)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cbHome = if ($env:CODINGBRAIN_HOME) { $env:CODINGBRAIN_HOME } else { Join-Path $env:LOCALAPPDATA 'CodingBrain' }
$current = Join-Path $cbHome 'app\current.json'
if (-not (Test-Path -LiteralPath $current)) { throw "Coding Brain is not installed at $cbHome" }
$active = Get-Content -Raw -LiteralPath $current | ConvertFrom-Json
$python = Join-Path $cbHome "app\versions\$($active.version)\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Coding Brain Python not found: $python" }
& $python -c 'import webview' 2>$null
if ($LASTEXITCODE -ne 0) {
    if (-not $InstallDesktopDependency) {
        throw "Native desktop support is not installed. Run this script with -InstallDesktopDependency to authorize installing pywebview, then retry."
    }
    & $python -m pip install 'pywebview>=5,<7'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install pywebview.' }
}
$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$root;$oldPythonPath"
try {
    Write-Host 'Opening Coding Brain Desktop...' -ForegroundColor Cyan
    & $python -m desktop_ui.native
    if ($LASTEXITCODE -ne 0) { throw "Coding Brain Desktop exited with status $LASTEXITCODE" }
} finally { $env:PYTHONPATH = $oldPythonPath }
