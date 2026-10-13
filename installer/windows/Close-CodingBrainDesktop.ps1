param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$ReportPath
)

$ErrorActionPreference = 'Stop'
try {
    $installRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\'
    $expectedDesktop = Join-Path $installRoot 'CodingBrainDesktop.exe'
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $key = -join ($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($installRoot.TrimEnd('\').ToLowerInvariant())) |
            ForEach-Object { $_.ToString('x2') })
    } finally { $hasher.Dispose() }
    $ownershipDirectory = Join-Path $env:LOCALAPPDATA "CodingBrainDesktop\process-ownership\$key"
    $script:trustedProcesses = @{}
    function Test-OwnedProcess($process) {
        $original = $script:processInfo[[int]$process.Id]
        if (-not $original -or -not $original.CreationDate -or -not $original.ExecutablePath) { return $false }
        try {
            # Pin the live process identity as well as the CIM snapshot; a reused PID
            # must never inherit an old process's ownership.
            $liveBirth = $process.StartTime.ToUniversalTime().ToFileTimeUtc()
            $birth = $original.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()
            $cimBirth = [long]$birth
            if (($liveBirth - ($liveBirth % 10)) -ne ($cimBirth - ($cimBirth % 10)) -or
                -not [string]::Equals($process.Path, $original.ExecutablePath, [StringComparison]::OrdinalIgnoreCase)) {
                return $false
            }
            $owner = Invoke-CimMethod -InputObject $original -MethodName GetOwnerSid -ErrorAction Stop
            if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $currentSid) { return $false }
            if ($script:trustedProcesses[$process.Id] -eq $birth) { return $true }
            $node = $original
            $visited = @{}
            while ($node -and -not $visited.ContainsKey([int]$node.ProcessId)) {
                $visited[[int]$node.ProcessId] = $true
                $nodeOwner = Invoke-CimMethod -InputObject $node -MethodName GetOwnerSid -ErrorAction Stop
                if ($nodeOwner.ReturnValue -ne 0 -or $nodeOwner.Sid -ne $currentSid) { return $false }
                $nodeBirth = $node.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()
                $verified = [string]::Equals($node.ExecutablePath, $expectedDesktop, [StringComparison]::OrdinalIgnoreCase)
                $recordPath = Join-Path $ownershipDirectory "$($node.ProcessId).json"
                if (-not $verified -and (Test-Path -LiteralPath $recordPath)) {
                    $recordOwner = (Get-Acl -LiteralPath $recordPath).GetOwner([Security.Principal.SecurityIdentifier]).Value
                    $record = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
                    $desktopHash = (Get-FileHash -LiteralPath $expectedDesktop -Algorithm SHA256).Hash.ToLowerInvariant()
                    $verified = $recordOwner -eq $currentSid -and $record.schema -eq 1 -and
                        $record.sid -eq $currentSid -and $record.pid -eq $node.ProcessId -and
                        $record.creation_time -eq $nodeBirth -and
                        $record.desktop_pid -gt 0 -and $record.desktop_sha256 -eq $desktopHash -and
                        [long]$record.desktop_creation_time -le [long]$nodeBirth -and
                        [string]::Equals($record.install_dir, $installRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -and
                        [string]::Equals($record.executable, $node.ExecutablePath, [StringComparison]::OrdinalIgnoreCase) -and
                        [string]::Equals($record.desktop_executable, $expectedDesktop, [StringComparison]::OrdinalIgnoreCase)
                }
                if ($verified) { $script:trustedProcesses[$process.Id] = $birth; return $true }
                # Only the desktop's exact Python service commands can inherit
                # ancestry proof. Detached tools or other child applications cannot.
                $service = $node.CommandLine -match '^\s*(?:"[^"]*\\pythonw?\.exe"|[^\s"]*\\pythonw?\.exe)\s+(?:-u\s+)?-m\s+(?:desktop_ui\.server\s+--no-open\s+--port\s+0\s+--handshake-stdout|brain\.local\s+api\s+--stdio)\s*$'
                if (-not $service) { return $false }
                $parent = $script:processInfo[[int]$node.ParentProcessId]
                if (-not $parent -or $parent.CreationDate -gt $node.CreationDate) { return $false }
                $node = $parent
            }
        } catch { return $false }
        return $false
    }
    function Get-InstalledProcesses {
        $script:processInfo = @{}
        Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object { $script:processInfo[[int]$_.ProcessId] = $_ }
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

    # Request normal window closure before stopping installation-specific leftovers.
    foreach ($process in (Get-InstalledProcesses)) {
        $owned = Test-OwnedProcess $process
        if ($owned -and $process.MainWindowHandle -ne 0) {
            [void]$process.CloseMainWindow()
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $remaining = @(Get-InstalledProcesses)
        if ($remaining.Count -eq 0) { exit 0 }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    # Setup is an update: release the verified executable/DLL holders automatically.
    # Never select processes by a generic name such as python.exe.
    foreach ($process in $remaining) {
        if (-not (Test-OwnedProcess $process)) { continue }
        try {
            $process.Kill()
        } catch {
            # A process may exit while we are closing another holder. The final scan
            # decides whether Setup can continue, including permission failures.
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $remaining = @(Get-InstalledProcesses)
        if ($remaining.Count -eq 0) { exit 0 }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    $details = 'Processes still using installed files (unverified ownership is left untouched):' + [Environment]::NewLine +
        (($remaining | ForEach-Object {
            $reason = if (Test-OwnedProcess $_) { 'shutdown failed' } else { 'ownership not verified; not stopped' }
            '{0} (PID {1}) - {2}' -f $_.ProcessName, $_.Id, $reason
        }) -join [Environment]::NewLine)
    [IO.File]::WriteAllText($ReportPath, $details)
    exit 1
} catch {
    [IO.File]::WriteAllText($ReportPath, 'Could not check running applications: ' + $_.Exception.Message)
    exit 2
}
