param(
    [string]$ContainerName = "mansim-gemma4-cu130-parallel"
)

& (Join-Path $PSScriptRoot "stop_vllm_gemma4_docker.ps1") -ContainerName $ContainerName
