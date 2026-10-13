param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][int]$DesktopPid,
    [Parameter(Mandatory = $true)][int]$BridgePid,
    [switch]$StopOwned
)
$ErrorActionPreference = 'Stop'
$installRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$expectedDesktop = Join-Path $installRoot 'CodingBrainDesktop.exe'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$processes = @{}
Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object { $processes[[int]$_.ProcessId] = $_ }
$desktop = $processes[$DesktopPid]
$caller = $processes[$PID]
if (-not $desktop -or -not $caller -or $caller.ParentProcessId -ne $DesktopPid -or
    -not [string]::Equals($desktop.ExecutablePath, $expectedDesktop, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Ownership records must be created directly by the installed desktop.'
}
$desktopOwner = Invoke-CimMethod -InputObject $desktop -MethodName GetOwnerSid -ErrorAction Stop
if ($desktopOwner.ReturnValue -ne 0 -or $desktopOwner.Sid -ne $sid) { throw 'Desktop Windows account mismatch.' }
$bridge = $processes[$BridgePid]
if (-not $bridge -or $bridge.ParentProcessId -ne $DesktopPid -or $bridge.CreationDate -lt $desktop.CreationDate) {
    throw 'The bridge is not a current child of this desktop.'
}
$hash = [Security.Cryptography.SHA256]::Create()
try {
    $key = -join ($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($installRoot.ToLowerInvariant())) |
        ForEach-Object { $_.ToString('x2') })
} finally { $hash.Dispose() }
$directory = Join-Path $env:LOCALAPPDATA "CodingBrainDesktop\process-ownership\$key"
[void][IO.Directory]::CreateDirectory($directory)
$desktopHash = (Get-FileHash -LiteralPath $expectedDesktop -Algorithm SHA256).Hash.ToLowerInvariant()
$owned = @{}
$owned[$BridgePid] = $bridge
do {
    $added = $false
    foreach ($process in $processes.Values) {
        $parent = $owned[[int]$process.ParentProcessId]
        $service = $process.Name -match '^pythonw?\.exe$' -and
            $process.CommandLine -match '^\s*(?:"[^"]*\\pythonw?\.exe"|[^\s"]*\\pythonw?\.exe)\s+(?:-u\s+)?-m\s+(?:desktop_ui\.server\s+--no-open\s+--port\s+0\s+--handshake-stdout|brain\.local\s+api\s+--stdio)\s*$'
        if (-not $owned.ContainsKey([int]$process.ProcessId) -and $parent -and $service -and
            $process.CreationDate -ge $parent.CreationDate) {
            $owned[[int]$process.ProcessId] = $process
            $added = $true
        }
    }
} while ($added)
foreach ($process in $owned.Values) {
    $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction Stop
    if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $sid -or -not $process.ExecutablePath) { continue }
    $record = @{
        schema = 1; install_dir = $installRoot; sid = $sid
        pid = [int]$process.ProcessId
        creation_time = $process.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()
        executable = $process.ExecutablePath
        desktop_pid = $DesktopPid; desktop_executable = $expectedDesktop
        desktop_sha256 = $desktopHash
        desktop_creation_time = $desktop.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()
    }
    $destination = Join-Path $directory "$($process.ProcessId).json"
    $temporary = $destination + '.tmp'
    [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Compress))
    # Elevated accounts can otherwise create files owned by Administrators rather
    # than their own SID. The installer requires a record owned by this exact user.
    $recordAcl = Get-Acl -LiteralPath $temporary
    $recordAcl.SetOwner([Security.Principal.SecurityIdentifier]::new($sid))
    Set-Acl -LiteralPath $temporary -AclObject $recordAcl
    Move-Item -LiteralPath $temporary -Destination $destination -Force
}
if ($StopOwned) {
    foreach ($process in @($owned.Values | Sort-Object CreationDate -Descending)) {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction Stop
        if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $sid) { continue }
        $live = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        if (-not $live) { continue }
        $liveBirth = $live.StartTime.ToUniversalTime().ToFileTimeUtc()
        $recordBirth = $process.CreationDate.ToUniversalTime().ToFileTimeUtc()
        if (($liveBirth - ($liveBirth % 10)) -eq ($recordBirth - ($recordBirth % 10))) {
            $live.Kill()
        }
    }
}
