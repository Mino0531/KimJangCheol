# Stop only the local server processes identified by the launcher's printed IDs.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [int] $PhonePid,
    [Parameter(Mandatory = $true)] [int] $DashboardPid
)

$ErrorActionPreference = 'Stop'
$snapshot = @(Get-CimInstance Win32_Process)
$roots = @(
    @{ Id = $PhonePid; Pattern = 'uvicorn\s+app\.main:app\s+--host\s+127\.0\.0\.1\s+--port\s+8000' },
    @{ Id = $DashboardPid; Pattern = 'uvicorn\s+backend\.app:app\s+--host\s+127\.0\.0\.1\s+--port\s+8767' }
)
$stopIds = @()
foreach ($root in $roots) {
    $process = $snapshot | Where-Object { $_.ProcessId -eq $root.Id } | Select-Object -First 1
    if (-not $process) { continue }
    if ($process.Name -ne 'uv.exe' -or $process.CommandLine -notmatch $root.Pattern) {
        throw "프로세스 $($root.Id)는 이 스크립트가 확인할 수 있는 서버가 아닙니다. 아무 프로세스도 종료하지 않았습니다."
    }
    $queue = @([int]$root.Id)
    while ($queue.Count -gt 0) {
        $next = $queue[0]
        $queue = @($queue | Select-Object -Skip 1)
        $stopIds += $next
        $queue += @($snapshot | Where-Object { $_.ParentProcessId -eq $next -and $_.Name -ne 'conhost.exe' } |
            ForEach-Object { [int]$_.ProcessId })
    }
}
for ($i = $stopIds.Count - 1; $i -ge 0; $i--) {
    Stop-Process -Id $stopIds[$i] -ErrorAction SilentlyContinue
}
Write-Output '확인된 로컬 서버 프로세스만 종료했습니다.'
