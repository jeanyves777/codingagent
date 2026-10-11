<#
.SYNOPSIS
  End-to-end acceptance test of an installed Coding Brain on this Windows PC: real local model,
  real sandbox, several agents, independent verification, combined result, your acceptance,
  restart recovery and a dependent item.

.DESCRIPTION
  Everything happens in a new folder (default: %TEMP%\codingbrain-acceptance-<time>) with its own
  Coding Brain home, so your configuration, projects and tasks are not touched. The installed
  program, the Ollama model and the Docker sandbox images are shared, not changed.
  The script plays the user only where a user must act: it accepts the combined result of its own
  throwaway project. It writes acceptance-report.json and acceptance-report.md and exits 0 only if
  every automatic check passed. The desktop check at the end is manual.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File windows_acceptance.ps1 -Model qwen2.5-coder:7b
#>
param(
    [Parameter(Mandatory = $true)][string]$Model,
    [int]$Minutes = 90,
    [string]$Out = (Join-Path $env:TEMP ("codingbrain-acceptance-" + (Get-Date -Format "yyyyMMdd-HHmmss"))),
    [switch]$SkipRestartTest
)
$ErrorActionPreference = "Stop"
$results = New-Object System.Collections.ArrayList
$started = Get-Date

function Record([string]$Name, [bool]$Ok, [string]$Detail) {
    [void]$results.Add([ordered]@{ step = $Name; passed = $Ok; detail = $Detail })
    $mark = if ($Ok) { "PASS" } else { "FAIL" }
    Write-Host ("[{0}] {1}: {2}" -f $mark, $Name, $Detail)
}

function CB {
    # The installed launcher; output is returned as text, the exit code in $LASTEXITCODE.
    # Windows PowerShell turns a native program's stderr into errors: never stop on those.
    $ErrorActionPreference = "Continue"
    $text = & codingbrain @args 2>&1 | Out-String
    return $text
}

function Status {
    $ErrorActionPreference = "Continue"
    $json = & codingbrain backlog status --json 2>$null | Out-String
    return ($json | ConvertFrom-Json)
}

function Item([object]$Status, [string]$Title) {
    return @($Status.items | Where-Object { $_.title -eq $Title })[0]
}

function Stop-Tree([int]$ProcessId) {
    # The launcher is a .cmd: stopping cmd.exe alone would leave Python running.
    $ErrorActionPreference = "Continue"
    & taskkill.exe /T /F /PID $ProcessId 2>&1 | Out-Null
}

function Run-Backlog([int]$TimeoutMinutes, [string]$Log) {
    $process = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "codingbrain backlog run > `"$Log`" 2>&1" `
        -WorkingDirectory $project -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit($TimeoutMinutes * 60 * 1000)) {
        Stop-Tree $process.Id
        return $false
    }
    return $true
}

function Write-Report {
    $report = [ordered]@{
        model = $Model; started = $started.ToString("o"); minutes = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
        version = $version; folder = $Out; passed = (@($results | Where-Object { -not $_.passed }).Count -eq 0)
        steps = $results; backlog = $final
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $Out "acceptance-report.json")
    $lines = @("# Coding Brain Windows acceptance", "", "Model ``$Model``, version $version, $($report.minutes) min.", "",
               "| Step | Result | Detail |", "|---|---|---|")
    foreach ($r in $results) {
        $lines += "| $($r.step) | $(if ($r.passed) { 'PASS' } else { 'FAIL' }) | $(($r.detail -replace '\|', '/') -replace "`r?`n", ' ') |"
    }
    $lines += @("", "Manual desktop check (not part of the result):",
                "1. Open Coding Brain Desktop, select the project $project.",
                "2. Open the Backlog tab: the items and their states match the table above, and the ledger is intact.")
    $lines | Set-Content -Encoding UTF8 (Join-Path $Out "acceptance-report.md")
    Write-Host ""
    Write-Host ("Report: " + (Join-Path $Out "acceptance-report.md"))
    return $report.passed
}

$final = $null
$version = ""
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$previousHome = $env:CODINGBRAIN_HOME
$env:CODINGBRAIN_HOME = Join-Path $Out "home"
$project = Join-Path $Out "project"
try {
    # 1. The installed program has the backlog.
    if (-not (Get-Command codingbrain -ErrorAction SilentlyContinue)) {
        Record "installed" $false "codingbrain is not on PATH: install Coding Brain first"
        exit [int](-not (Write-Report))
    }
    $version = (CB version).Trim()
    $null = CB backlog --help
    Record "installed" ($LASTEXITCODE -eq 0) ("version " + $version + $(if ($LASTEXITCODE -ne 0) { "; this build has no 'backlog' command: install a preview that contains it" } else { "" }))
    if ($LASTEXITCODE -ne 0) { exit [int](-not (Write-Report)) }

    # 2. A separate home: the model, autonomy.execution=auto (proposals run; acceptance stays manual).
    $text = CB setup --non-interactive --model $Model --execution auto
    Record "setup" ($LASTEXITCODE -eq 0) ("separate home " + $env:CODINGBRAIN_HOME)
    $text = CB doctor --full --json
    $doctor = $null
    try { $doctor = ($text | ConvertFrom-Json).readiness } catch { }
    $ready = $doctor -and $doctor.levels.core.ready -and $doctor.levels.sandbox.ready
    Record "doctor --full" ([bool]$ready) $(if ($doctor) { "level " + $doctor.level } else { "no readiness report: " + $text.Substring(0, [math]::Min(300, $text.Length)) })
    if (-not $ready) { exit [int](-not (Write-Report)) }

    # 3. A throwaway Git project with acceptance tests.
    New-Item -ItemType Directory -Force -Path (Join-Path $project "tests"), (Join-Path $project "acceptance") | Out-Null
    Set-Content -Encoding UTF8 (Join-Path $project "README.md") "# acceptance project"
    Set-Content -Encoding UTF8 (Join-Path $project "tests\test_smoke.py") "def test_smoke():`n    assert True"
    Set-Content -Encoding UTF8 (Join-Path $project "acceptance\test_slug.py") (@(
        "from slug import slugify", "", "", "def test_slugify():",
        "    assert slugify('Hello, World!') == 'hello-world'",
        "    assert slugify('  Many   spaces ') == 'many-spaces'") -join "`n")
    Set-Content -Encoding UTF8 (Join-Path $project "acceptance\test_bounds.py") (@(
        "import pytest", "", "from bounds import clamp", "", "", "def test_clamp():",
        "    assert clamp(5, 0, 10) == 5", "    assert clamp(-1, 0, 10) == 0", "    assert clamp(11, 0, 10) == 10",
        "    with pytest.raises(ValueError):", "        clamp(1, 5, 0)") -join "`n")
    Set-Content -Encoding UTF8 (Join-Path $project "acceptance\test_titles.py") (@(
        "from titles import title_slug", "", "", "def test_title_slug():",
        "    assert title_slug('Hello, World!') == 'hello-world'",
        "    assert title_slug('A very long title for a blog post') == 'a-very-long-title-fo'") -join "`n")
    Push-Location $project
    $ErrorActionPreference = "Continue"  # git and codingbrain report progress on stderr
    & git init -q; & git -c user.name=Acceptance -c user.email=acceptance@localhost add -A
    & git -c user.name=Acceptance -c user.email=acceptance@localhost commit -qm start
    $head = (& git rev-parse HEAD).Trim()

    $null = CB backlog add "Create slug.py with slugify(text): lowercase, every run of characters that are not letters or digits becomes one hyphen, no hyphens at either end" --title slugify --priority P0 --accept-file acceptance/test_slug.py --accept-node acceptance/test_slug.py::test_slugify
    $a = $LASTEXITCODE
    $null = CB backlog add "Create bounds.py with clamp(value, low, high): below low it returns low, above high it returns high, otherwise value; ValueError when low is greater than high" --title clamp --accept-file acceptance/test_bounds.py --accept-node acceptance/test_bounds.py::test_clamp
    $b = $LASTEXITCODE
    $slug = (Item (Status) "slugify").id
    $null = CB backlog add "Create titles.py with title_slug(title): slug.slugify(title) cut to at most 20 characters, without a trailing hyphen; use the existing slug module" --title titles --depends $slug --accept-file acceptance/test_titles.py --accept-node acceptance/test_titles.py::test_title_slug
    $c = $LASTEXITCODE
    $s = Status
    Record "backlog add" (($a + $b + $c) -eq 0 -and @($s.items).Count -eq 3 -and (Item $s "titles").state -eq "waiting") ("states: " + ((@($s.items) | ForEach-Object { $_.title + "=" + $_.state }) -join ", "))

    # 4. Restart recovery: stop the run while agents are working, then run again.
    if (-not $SkipRestartTest) {
        $process = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "codingbrain backlog run > `"$Out\run-1.log`" 2>&1" -WorkingDirectory $project -PassThru -WindowStyle Hidden
        $busy = $false
        $deadline = (Get-Date).AddMinutes(15)
        while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
            Start-Sleep -Seconds 10
            if (@((Status).agents.busy).Count -gt 0) { $busy = $true; Start-Sleep -Seconds 60; break }
        }
        Stop-Tree $process.Id
        Record "stopped while working" $busy $(if ($busy) { "agents were busy; the run was stopped" } else { "no agent started within 15 minutes (see run-1.log)" })
    }

    # 5. Work through the backlog: independent verification and the combined result.
    $finished = Run-Backlog $Minutes (Join-Path $Out "run-2.log")
    $s = Status
    $verified = @($s.items | Where-Object { $_.state -eq "verified" }).Count
    Record "backlog run" ($finished -and $s.waiting_on_people_only) ("settled: $finished; " + ((@($s.items) | ForEach-Object { $_.title + "=" + $_.state + "(" + $_.attempts + ")" }) -join ", "))
    Record "agents in parallel" ($s.agents.local_slots -ge 2) ("local slots " + $s.agents.local_slots + " (1 means the model server generates one at a time; see run-2.log)")
    Record "independent verification" ($verified -ge 1) ("$verified item(s) verified by their frozen acceptance tests")
    Record "combined result" ($null -ne $s.integration -and @($s.integration.included).Count -eq $verified) $(if ($s.integration) { "integration " + $s.integration.id + " with " + @($s.integration.included).Count + " item(s)" } else { "none" })
    Record "ledger" ([bool]$s.ledger_intact) "hash chain intact"

    # 6. Accept as the user: one new branch, the current branch unchanged.
    if ($s.integration) {
        $text = CB backlog accept
        $branch = (& git branch --list "codingbrain/backlog-*" | Out-String).Trim()
        $same = ((& git rev-parse HEAD).Trim() -eq $head) -and -not (& git status --porcelain | Out-String).Trim()
        Record "accept" ($LASTEXITCODE -eq 0 -and $branch -and $same) ("branch " + $branch + "; current branch unchanged: $same")
    }

    # 7. The dependent item starts from the accepted work.
    $s = Status
    if ((Item $s "slugify").state -eq "done") {
        $finished = Run-Backlog $Minutes (Join-Path $Out "run-3.log")
        $s = Status
        Record "dependent item" ((Item $s "titles").state -eq "verified") ("titles=" + (Item $s "titles").state + "; settled: $finished")
    } else {
        Record "dependent item" $false ("slugify was not accepted (" + (Item $s "slugify").state + "), so titles could not start")
    }
    $final = $s
    Pop-Location
} catch {
    Record "script" $false $_.Exception.Message
} finally {
    $env:CODINGBRAIN_HOME = $previousHome
}
exit [int](-not (Write-Report))
