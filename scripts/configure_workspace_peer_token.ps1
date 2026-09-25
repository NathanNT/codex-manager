param([string]$Name = 'CODEX_WORKSPACE_PEER_TOKEN')

$secret = Read-Host 'Jeton commun des deux workspaces (32 caractères minimum)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ($value.Length -lt 32) { throw 'Jeton trop court : 32 caractères minimum.' }
    [Environment]::SetEnvironmentVariable($Name, $value, 'User')
    [Environment]::SetEnvironmentVariable($Name, $value, 'Process')
    Write-Output "Jeton enregistré dans la variable utilisateur $Name. Redémarrez VS Code et le moniteur."
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $value = $null
}
