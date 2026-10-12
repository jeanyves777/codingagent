param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$ReportPath
)

$ErrorActionPreference = 'Stop'
try {
    $installRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\'
    function Get-InstalledProcesses {
        @(Get-Process -ErrorAction Stop | Where-Object {
            $processPath = $_.Path
            $processPath -and $processPath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase)
        })
    }

    # Request normal window closure; never terminate a process or discard unsaved work.
    foreach ($process in (Get-InstalledProcesses)) {
        if ($process.MainWindowHandle -ne 0) {
            [void]$process.CloseMainWindow()
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $remaining = @(Get-InstalledProcesses)
        if ($remaining.Count -eq 0) { exit 0 }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    $details = 'Still running from the installation folder:' + [Environment]::NewLine +
        (($remaining | ForEach-Object { '{0} (PID {1})' -f $_.ProcessName, $_.Id }) -join [Environment]::NewLine)
    [IO.File]::WriteAllText($ReportPath, $details)
    exit 1
} catch {
    [IO.File]::WriteAllText($ReportPath, 'Could not check running applications: ' + $_.Exception.Message)
    exit 2
}
