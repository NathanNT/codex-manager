$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$removed = $false
foreach ($name in @('Codex Manager.lnk', 'Codex Agent Manager.lnk', 'Codex Supervision.lnk')) {
    $shortcutPath = Join-Path $startup $name
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
        Write-Output "Demarrage automatique retire : $shortcutPath"
        $removed = $true
    }
}
if (-not $removed) { Write-Output 'Aucun demarrage automatique Codex Manager installe.' }
