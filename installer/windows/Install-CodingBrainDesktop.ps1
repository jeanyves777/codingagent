<#
.SYNOPSIS
  Install the verified local CodingBrainDesktop portable folder as a Start Menu app.
.DESCRIPTION
  Does not change the installed Coding Brain version or third-party components.
  A signed or independently verified package source is required for production.
.EXAMPLE
  .\Install-CodingBrainDesktop.ps1 -SourceFolder C:\Downloads\CodingBrainDesktop
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$SourceFolder,
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'CodingBrain\desktop'),
  [string]$Version = 'dev',
  [string]$ExpectedSha256,
  [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceFolder).Path
$exe = Join-Path $source 'CodingBrainDesktop.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Desktop executable missing: $exe" }
if ($ExpectedSha256) {
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
  if ($actual -ne $ExpectedSha256.ToLowerInvariant()) { throw 'Desktop executable SHA256 mismatch.' }
}
if ($Version -notmatch '^[a-zA-Z0-9._-]{1,40}$') { throw 'Invalid version label' }
$target = Join-Path $InstallRoot "versions\$Version"
if (Test-Path -LiteralPath $target) { throw "Version $Version already installed; refusing to overwrite it." }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
$staging = "$target.staging.$PID"
try {
  Copy-Item -LiteralPath $source -Destination $staging -Recurse
  if (-not (Test-Path -LiteralPath (Join-Path $staging 'CodingBrainDesktop.exe'))) { throw 'Staging validation failed' }
  Move-Item -LiteralPath $staging -Destination $target
} finally {
  if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Force -Recurse }
}
if (-not $NoShortcut) {
  $start = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
  New-Item -ItemType Directory -Path $start -Force | Out-Null
  $link = Join-Path $start 'Coding Brain Desktop.lnk'
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($link)
  $shortcut.TargetPath = Join-Path $target 'CodingBrainDesktop.exe'
  $shortcut.WorkingDirectory = $target
  $shortcut.Description = 'Coding Brain autonomous coding workspace'
  $shortcut.Save()
}
Write-Host "Coding Brain Desktop installed to $target" -ForegroundColor Green
Write-Host 'This UI uses your existing Coding Brain engine; it never installs another model or Python.'
