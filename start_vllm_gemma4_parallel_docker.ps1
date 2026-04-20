param(
    [string]$Image = "vllm/vllm-openai:gemma4-cu130",
    [string]$ContainerName = "mansim-gemma4-cu130-parallel",
    [string]$Model = "google/gemma-4-E4B-it",
    [string]$ServedModelName = "mansim-gemma4-e4b-parallel",
    [string]$GpuVisibleDevices = "0",
    [int]$HostPort = 8001,
    [int]$ContainerPort = 8000,
    [int]$MaxModelLen = 32768,
    [double]$GpuMemoryUtilization = 0.90,
    [Nullable[int]]$Seed = $null,
    [string]$HuggingFaceCacheDir = "",
    [int]$HealthTimeoutSec = 1800,
    [switch]$ForceRestart,
    [switch]$DisableToolReasoningFlags,
    [string[]]$ExtraDockerArgs = @(),
    [string[]]$ExtraArgs = @()
)

$forwardArgs = @{
    Image = $Image
    ContainerName = $ContainerName
    Model = $Model
    ServedModelName = $ServedModelName
    GpuVisibleDevices = $GpuVisibleDevices
    HostPort = $HostPort
    ContainerPort = $ContainerPort
    MaxModelLen = $MaxModelLen
    GpuMemoryUtilization = $GpuMemoryUtilization
    Seed = $Seed
    HuggingFaceCacheDir = $HuggingFaceCacheDir
    HealthTimeoutSec = $HealthTimeoutSec
    ExtraDockerArgs = $ExtraDockerArgs
    ExtraArgs = $ExtraArgs
}
if ($ForceRestart) {
    $forwardArgs["ForceRestart"] = $true
}
if ($DisableToolReasoningFlags) {
    $forwardArgs["DisableToolReasoningFlags"] = $true
}

& (Join-Path $PSScriptRoot "start_vllm_gemma4_docker.ps1") @forwardArgs
