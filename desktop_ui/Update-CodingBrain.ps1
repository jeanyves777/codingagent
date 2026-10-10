<#
.SYNOPSIS
  Update both the installed Coding Brain engine and the native Windows desktop from one
  published GitHub release. Launched by the desktop Update button in a separate console.
.DESCRIPTION
  Never uses branch-head / workflow artifacts. The desktop installer must match the
  SHA256SUMS manifest from the same published, tagged GitHub release.
  Keeps engine state and settings through the official Coding Brain updater. Never modifies
  running desktop files: asks the user to close the GUI first.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$EnginePython,
  [Parameter(Mandatory=$true)][string]$BrainHome
)
$ErrorActionPreference = 'Stop'
$repo = 'jeanyves777/codingagent'
$headers = @{
  'Accept' = 'application/vnd.github+json'
  'User-Agent' = 'CodingBrainDesktop-Updater'
  'Cache-Control' = 'no-cache'
}
if (-not (Test-Path -LiteralPath $EnginePython -PathType Leaf)) { throw 'Installed Coding Brain Python was not found.' }
if (-not (Test-Path -LiteralPath (Join-Path $BrainHome 'app\current.json'))) {
  throw 'Coding Brain engine installation not found. Install it first.'
}
function Fail-If-LastExit([string]$action) {
  if ($LASTEXITCODE -ne 0) { throw "$action failed (exit code $LASTEXITCODE)." }
}
function Fetch-Asset([object]$release, [string]$name, [string]$folder) {
  $asset = @($release.assets | Where-Object { $_.name -ceq $name })
  if ($asset.Count -ne 1) { throw "Release $($release.tag_name) has no unique asset: $name" }
  $uri = [uri]$asset[0].browser_download_url
  if ($uri.Scheme -ne 'https' -or $uri.Host -ne 'github.com') {
    throw "Asset download is not from the official GitHub host: $name"
  }
  $destination = Join-Path $folder $name
  Invoke-WebRequest -Uri $uri.AbsoluteUri -Headers $headers -UseBasicParsing -OutFile $destination
  return $destination
}
function Expected-Sha256([string]$sumFile, [string]$filename) {
  $found = @()
  foreach ($line in (Get-Content -LiteralPath $sumFile)) {
    if ($line -match '^([0-9a-fA-F]{64})\s+(.+)$' -and $Matches[2] -ceq $filename) {
      $found += $Matches[1].ToLowerInvariant()
    }
  }
  if ($found.Count -ne 1) { throw "SHA256SUMS has no unique checksum for $filename" }
  return $found[0]
}
function Verify-Asset([string]$filename, [string]$folder, [string]$sums) {
  $path = Join-Path $folder $filename
  $expected = Expected-Sha256 $sums $filename
  $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -cne $expected) { throw "SHA256 mismatch: $filename. Nothing from this download will be run." }
  return $path
}

Write-Host ''
Write-Host 'CODING BRAIN - VERIFIED WINDOWS UPDATE' -ForegroundColor Cyan
Write-Host 'This updates the official engine and, when published, the native desktop application.'
Write-Host 'First, close Coding Brain Desktop completely, including its native window.'
Read-Host 'Press Enter after closing Coding Brain Desktop'
$latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers -TimeoutSec 30
$version = [string]$latest.tag_name
if ($latest.draft -or $latest.prerelease -or $version -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') {
  throw 'The latest release is not an official stable semantic version.'
}
$version = $version.Substring(1)
Write-Host "Verified stable release candidate: v$version"
# Force both components to use the exact same published release tag; never follow an
# unpinned "latest" link between the engine and installer downloads.
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/v$version" -Headers $headers -TimeoutSec 30
if ($release.draft -or $release.prerelease -or $release.tag_name -cne "v$version") {
  throw 'Pinned release metadata does not match the approved stable release.'
}
$installerAsset = @($release.assets | Where-Object { $_.name -ceq 'CodingBrain-Setup.exe' })
if ($installerAsset.Count -ne 1) {
  # The official v0.11.0 and older releases contain only the engine.
  # Do not pretend this will update the desktop; let the user opt in to an engine-only update.
  Write-Warning "v$version contains no CodingBrain-Setup.exe. This release updates only the engine."
  $answer = Read-Host 'Update the engine only? (y/N)'
  if ($answer -cne 'y' -and $answer -cne 'Y') {
    Write-Host 'No changes made.'
    exit 0
  }
}

$folder = Join-Path ([IO.Path]::GetTempPath()) ("codingbrain-desktop-update-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $folder -Force | Out-Null
try {
  $setup = $null
  if ($installerAsset.Count -eq 1) {
    $sums = Fetch-Asset $release 'SHA256SUMS' $folder
    $manifest = Fetch-Asset $release 'release.json' $folder
    [void](Verify-Asset 'release.json' $folder $sums)
    $info = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    if ($info.version -cne $version -or $info.channel -cne 'stable') {
      throw 'Release metadata and published assets disagree.'
    }
    if (-not $info.desktop -or $info.desktop.version -cne $version) {
      throw 'Desktop asset was not declared by the verified release manifest.'
    }
    $setup = Fetch-Asset $release 'CodingBrain-Setup.exe' $folder
    [void](Verify-Asset 'CodingBrain-Setup.exe' $folder $sums)
    Write-Host 'Desktop installer SHA256 verified against the tagged release.' -ForegroundColor Green
  }

  Write-Host "Updating the backend to v$version through Coding Brain's official updater..."
  & $EnginePython -I -m brain.local update --version $version
  Fail-If-LastExit 'Backend update'
  $current = Get-Content -LiteralPath (Join-Path $BrainHome 'app\current.json') -Raw | ConvertFrom-Json
  if ($current.version -cne $version) { throw "Engine still reports $($current.version), expected $version" }
  Write-Host "Backend v$version installed." -ForegroundColor Green

  if ($setup) {
    # The installer updates only the native app. Inno Setup's AppId preserves the user's
    # Start Menu shortcut; config, state and project files are outside its target directory.
    Write-Host "Installing desktop v$version (the application must remain closed)..."
    $process = Start-Process -FilePath $setup -ArgumentList @('/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -PassThru -Wait
    if ($process.ExitCode -ne 0) {
      throw "Desktop installer failed (exit code $($process.ExitCode)); backend v$version remains installed."
    }
    Write-Host "Desktop v$version installed." -ForegroundColor Green
  } else {
    Write-Warning "Desktop was NOT updated: v$version has no desktop installer. Install a future unified release once available."
  }
  Write-Host 'Update complete. You can restart Coding Brain Desktop now.' -ForegroundColor Green
} finally {
  Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue
}
