<#
.SYNOPSIS
  Explicitly authorized Coding Brain Windows engine + desktop update.
.DESCRIPTION
  Stable: pinned owner-approved, tagged GitHub release and checksum-verified assets.
  Preview: a completed successful GitHub Actions build for the current preview branch
  head, delivered as an UNRELEASED workflow artifact and explicitly opted into.
  Both use a single version-matched wheel/desktop installer and do not install
  draft PRs or replace a newer installed engine with an older version.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$EnginePython,
  [Parameter(Mandatory=$true)][string]$BrainHome,
  [ValidateSet('stable', 'preview')][string]$Channel = 'stable'
)
$ErrorActionPreference = 'Stop'
$repo = 'jeanyves777/codingagent'
$headers = @{
  'Accept' = 'application/vnd.github+json'
  'User-Agent' = 'CodingBrainDesktop-Updater'
  'Cache-Control' = 'no-cache'
}

if (-not (Test-Path -LiteralPath $EnginePython -PathType Leaf)) { throw 'Installed Coding Brain Python was not found.' }
$currentFile = Join-Path $BrainHome 'app\current.json'
if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) { throw 'Coding Brain engine installation not found.' }

function Fail-If-LastExit([string]$action) {
  if ($LASTEXITCODE -ne 0) { throw "$action failed (exit code $LASTEXITCODE)." }
}
function Fetch-Asset([object]$release, [string]$name, [string]$folder) {
  $asset = @($release.assets | Where-Object { $_.name -ceq $name })
  if ($asset.Count -ne 1) { throw "Release $($release.tag_name) has no unique asset: $name" }
  $uri = [uri]$asset[0].browser_download_url
  if ($uri.Scheme -ne 'https' -or $uri.Host -ne 'github.com') {
    throw "Asset is not hosted by the official GitHub release: $name"
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
  if ($actual -cne $expected) { throw "SHA256 mismatch for $filename. The download will not be installed." }
  return $path
}
function GithubCli {
  $command = Get-Command gh -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  $system = Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'
  if (Test-Path -LiteralPath $system -PathType Leaf) { return $system }
  throw 'GitHub CLI is required for UNRELEASED preview artifacts. Install it and sign in with gh auth login.'
}
function Get-Verified-Preview([string]$folder) {
  $gh = GithubCli
  # Public CI artifacts are accessed using the user's official GitHub CLI session.
  # No auth cookies/tokens are passed through or printed by the Coding Brain desktop.
  & $gh auth status --hostname github.com 1>$null 2>$null
  Fail-If-LastExit 'GitHub CLI authentication'
  $ref = & $gh api "repos/$repo/git/ref/heads/preview" | ConvertFrom-Json
  Fail-If-LastExit 'Preview branch lookup'
  $head = [string]$ref.object.sha
  if ($head -cnotmatch '^[0-9a-f]{40}$') { throw 'Preview branch has no valid Git commit SHA.' }
  $runs = & $gh api "repos/$repo/actions/workflows/preview-build.yml/runs?branch=preview&per_page=30" | ConvertFrom-Json
  Fail-If-LastExit 'Preview workflow lookup'
  $run = @($runs.workflow_runs | Where-Object {
    $_.name -ceq 'Preview build (unreleased, Windows)' -and
    $_.head_branch -ceq 'preview' -and $_.head_sha -ceq $head -and
    $_.status -ceq 'completed' -and $_.conclusion -ceq 'success'
  } | Sort-Object run_number -Descending | Select-Object -First 1)
  if ($run.Count -ne 1) {
    throw "No successful Windows preview build exists for current preview commit $head. Wait for CI."
  }
  $artifactList = & $gh api "repos/$repo/actions/runs/$($run[0].id)/artifacts" | ConvertFrom-Json
  Fail-If-LastExit 'Preview artifact lookup'
  $artifact = @($artifactList.artifacts | Where-Object {
    -not $_.expired -and $_.name -match '^CodingBrain-Preview-[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+$'
  })
  if ($artifact.Count -ne 1) { throw 'Successful preview build does not contain one unexpired versioned artifact.' }
  $expectedVersion = [string]$artifact[0].name.Replace('CodingBrain-Preview-', '')
  if ($expectedVersion -cnotmatch ("\.dev" + [regex]::Escape([string]$run[0].run_number) + '$')) {
    throw 'Preview artifact version does not match the successful CI run number.'
  }
  Write-Warning "This is an UNRELEASED development preview $expectedVersion, NOT an approved GitHub Release."
  Write-Host "Source commit: $head"
  Write-Host "Passed preview CI: $($run[0].html_url)"
  & $gh run download ([string]$run[0].id) --repo $repo --name ([string]$artifact[0].name) --dir $folder
  Fail-If-LastExit 'Preview artifact download'
  $manifests = @(Get-ChildItem -LiteralPath $folder -Recurse -File -Filter 'release.json')
  if ($manifests.Count -ne 1) { throw 'Preview artifact has no unique release.json.' }
  return @{ Folder = [string]$manifests[0].DirectoryName; Version = $expectedVersion;
            Run = $run[0].id; SourceSha = $head }
}
function Check-Versions([string]$installed, [string]$requested) {
  # Python packaging uses PEP 440 ordering: 0.13.0.devN > 0.11.0 but < 0.13.0.
  # Return 0=older, 1=same, 2=newer. Never silently downgrade from preview to stable.
  $result = & $EnginePython -I -c "from packaging.version import Version; import sys; x,y=map(Version,sys.argv[1:]); print(0 if x<y else 1 if x==y else 2)" $installed $requested
  Fail-If-LastExit 'Version comparison'
  if ([string]$result -cnotmatch '^[012]$') { throw 'Version comparison returned an invalid result.' }
  return [int]$result
}

Write-Host ''
Write-Host "CODING BRAIN - $($Channel.ToUpperInvariant()) WINDOWS UPDATE" -ForegroundColor Cyan
Write-Host 'Close Coding Brain Desktop completely before updating either component.'
Read-Host 'Press Enter after closing the desktop window'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ("codingbrain-desktop-update-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
try {
  $source = $null
  $release = $null
  $preview = $null
  if ($Channel -eq 'stable') {
    $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers -TimeoutSec 30
    $tag = [string]$latest.tag_name
    if ($latest.draft -or $latest.prerelease -or $tag -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') {
      throw 'Latest release is not a published stable semantic version.'
    }
    $version = $tag.Substring(1)
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$tag" -Headers $headers -TimeoutSec 30
    if ($release.draft -or $release.prerelease -or $release.tag_name -cne $tag) {
      throw 'Pinned release metadata does not match the stable release.'
    }
    $source = $scratch
    [void](Fetch-Asset $release 'SHA256SUMS' $source)
    [void](Fetch-Asset $release 'release.json' $source)
  } else {
    $preview = Get-Verified-Preview $scratch
    $version = $preview.Version
    $source = $preview.Folder
  }
  $sumFile = Join-Path $source 'SHA256SUMS'
  if (-not (Test-Path -LiteralPath $sumFile -PathType Leaf)) { throw 'SHA256SUMS is missing.' }
  [void](Verify-Asset 'release.json' $source $sumFile)
  $info = Get-Content -LiteralPath (Join-Path $source 'release.json') -Raw | ConvertFrom-Json
  $wantedChannel = if ($Channel -eq 'preview') { 'dev' } else { 'stable' }
  if ($info.version -cne $version -or $info.channel -cne $wantedChannel) {
    throw 'The version or channel in release.json does not match the selected release.'
  }

  $installed = [string](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).version
  $cmp = Check-Versions $installed $version
  if ($cmp -eq 2) { throw "Installed engine $installed is newer than selected $Channel $version. Refusing downgrade." }

  $setup = $null
  if ($Channel -eq 'stable') {
    $installerAssets = @($release.assets | Where-Object { $_.name -ceq 'CodingBrain-Setup.exe' })
    if ($installerAssets.Count -eq 1) {
      if (-not $info.desktop -or $info.desktop.version -cne $version) {
        throw 'Stable release has a desktop asset but no matching manifest declaration.'
      }
      $setup = Fetch-Asset $release 'CodingBrain-Setup.exe' $source
      [void](Verify-Asset 'CodingBrain-Setup.exe' $source $sumFile)
    } else {
      Write-Warning "Stable v$version has no desktop installer. Only the engine can be updated."
      if ($cmp -eq 1) {
        Write-Host 'The engine is already current. No changes are needed.'
        exit 0
      }
      $answer = Read-Host 'Proceed with an engine-only update? (y/N)'
      if ($answer -cne 'y' -and $answer -cne 'Y') { Write-Host 'No changes made.'; exit 0 }
    }
  } else {
    if (-not $info.desktop -or $info.desktop.version -cne $version) {
      throw 'This preview did not package a matching desktop installer; use the manual engine-only installer instead.'
    }
    $setup = Verify-Asset 'CodingBrain-Setup.exe' $source $sumFile
    Write-Host "Preview $version checksum-verified from successful Windows CI." -ForegroundColor Green
  }

  if ($cmp -eq 0) {
    Write-Host "Installing verified Coding Brain backend $version..."
    # Read recovery logic from the verified target wheel before invoking an older
    # installed updater whose guard cannot distinguish crashed tasks from live work.
    $wheelName = "coding_brain-$version-py3-none-any.whl"
    if ($Channel -eq 'stable') { [void](Fetch-Asset $release $wheelName $source) }
    $wheel = Verify-Asset $wheelName $source $sumFile
    $recovery = Join-Path $PSScriptRoot 'Recover-CodingBrainTasks.py'
    if (-not (Test-Path -LiteralPath $recovery -PathType Leaf)) { throw 'Task recovery helper is missing.' }
    & $EnginePython -I $recovery --wheel $wheel --home $BrainHome
    Fail-If-LastExit 'Task ownership recovery'
    if ($Channel -eq 'preview') {
      # The existing engine updater validates every wheel/constraints/release.json
      # against SHA256SUMS before installation; no GitHub Release tag is faked.
      & $EnginePython -I -m brain.local update --source $source
    } else {
      & $EnginePython -I -m brain.local update --version $version
    }
    Fail-If-LastExit 'Backend update'
    $result = [string](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).version
    if ($result -cne $version) { throw "Backend still reports $result; expected $version." }
    Write-Host "Backend $version installed." -ForegroundColor Green
  } else {
    Write-Host "Backend $version already installed. No engine reinstall necessary."
  }

  if ($setup) {
    Write-Host "Installing native Coding Brain Desktop $version. Keep the desktop closed."
    $process = Start-Process -FilePath $setup -ArgumentList @('/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    if ($process.ExitCode -ne 0) {
      throw "Desktop install failed (exit $($process.ExitCode)); backend $version remains installed."
    }
    Write-Host "Native desktop $version installed." -ForegroundColor Green
  } else {
    Write-Warning 'The existing native desktop EXE was NOT updated (engine-only stable release).'
  }
  Write-Host 'Update complete. Restart Coding Brain Desktop.' -ForegroundColor Green
} finally {
  Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}
