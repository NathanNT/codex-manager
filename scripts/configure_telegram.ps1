param([switch]$Auto)

$ErrorActionPreference = 'Stop'
$token = [Environment]::GetEnvironmentVariable('TELEGRAM_API_KEY', 'User')
if (-not $token) { $token = [Environment]::GetEnvironmentVariable('TELEGRAM_API_KEY', 'Process') }
if (-not $token) { $token = [Environment]::GetEnvironmentVariable('TELEGRAM_BOT_TOKEN', 'User') }
if (-not $token) { $token = [Environment]::GetEnvironmentVariable('TELEGRAM_BOT_TOKEN', 'Process') }
if (-not $token) {
    Write-Output 'Entrez le jeton du bot Telegram. Il ne sera pas affiché.'
    $secureToken = Read-Host 'TELEGRAM_API_KEY' -AsSecureString
    if (-not $secureToken -or $secureToken.Length -eq 0) { throw 'Jeton vide.' }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}
if (-not $token) { throw 'Jeton vide.' }

# The old -Auto mode selected the only /start it found. Pairing now always
# requires a fresh random code sent from the owner's private Telegram chat.
if ($Auto) { Write-Output 'Appairage par code requis ; le mode automatique est désactivé.' }
[Environment]::SetEnvironmentVariable('TELEGRAM_API_KEY', $token, 'Process')
$pairLock = Join-Path (Split-Path -Parent $PSScriptRoot) 'data/telegram-pairing.lock'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pairLock) | Out-Null
Set-Content -LiteralPath $pairLock -Value (Get-Date -Format o) -Encoding UTF8
try {
    & (Join-Path $PSScriptRoot 'stop.ps1') -LiveOnly
    & python (Join-Path $PSScriptRoot 'telegram_pair.py')
    if ($LASTEXITCODE -ne 0) { throw 'Appairage Telegram non terminé. La configuration précédente reste inchangée.' }
} finally {
    Remove-Item -LiteralPath $pairLock -Force -ErrorAction SilentlyContinue
    & (Join-Path $PSScriptRoot 'start.ps1') -LiveOnly
}
$token = $null

for ($attempt = 0; $attempt -lt 12; $attempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2 -ErrorAction Stop
        if ($health.ok -and $health.telegram_goal_alerts) {
            Write-Output 'Telegram est appairé et actif pour les changements de goal.'
            exit 0
        }
    } catch { }
}
throw 'Appairage enregistré, mais le serveur ne confirme pas encore Telegram. Redémarrez-le depuis son icône.'
