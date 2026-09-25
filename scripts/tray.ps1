param([switch]$Probe)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$url = 'http://127.0.0.1:8765/'
$healthUrl = "${url}api/health"
$logFile = Join-Path $root 'data/logs/tray.log'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$createdNew = $false
$singleton = [System.Threading.Mutex]::new($false, 'CodexSupervisionTray', [ref]$createdNew)
if (-not $createdNew) {
    $singleton.Dispose()
    exit 0
}

function Write-TrayLog([string]$message) {
    $directory = Split-Path -Parent $logFile
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    Add-Content -LiteralPath $logFile -Value ("{0} {1}" -f (Get-Date -Format s), $message) -Encoding UTF8
}

function Test-DashboardHealth {
    try {
        $reply = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 2 -ErrorAction Stop
        return ($reply.ok -eq $true -and $reply.demo -eq $false)
    } catch {
        return $false
    }
}

function Start-DashboardServer {
    $script:lastStartAttempt = Get-Date
    try {
        & (Join-Path $PSScriptRoot 'start.ps1') -LiveOnly | Out-Null
    } catch {
        Write-TrayLog ("Start failed: " + $_.Exception.Message)
    }
}

function Stop-DashboardServer {
    try {
        & (Join-Path $PSScriptRoot 'stop.ps1') -LiveOnly | Out-Null
    } catch {
        Write-TrayLog ("Stop failed: " + $_.Exception.Message)
    }
}

function Set-TrayStatus([string]$state) {
    $script:notify.Icon = $script:icons[$state]
    switch ($state) {
        'online' {
            $script:notify.Text = 'Codex Manager - serveur actif'
            $script:statusItem.Text = 'Serveur actif'
        }
        'starting' {
            $script:notify.Text = 'Codex Manager - demarrage'
            $script:statusItem.Text = 'Demarrage du serveur'
        }
        default {
            $script:notify.Text = 'Codex Manager - serveur indisponible'
            $script:statusItem.Text = 'Serveur indisponible'
        }
    }
}

function Refresh-TrayStatus {
    if (Test-DashboardHealth) {
        Set-TrayStatus 'online'
        return
    }
    if ($script:autoRestart -and ((Get-Date) - $script:lastStartAttempt).TotalSeconds -ge 30) {
        Set-TrayStatus 'starting'
        Start-DashboardServer
    } else {
        Set-TrayStatus 'offline'
    }
}

function Open-Dashboard {
    if (-not (Test-DashboardHealth)) {
        Start-DashboardServer
        for ($attempt = 0; $attempt -lt 12 -and -not (Test-DashboardHealth); $attempt++) {
            Start-Sleep -Milliseconds 250
        }
    }
    if (Test-DashboardHealth) {
        Start-Process $url
        Set-TrayStatus 'online'
    } else {
        Set-TrayStatus 'offline'
        $script:notify.ShowBalloonTip(3000, 'Codex Manager',
            'Le serveur ne repond pas. Consultez data/logs/tray.log.',
            [System.Windows.Forms.ToolTipIcon]::Warning)
    }
}

try {
    $script:icons = @{
        online = [System.Drawing.Icon]::new((Join-Path $root 'assets/tray-online.ico'))
        starting = [System.Drawing.Icon]::new((Join-Path $root 'assets/tray-starting.ico'))
        offline = [System.Drawing.Icon]::new((Join-Path $root 'assets/tray-offline.ico'))
    }
    if ($Probe) {
        Write-Output ('Health: ' + (Test-DashboardHealth))
        Write-Output ('Icons: ' + $script:icons.Count)
        exit 0
    }

    $script:notify = New-Object System.Windows.Forms.NotifyIcon
    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    $openItem = $menu.Items.Add('Ouvrir le dashboard')
    $script:statusItem = $menu.Items.Add('Etat inconnu')
    $script:statusItem.Enabled = $false
    [void]$menu.Items.Add([System.Windows.Forms.ToolStripSeparator]::new())
    $restartItem = $menu.Items.Add('Redemarrer le serveur')
    $quitItem = $menu.Items.Add('Quitter et arreter le serveur')

    $script:notify.ContextMenuStrip = $menu
    $script:notify.Icon = $script:icons.offline
    $script:notify.Text = 'Codex Manager - serveur indisponible'
    $script:notify.Visible = $true
    $script:context = New-Object System.Windows.Forms.ApplicationContext
    $script:autoRestart = $true
    $script:lastStartAttempt = [datetime]::MinValue

    $openItem.add_Click({ Open-Dashboard })
    $script:notify.add_MouseClick({
        param($sender, $eventArgs)
        if ($eventArgs.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Open-Dashboard }
    })
    $restartItem.add_Click({
        Stop-DashboardServer
        Start-DashboardServer
        Refresh-TrayStatus
    })
    $quitItem.add_Click({
        $script:autoRestart = $false
        Stop-DashboardServer
        $script:context.ExitThread()
    })

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 10000
    $timer.add_Tick({ Refresh-TrayStatus })
    Refresh-TrayStatus
    $timer.Start()
    [System.Windows.Forms.Application]::Run($script:context)
} catch {
    Write-TrayLog ("Tray failed: " + $_.Exception.ToString())
    if ($Probe) { throw }
} finally {
    if ($timer) { $timer.Stop(); $timer.Dispose() }
    if ($script:notify) { $script:notify.Visible = $false; $script:notify.Dispose() }
    if ($menu) { $menu.Dispose() }
    if ($script:icons) { foreach ($icon in $script:icons.Values) { $icon.Dispose() } }
    $singleton.Dispose()
}
