<#
.SYNOPSIS
  Upgrade verification from the published v0.9.0 release to this build, on Windows.

.DESCRIPTION
  Installs the real v0.9.0 release assets (downloaded from GitHub and checked against their
  SHA256SUMS) with the published install.ps1, creates configuration, a project, a saved task
  (session) and memory, then updates with `codingbrain update`, exactly as a user would. Checks
  that state survives, that multimodal capabilities are registered and work on Windows
  (document parsing, OCR when Tesseract is installed, the Edge browser for visual checks), that
  a failing update is rolled back automatically, and that `codingbrain rollback` returns to
  v0.9.0 with working state. Uses throwaway folders; never touches an existing installation.

    python scripts\build_release.py --out dist\new
    powershell -ExecutionPolicy Bypass -File installer\windows\verify-upgrade.ps1 -New dist\new
#>
param(
  [Parameter(Mandatory = $true)][string]$New,
  [string]$Published = 'v0.9.0',
  [string]$Repository = 'jeanyves777/codingagent',
  [string]$PublishedDir = ''  # the published assets, already downloaded (CI uses gh release download)
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 may not offer TLS 1.2, which GitHub requires.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$New = (Resolve-Path $New).Path
if ($PublishedDir) { $PublishedDir = (Resolve-Path $PublishedDir).Path }
$work = Join-Path ([IO.Path]::GetTempPath()) ("cb-upgrade-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$home_ = Join-Path $work 'CodingBrain'
$results = [ordered]@{}
$savedUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$savedHome = [Environment]::GetEnvironmentVariable('CODINGBRAIN_HOME', 'User')
$shellExe = (Get-Process -Id $PID).Path
$pythonExe = (Get-Command python).Source

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
  $results[$Name] = [ordered]@{ ok = $Ok; detail = $Detail }
  $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
  Write-Host "[$mark] $Name $Detail"
}

function CB {
  $ErrorActionPreference = 'Continue'
  $output = & cmd.exe /d /c codingbrain @args 2>&1 | Out-String
  $code = $LASTEXITCODE
  Write-Host ("--- codingbrain {0} (exit {1})`n{2}" -f ($args -join ' '), $code, $output.TrimEnd())
  return [pscustomobject]@{ Code = $code; Text = $output }
}

function Write-Sums($folder) {
  $lines = Get-ChildItem $folder -File | Where-Object { $_.Name -ne 'SHA256SUMS' } | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower(), $_.Name }
  [IO.File]::WriteAllText((Join-Path $folder 'SHA256SUMS'), (($lines -join "`n") + "`n"))
}

$newVersion = (Get-Content -Raw (Join-Path $New 'release.json') | ConvertFrom-Json).version
try {
  New-Item -ItemType Directory -Path $work | Out-Null
  # 1. The published release, verified against its own checksums
  $published = Join-Path $work 'published'
  New-Item -ItemType Directory -Path $published | Out-Null
  $base = "https://github.com/$Repository/releases/download/$Published"
  $oldVersion = $Published.TrimStart('v')
  foreach ($name in @('SHA256SUMS', 'release.json', 'constraints.txt', 'install.ps1', 'uninstall.ps1', "coding_brain-$oldVersion-py3-none-any.whl")) {
    $target = Join-Path $published $name
    if ($PublishedDir) { Copy-Item (Join-Path $PublishedDir $name) $target; continue }
    foreach ($attempt in 1..4) {
      try { Invoke-WebRequest -UseBasicParsing -Uri "$base/$name" -OutFile $target; break }
      catch {
        if ($attempt -eq 4) { throw "download of $base/$name failed: $($_.Exception.Message)" }
        Start-Sleep ([math]::Pow(2, $attempt))
      }
    }
  }
  $sumsOk = $true
  foreach ($line in Get-Content (Join-Path $published 'SHA256SUMS')) {
    $hash, $file = $line -split '\s+', 2
    if ((Get-FileHash -Algorithm SHA256 (Join-Path $published $file)).Hash.ToLower() -ne $hash) { $sumsOk = $false }
  }
  Check 'published release downloaded and checksums match' $sumsOk $Published
  & $shellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $published 'install.ps1') -From $published -InstallDir $home_ -Python $pythonExe -Yes
  Check 'published installer installs v0.9.0' ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE"
  $env:Path = "$home_\bin;" + $env:Path
  $env:CODINGBRAIN_HOME = $home_
  $v = CB --version
  Check 'v0.9.0 runs' ($v.Text -match [regex]::Escape($oldVersion)) $v.Text.Trim()

  # 2. State a v0.9.0 user would have: settings, a project, a saved task, project data
  $project = Join-Path $work 'Projects\Shop'
  New-Item -ItemType Directory -Path $project | Out-Null
  Set-Content -Path "$project\index.html" -Value '<!doctype html><html lang="en"><head><title>Shop</title><meta name="viewport" content="width=device-width"></head><body><h1>Shop</h1><button>Buy</button></body></html>'
  git -C $project init -q; git -C $project add -A; git -C $project -c user.email=v@v -c user.name=v commit -qm init
  Push-Location $project
  try {
    CB init | Out-Null
    $setup = CB setup --non-interactive --model qwen2.5-coder:7b --fast-model qwen2.5-coder:3b --execution auto --no-enable-claude --no-enable-codex
    Check 'v0.9.0 setup saved' ($setup.Code -eq 0)
  } finally { Pop-Location }
  $projectData = (Get-ChildItem "$home_\data\projects" -Directory | Where-Object { $_.Name -like 'Shop-*' }).FullName
  $oldPython = Join-Path $home_ "app\versions\$oldVersion\venv\Scripts\python.exe"
  $taskScript = "import sys; from pathlib import Path; from brain.store import Store; Store(Path(sys.argv[1]) / 'brain.sqlite3').save({'id': 'f' * 32, 'kind': 'task', 'repository': 'Shop', 'goal': 'saved session from 0.9.0', 'status': 'proposed', 'events': [{'time': '2026-10-09T00:00:00', 'kind': 'created', 'detail': ''}]})"
  & $oldPython -I -c $taskScript $projectData
  Push-Location $project
  try { $tasks = CB tasks } finally { Pop-Location }
  Check 'v0.9.0 has a saved task' ($tasks.Text -match 'saved session from 0.9.0')
  Set-Content -Path "$projectData\sentinel.txt" -Value 'keep me'

  # 3. A failing update is rolled back automatically (dependency that cannot be installed)
  $broken = Join-Path $work 'broken'
  Copy-Item -Recurse $New $broken
  Add-Content -Path (Join-Path $broken 'constraints.txt') -Value 'coding-brain-missing-dependency==99.0'
  $info = Get-Content -Raw (Join-Path $broken 'release.json') | ConvertFrom-Json
  $wheel = Join-Path $broken "coding_brain-$newVersion-py3-none-any.whl"
  & $pythonExe -c "import sys, zipfile, shutil; src = sys.argv[1]; tmp = src + '.tmp'; zin = zipfile.ZipFile(src); zout = zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED); [zout.writestr(i, zin.read(i.filename) if not i.filename.endswith('METADATA') else zin.read(i.filename) + b'Requires-Dist: coding-brain-missing-dependency\n') for i in zin.infolist()]; zout.close(); zin.close(); shutil.move(tmp, src)" $wheel
  Write-Sums $broken
  $failed = CB update --source $broken
  $after = CB --version
  Check 'failing update rolled back, v0.9.0 still active' ($failed.Code -ne 0 -and $after.Text -match [regex]::Escape($oldVersion)) $after.Text.Trim()
  Push-Location $project
  try { $tasks = CB tasks } finally { Pop-Location }
  Check 'state intact after the failed update' ($tasks.Text -match 'saved session from 0.9.0' -and (Get-Content "$projectData\sentinel.txt") -eq 'keep me') ''

  # 4. The real update: codingbrain update
  $check = CB update --check --source $New
  Check 'update --check sees the new release' ($check.Text -match [regex]::Escape($newVersion)) $check.Text.Trim()
  $update = CB update --source $New
  $after = CB --version
  Check "update from $oldVersion to $newVersion" ($update.Code -eq 0 -and $after.Text -match [regex]::Escape($newVersion)) $after.Text.Trim()
  $config = Get-Content -Raw "$home_\config\config.json" | ConvertFrom-Json
  Check 'provider settings preserved' ($config.models.model -eq 'qwen2.5-coder:7b' -and $config.models.fast_model -eq 'qwen2.5-coder:3b' -and $config.autonomy.execution -eq 'auto') ''
  Check 'multimodal capabilities registered' ($config.vision -ne $null -and $config.ocr.engine -eq 'tesseract' -and $config.visual.enabled -eq $true) ''
  Push-Location $project
  try {
    $tasks = CB tasks
    Check 'saved sessions preserved' ($tasks.Text -match 'saved session from 0.9.0')
    $memory = CB memory status
    Check 'project memory works after the update' ($memory.Code -eq 0 -and $memory.Text -match 'records') ''
    $doctor = CB doctor --offline --json
    $report = $doctor.Text | ConvertFrom-Json
    $documents = $report.checks | Where-Object { $_.name -eq 'documents' }
    Check 'health check with document parsing' ($report.ok -and $documents.ok) $documents.detail
    $online = CB doctor
    Check 'doctor reports each capability' ($online.Text -match 'vision' -and $online.Text -match 'ocr' -and $online.Text -match 'documents' -and $online.Text -match 'browser' -and $online.Text -match 'premium: claude') ''

    # 5. Multimodal on Windows: attachments, OCR (if installed), browser checks through Edge
    $png = Join-Path $work 'error.png'
    $docx = Join-Path $work 'spec.docx'
    & $pythonExe -c "import sys; sys.path.insert(0, sys.argv[3]); import multimodal_fixtures as f; from pathlib import Path; f.ui_image(Path(sys.argv[1])); f.docx(Path(sys.argv[2]), ['The Buy button must be green.'], [['Plan', 'Price'], ['Pro', '9']])" $png $docx (Join-Path $PSScriptRoot '..\..\tests')
    $preview = CB attachments preview $png $docx
    Check 'attachments preview (image + Word) on Windows' ($preview.Code -eq 0 -and $preview.Text -match 'Buy button must be green' -and $preview.Text -match 'error.png') ''
    $tesseract = (Get-Command tesseract -ErrorAction SilentlyContinue) -or (Test-Path 'C:\Program Files\Tesseract-OCR\tesseract.exe')
    if ($tesseract) {
      Check 'OCR on Windows reads the screenshot' ($preview.Text -match 'payment failed') ''
    }
    $exe = Join-Path $work 'tool.png'
    [IO.File]::WriteAllBytes($exe, [byte[]](0x4D, 0x5A) + [byte[]](,0 * 200))
    $refused = CB attachments preview $exe
    Check 'unsafe attachment refused' ($refused.Code -ne 0 -and $refused.Text -match 'programs are never accepted') ''
    $server = Start-Process $pythonExe -ArgumentList '-m', 'http.server', '8765', '--bind', '127.0.0.1', '--directory', $project -PassThru -WindowStyle Hidden
    Start-Sleep 3
    try { $ui = CB inspect-ui http://127.0.0.1:8765/index.html --json } finally { Stop-Process -Id $server.Id -Force }
    $inspection = $ui.Text | ConvertFrom-Json
    Check 'visual inspection through Microsoft Edge' ($inspection.screenshots.Count -eq 3 -and $inspection.viewports -contains 'mobile') ($inspection.statement)
  } finally { Pop-Location }
  Check 'project untouched' ((git -C $project status --porcelain) -eq $null)

  # 6. Rollback to v0.9.0 keeps working with the migrated state, then update again
  $rollback = CB rollback
  $after = CB --version
  Check 'rollback to v0.9.0' ($rollback.Code -eq 0 -and $after.Text -match [regex]::Escape($oldVersion)) $after.Text.Trim()
  Push-Location $project
  try {
    $status = CB status
    $tasks = CB tasks
  } finally { Pop-Location }
  Check 'v0.9.0 works with the state after rollback' ($status.Code -eq 0 -and $status.Text -match 'qwen2.5-coder:7b' -and $tasks.Text -match 'saved session from 0.9.0') ''
  $again = CB update --source $New
  $after = CB --version
  Check 'update again after rollback' ($again.Code -eq 0 -and $after.Text -match [regex]::Escape($newVersion)) ''
} catch {
  Check 'upgrade verification ran to completion' $false $_.Exception.Message
} finally {
  [Environment]::SetEnvironmentVariable('Path', $savedUserPath, 'User')
  [Environment]::SetEnvironmentVariable('CODINGBRAIN_HOME', $savedHome, 'User')
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

$failedChecks = @($results.Keys | Where-Object { -not $results[$_].ok })
$report = [ordered]@{ powershell = $PSVersionTable.PSVersion.ToString(); os = [Environment]::OSVersion.VersionString; from = $Published; to = $newVersion; checks = $results; failed = $failedChecks }
$report | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $PWD 'upgrade-report.json')
if ($failedChecks.Count) { Write-Host "FAILED: $($failedChecks -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "Upgrade from $Published verified." -ForegroundColor Green
