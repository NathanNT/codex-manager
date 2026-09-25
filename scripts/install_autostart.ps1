$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$trayScript = Join-Path $PSScriptRoot 'tray.ps1'
$python = (Get-Command python -ErrorAction Stop).Source
$powerShell = Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Codex Manager.lnk'

if (-not (Test-Path -LiteralPath $powerShell)) { throw 'Windows PowerShell introuvable.' }
& $python (Join-Path $PSScriptRoot 'build_tray_icons.py')
if ($LASTEXITCODE -ne 0) { throw 'Creation des icones impossible.' }

New-Item -ItemType Directory -Force -Path $startup | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$arguments = '-NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "' + $trayScript + '"'
$shortcut.TargetPath = $powerShell
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = (Join-Path $root 'assets/tray-online.ico') + ',0'
$shortcut.Description = 'Codex Manager - demarrage automatique et icone de notification'
$shortcut.WindowStyle = 7
$shortcut.Save()
foreach ($legacyName in @('Codex Agent Manager.lnk', 'Codex Supervision.lnk')) {
    $legacyShortcutPath = Join-Path $startup $legacyName
    if (Test-Path -LiteralPath $legacyShortcutPath) {
        $legacyShortcut = $shell.CreateShortcut($legacyShortcutPath)
        if ($legacyShortcut.Arguments.Contains($trayScript)) {
            Remove-Item -LiteralPath $legacyShortcutPath
        }
    }
}

$existing = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($trayScript) } |
    Select-Object -First 1
if ($existing) {
    Write-Output "Demarrage automatique installe : $shortcutPath"
    Write-Output "Icone de notification deja active (PID $($existing.ProcessId))."
    return
}

$trayProcess = Start-Process -FilePath $powerShell -ArgumentList $arguments `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
Write-Output "Demarrage automatique installe : $shortcutPath"
Write-Output "Icone de notification lancee (PID $($trayProcess.Id))."
