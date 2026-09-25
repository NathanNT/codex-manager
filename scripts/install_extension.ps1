param([switch]$PackageOnly)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$extensionDir = Join-Path $root 'extension'
$vsix = Join-Path $extensionDir 'codex-supervision-presence-0.1.0.vsix'

Push-Location $extensionDir
try {
    npm exec --yes --package '@vscode/vsce' -- vsce package --no-dependencies
    if ($LASTEXITCODE -ne 0) { throw 'La création du VSIX a échoué.' }
} finally { Pop-Location }

if ($PackageOnly) {
    Write-Host "VSIX prêt : $vsix"
    return
}

$base = Join-Path $env:LOCALAPPDATA 'VSCode-Codex'
foreach ($number in 1,2) {
    $profile = Join-Path $base "Compte-$number"
    $userData = Join-Path $profile 'vscode'
    $extensions = Join-Path $profile 'extensions'
    if (-not (Test-Path -LiteralPath $userData)) { throw "Profil VS Code introuvable : $userData" }
    code --install-extension $vsix --force --user-data-dir $userData --extensions-dir $extensions
    if ($LASTEXITCODE -ne 0) { throw "Installation impossible pour Compte-$number" }
    Write-Host "Extension installée pour Compte-$number"
}
Write-Host 'Rechargez les fenêtres VS Code déjà ouvertes pour activer la présence.'
