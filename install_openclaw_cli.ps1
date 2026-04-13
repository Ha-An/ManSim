param(
    [string]$InstallRoot = ".tooling\\openclaw-cli",
    [string]$PackageName = "openclaw"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$nodeDir = Join-Path $PSScriptRoot ".tooling\\node"
$npmCmd = Join-Path $PSScriptRoot ".tooling\\node\\npm.cmd"
if (!(Test-Path $npmCmd)) {
    throw "Portable npm not found: $npmCmd"
}
if (!(Test-Path $nodeDir)) {
    throw "Portable Node directory not found: $nodeDir"
}

$env:Path = "$nodeDir;$env:Path"

$resolvedInstallRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $InstallRoot))
New-Item -ItemType Directory -Force -Path $resolvedInstallRoot | Out-Null

& $npmCmd install $PackageName --prefix $resolvedInstallRoot
