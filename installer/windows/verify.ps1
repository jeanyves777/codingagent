<#
.SYNOPSIS
  End-to-end verification of the Windows installer, launcher, updates, rollback and uninstall.

.DESCRIPTION
  Uses a throwaway install directory and a throwaway Git project; never touches an existing
  installation or your projects. Run it in CI (see .github/workflows/windows-local-install.yml)
  or on your own machine:

    python scripts\build_release.py --out dist\old --version 0.9.0
    python scripts\build_release.py --out dist\new --version 0.9.0.post1 --constraints dist\old\constraints.txt
    powershell -ExecutionPolicy Bypass -File installer\windows\verify.ps1 -Old dist\old -New dist\new
#>
param(
  [Parameter(Mandatory = $true)][string]$Old,
  [Parameter(Mandatory = $true)][string]$New
)
$ErrorActionPreference = 'Stop'
$Old = (Resolve-Path $Old).Path
$New = (Resolve-Path $New).Path
$work = Join-Path ([IO.Path]::GetTempPath()) ("cb-verify-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$home_ = Join-Path $work 'CodingBrain'
$results = [ordered]@{}
$savedUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$savedHome = [Environment]::GetEnvironmentVariable('CODINGBRAIN_HOME', 'User')

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
  $results[$Name] = [ordered]@{ ok = $Ok; detail = $Detail }
  $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
  Write-Host "[$mark] $Name $Detail"
}

function CB {
  # The launcher on PATH, run as a user would: a .cmd found through PATH.
  $ErrorActionPreference = 'Continue'  # native stderr is output here, not a script error
  $output = & cmd.exe /d /c codingbrain @args 2>&1 | Out-String
  $code = $LASTEXITCODE
  Write-Host ("--- codingbrain {0} (exit {1})`n{2}" -f ($args -join ' '), $code, $output.TrimEnd())
  return [pscustomobject]@{ Code = $code; Text = $output }
}
$shellExe = (Get-Process -Id $PID).Path  # run the installer under the PowerShell being verified
$pythonExe = (Get-Command python).Source

function Version-Of($folder) { (Get-Content -Raw (Join-Path $folder 'release.json') | ConvertFrom-Json).version }
$oldVersion = Version-Of $Old
$newVersion = Version-Of $New

try {
  New-Item -ItemType Directory -Path $work | Out-Null
  # 1. Fresh installation
  & $shellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1') -From $Old -InstallDir $home_ -Python $pythonExe -Yes
  Check 'fresh install' ($LASTEXITCODE -eq 0 -and (Test-Path "$home_\bin\codingbrain.cmd")) "exit $LASTEXITCODE"
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  Check 'user PATH entry' (($userPath -split ';') -contains "$home_\bin")
  $env:Path = "$home_\bin;" + $env:Path
  $env:CODINGBRAIN_HOME = $home_
  $v = CB --version
  Check 'version command' ($v.Code -eq 0 -and $v.Text -match [regex]::Escape($oldVersion)) $v.Text.Trim()

  # 2. Launch from an unrelated Git repository; recognition is read-only
  $project = Join-Path $work 'Projects\MyApplication'
  New-Item -ItemType Directory -Path $project | Out-Null
  Set-Content -Path "$project\calc.py" -Value "def add(a, b):`n    return a + b"
  Set-Content -Path "$project\package.json" -Value '{"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}}'
  git -C $project init -q; git -C $project add -A; git -C $project -c user.email=v@v -c user.name=v commit -qm init
  Push-Location $project
  try {
    $init = CB init
    Check 'recognize project' ($init.Code -eq 0 -and $init.Text -match 'MyApplication' -and $init.Text -match 'Python' -and $init.Text -match 'Jest') ''
    $setup = CB setup --non-interactive --model qwen2.5-coder:7b --execution propose
    Check 'non-interactive setup' ($setup.Code -eq 0)
    $doctor = CB doctor --offline --json
    Check 'health check' ($doctor.Code -eq 0 -and ($doctor.Text | ConvertFrom-Json).ok) ''
    $status = CB status
    Check 'configuration persists across processes' ($status.Text -match 'qwen2.5-coder:7b')
    $online = CB doctor
    Check 'premium provider detection reported' ($online.Text -match 'premium: claude' -and $online.Text -match 'premium: codex') ''
  } finally { Pop-Location }
  Check 'project untouched after recognition' ((git -C $project status --porcelain) -eq $null)
  $memory = Get-ChildItem "$home_\data\projects" -Directory | Where-Object { $_.Name -like 'MyApplication-*' }
  Check 'per-project memory outside the project' ($memory -ne $null)
  Set-Content -Path "$home_\data\projects\sentinel.txt" -Value 'keep me'

  # 3. Updates: check, refuse tampered, refuse during a session, apply, preserve, roll back
  $check = CB update --check --source $New
  Check 'update check' ($check.Text -match [regex]::Escape($newVersion)) $check.Text.Trim()
  $bad = Join-Path $work 'bad'
  Copy-Item -Recurse $New $bad
  Add-Content -Path (Join-Path $bad "coding_brain-$newVersion-py3-none-any.whl") -Value 'x'
  $tampered = CB update --source $bad
  $after = CB --version
  Check 'tampered update rejected, old version kept' ($tampered.Code -ne 0 -and $tampered.Text -match 'Checksum mismatch' -and $after.Text -match [regex]::Escape($oldVersion)) ''
  $sleeper = Start-Process powershell -ArgumentList '-NoProfile', '-Command', 'Start-Sleep 120' -PassThru -WindowStyle Hidden
  Set-Content -Path "$home_\locks\session-$($sleeper.Id).json" -Value ("{`"pid`": $($sleeper.Id), `"project`": `"MyApplication`"}")
  $busy = CB update --source $New
  Stop-Process -Id $sleeper.Id -Force
  Check 'no update during an active session' ($busy.Code -ne 0 -and $busy.Text -match 'session is running') ''
  $update = CB update --source $New
  $after = CB --version
  Check 'update applied' ($update.Code -eq 0 -and $after.Text -match [regex]::Escape($newVersion)) $after.Text.Trim()
  Push-Location $project
  try { $status = CB status } finally { Pop-Location }
  Check 'configuration and memory preserved by update' ($status.Text -match 'qwen2.5-coder:7b' -and (Get-Content "$home_\data\projects\sentinel.txt") -eq 'keep me' -and (Test-Path $memory.FullName)) ''
  Check 'state backed up before update' ((Get-ChildItem "$home_\backups" -Directory).Count -ge 1)
  $rollback = CB rollback
  $after = CB --version
  Check 'rollback' ($rollback.Code -eq 0 -and $after.Text -match [regex]::Escape($oldVersion)) $after.Text.Trim()

  # 4. Uninstall keeps data and never touches projects; -RemoveData removes Coding Brain's data only
  & $shellExe -NoProfile -ExecutionPolicy Bypass -File "$home_\uninstall.ps1" -InstallDir $home_ -Yes
  $pathAfter = [Environment]::GetEnvironmentVariable('Path', 'User')
  Check 'uninstall removes application and PATH entry' (-not (Test-Path "$home_\app") -and -not (Test-Path "$home_\bin") -and -not (($pathAfter -split ';') -contains "$home_\bin")) ''
  Check 'uninstall keeps configuration and memory' ((Test-Path "$home_\config\config.json") -and (Test-Path $memory.FullName)) ''
  & $shellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1') -From $New -InstallDir $home_ -Python $pythonExe -Yes
  $env:Path = "$home_\bin;" + $env:Path
  Push-Location $project
  try { $status = CB status } finally { Pop-Location }
  Check 'reinstall reuses kept configuration' ($status.Text -match 'qwen2.5-coder:7b')
  & $shellExe -NoProfile -ExecutionPolicy Bypass -File "$home_\uninstall.ps1" -InstallDir $home_ -RemoveData -Yes
  Check 'full removal' (-not (Test-Path $home_))
  Check 'project files and branch unchanged' ((Test-Path "$project\calc.py") -and ((git -C $project status --porcelain) -eq $null) -and ((git -C $project log --oneline | Measure-Object).Count -eq 1)) ''
} catch {
  Check 'verification ran to completion' $false $_.Exception.Message
} finally {
  [Environment]::SetEnvironmentVariable('Path', $savedUserPath, 'User')
  [Environment]::SetEnvironmentVariable('CODINGBRAIN_HOME', $savedHome, 'User')
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

$failed = @($results.Keys | Where-Object { -not $results[$_].ok })
$report = [ordered]@{ powershell = $PSVersionTable.PSVersion.ToString(); os = [Environment]::OSVersion.VersionString; old = $oldVersion; new = $newVersion; checks = $results; failed = $failed }
$report | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $PWD 'verify-report.json')
if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host 'All Windows verification checks passed.' -ForegroundColor Green
