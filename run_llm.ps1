# Helper script for local OpenClaw + vLLM runs.
param(
    [string]$Decision = "llm_planner",
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
    & wsl.exe -d $Distro -u root -- bash -lc "systemctl restart vllm || true"

    $cmdArgs = @("main.py", "decision=$Decision") + $ExtraArgs
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
