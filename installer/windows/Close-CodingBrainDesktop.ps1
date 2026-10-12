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
            if ($processPath -and $processPath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
            # The installed engine uses an external Python interpreter. Its executable
            # is outside {app}, but inherited DLL search paths can load bundled DLLs.
            try {
                foreach ($module in $_.Modules) {
                    if ($module.FileName -and $module.FileName.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
                        return $true
                    }
                }
            } catch {
                # Windows denies module enumeration for protected/unrelated processes.
            }
            return $false
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

    $details = 'Processes using files in the installation folder:' + [Environment]::NewLine +
        (($remaining | ForEach-Object { '{0} (PID {1})' -f $_.ProcessName, $_.Id }) -join [Environment]::NewLine)
    [IO.File]::WriteAllText($ReportPath, $details)
    exit 1
} catch {
    [IO.File]::WriteAllText($ReportPath, 'Could not check running applications: ' + $_.Exception.Message)
    exit 2
}
