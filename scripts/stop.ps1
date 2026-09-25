param([switch]$LiveOnly, [switch]$DemoOnly)

$ErrorActionPreference = 'Stop'
$ports = @()
if (-not $DemoOnly) { $ports += 8765 }
if (-not $LiveOnly) { $ports += 8768 }
foreach ($port in $ports) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($process.Name -notmatch '^python(?:\.exe)?$' -or $process.CommandLine -notmatch 'server\.py') {
            Write-Warning "Port $port utilisé par un autre programme ; arrêt ignoré."
            continue
        }
        Stop-Process -Id $process.ProcessId -Force
        Write-Host "Serveur du port $port arrêté."
    }
}
