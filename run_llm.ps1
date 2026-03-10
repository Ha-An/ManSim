# Helper script for local LLM runs through Ollama on WSL.
# It keeps the WSL distro alive long enough to restart Ollama and then runs
# the ManSim entry point with the requested decision preset.
param(
    [string]$Decision = "llm",
    [string]$Distro = "Ubuntu-24.04",
    [string]$PythonPath = ".\.venv\Scripts\python.exe",
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path $PythonPath)) {
    throw "Python not found: $PythonPath"
}

$keepAliveProc = $null

try {
    $keepAliveArgs = '-d {0} -- bash -lc "sleep infinity"' -f $Distro
    $keepAliveProc = Start-Process -WindowStyle Hidden -FilePath "wsl.exe" -ArgumentList $keepAliveArgs -PassThru

    Start-Sleep -Seconds 2
    & wsl.exe -d $Distro -u root -- bash -lc "systemctl restart ollama"

    $cmdArgs = @("-m", "manufacturing_sim.simulation.main", "decision=$Decision") + $ExtraArgs
    & $PythonPath @cmdArgs
}
finally {
    if ($null -ne $keepAliveProc) {
        try {
            if (-not $keepAliveProc.HasExited) {
                Stop-Process -Id $keepAliveProc.Id -Force
            }
        }
        catch {
        }
    }
}
