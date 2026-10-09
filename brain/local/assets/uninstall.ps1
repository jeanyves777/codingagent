<#
.SYNOPSIS
  Uninstall Coding Brain for the current user. Your projects are never touched.

.DESCRIPTION
  Removes the application versions, the launcher and the PATH entry. Configuration, project memory,
  sessions and backups are kept unless -RemoveData is given. Branches Coding Brain created in your
  repositories (codingbrain/...) are left in place.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = $(if ($env:CODINGBRAIN_HOME) { $env:CODINGBRAIN_HOME } else { Join-Path $env:LOCALAPPDATA 'CodingBrain' }),
  [switch]$RemoveData,
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path (Join-Path $InstallDir 'app'))) { Write-Host "Coding Brain is not installed in $InstallDir"; exit 0 }
foreach ($lock in Get-ChildItem (Join-Path $InstallDir 'locks') -Filter 'session-*.json' -ErrorAction SilentlyContinue) {
  $info = Get-Content -Raw $lock.FullName | ConvertFrom-Json
  if (Get-Process -Id $info.pid -ErrorAction SilentlyContinue) {
    throw "A Coding Brain session is running (pid $($info.pid), project $($info.project)). Close it first."
  }
}
if (-not $Yes) {
  $what = if ($RemoveData) { 'the application AND all Coding Brain configuration, memory and backups' } else { 'the application (configuration and project memory are kept)' }
  if ((Read-Host "Remove $what from $InstallDir? [y/N]") -notmatch '^(y|yes)$') { exit 1 }
}

$projectRoots = @()
if ($RemoveData) {
  foreach ($record in Get-ChildItem (Join-Path $InstallDir 'data\projects') -Filter 'project.json' -Recurse -ErrorAction SilentlyContinue) {
    $projectRoots += (Get-Content -Raw $record.FullName | ConvertFrom-Json).root
  }
}

Remove-Item -Recurse -Force (Join-Path $InstallDir 'app'), (Join-Path $InstallDir 'bin') -ErrorAction SilentlyContinue
$bin = Join-Path $InstallDir 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
[Environment]::SetEnvironmentVariable('Path', ((@($userPath -split ';') | Where-Object { $_ -and $_ -ne $bin }) -join ';'), 'User')

if ($RemoveData) {
  Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
  [Environment]::SetEnvironmentVariable('CODINGBRAIN_HOME', $null, 'User')
  foreach ($root in $projectRoots) {
    # Only forgets the deleted task worktrees; never changes project files or branches.
    if ($root -and (Test-Path $root)) { try { git -C $root worktree prune 2>$null } catch { } }
  }
  Write-Host 'Coding Brain and its data were removed. Your projects were not changed.'
} else {
  Write-Host "Coding Brain was removed. Configuration and project memory remain in $InstallDir (delete with -RemoveData)."
}
