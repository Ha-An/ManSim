param(
    [string]$ContainerName = "mansim-gemma4-cu130"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$dockerExe = $null

$cmd = Get-Command docker -ErrorAction SilentlyContinue
if ($null -ne $cmd) {
    $dockerExe = $cmd.Source
}
elseif (Test-Path "C:\Program Files\Docker\Docker\resources\bin\docker.exe") {
    $dockerExe = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
}
if ($null -eq $dockerExe) {
    throw "docker is not installed. Nothing to stop."
}
$dockerBinDir = Split-Path -Parent $dockerExe
if (-not [string]::IsNullOrWhiteSpace($dockerBinDir)) {
    $pathValue = [string]$env:Path
    if ($pathValue -notlike "*$dockerBinDir*") {
        $env:Path = "$dockerBinDir;$pathValue"
    }
}

$existing = & $dockerExe ps -a --filter "name=^/$ContainerName$" --format "{{.ID}}"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($existing | Out-String))) {
    Write-Output "GEMMA4_RUNTIME_NOT_FOUND"
    exit 0
}

& $dockerExe rm -f $ContainerName | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to stop/remove container '$ContainerName'."
}

Write-Output "GEMMA4_RUNTIME_STOPPED"
