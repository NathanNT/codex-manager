param([switch]$LiveOnly, [switch]$DemoOnly)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$python = (Get-Command python -ErrorAction Stop).Source

# A tray process started before Telegram setup needs the latest user variables.
$peerConfig = Join-Path $root 'data/workspace-federation.json'
$peerTokenVariable = 'CODEX_WORKSPACE_PEER_TOKEN'
if (Test-Path -LiteralPath $peerConfig) {
    try {
        $peerTokenVariable = (Get-Content -LiteralPath $peerConfig -Raw | ConvertFrom-Json).token_env
        if (-not $peerTokenVariable) { $peerTokenVariable = 'CODEX_WORKSPACE_PEER_TOKEN' }
    } catch { $peerTokenVariable = 'CODEX_WORKSPACE_PEER_TOKEN' }
}
foreach ($name in @('TELEGRAM_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_PAIRED_CHAT_ID', $peerTokenVariable)) {
    $saved = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($saved) { [Environment]::SetEnvironmentVariable($name, $saved, 'Process') }
}

function Start-Dashboard([int]$port, [bool]$demo) {
    $pairLock = Join-Path $root 'data/telegram-pairing.lock'
    if (-not $demo -and (Test-Path -LiteralPath $pairLock)) {
        $age = ((Get-Date) - (Get-Item -LiteralPath $pairLock).LastWriteTime).TotalMinutes
        if ($age -lt 6) {
            Write-Host 'Appairage Telegram en cours ; démarrage différé.'
            return
        }
        Remove-Item -LiteralPath $pairLock -Force
    }
    $existing = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Port $port déjà occupé ; aucun nouveau serveur lancé."
        return
    }
    $arguments = @('server.py', '--port', "$port")
    if ($demo) { $arguments += '--demo' }
    $name = if ($demo) { 'demo' } else { 'live' }
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "$name.stdout.log") -RedirectStandardError (Join-Path $logs "$name.stderr.log")
    Write-Host "$name : http://127.0.0.1:$port/ (PID $($process.Id))"
}

if (-not $DemoOnly) { Start-Dashboard 8765 $false }
if (-not $LiveOnly) { Start-Dashboard 8768 $true }
