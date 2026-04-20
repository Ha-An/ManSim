param(
    [string]$Image = "vllm/vllm-openai:gemma4-cu130",
    [string]$ContainerName = "mansim-gemma4-cu130",
    [string]$Model = "google/gemma-4-E4B-it",
    [string]$ServedModelName = "mansim-gemma4-e4b",
    [string]$GpuVisibleDevices = "1",
    [int]$HostPort = 8000,
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

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$script:DockerExe = $null

if ($null -eq $Seed) {
    $configPath = Join-Path $PSScriptRoot "configs\\config.yaml"
    if (Test-Path $configPath) {
        $seedLine = Get-Content $configPath | Where-Object { $_ -match '^\s*seed:\s*\d+\s*$' } | Select-Object -First 1
        if ($seedLine -match '^\s*seed:\s*(\d+)\s*$') {
            $Seed = [int]$Matches[1]
        }
    }
}

function Require-Command([string]$CommandName, [string]$InstallHint) {
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $cmd -and $CommandName -eq "docker") {
        $fallback = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        if (Test-Path $fallback) {
            $script:DockerExe = $fallback
            return $fallback
        }
    }
    if ($null -eq $cmd) {
        throw "$CommandName is not installed. $InstallHint"
    }
    $script:DockerExe = $cmd.Source
    return $cmd.Source
}

function Ensure-DockerBinOnPath() {
    if (-not $script:DockerExe) {
        return
    }
    $dockerBinDir = Split-Path -Parent $script:DockerExe
    if (-not [string]::IsNullOrWhiteSpace($dockerBinDir)) {
        $pathValue = [string]$env:Path
        if ($pathValue -notlike "*$dockerBinDir*") {
            $env:Path = "$dockerBinDir;$pathValue"
        }
    }
}

function Invoke-Docker([string[]]$Args) {
    & $script:DockerExe @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker command failed: docker $($Args -join ' ')"
    }
}

function Test-ContainerRunning([string]$Name) {
    $status = & $script:DockerExe ps --filter "name=^/$Name$" --format "{{.Names}}`t{{.Status}}"
    return $LASTEXITCODE -eq 0 -and [string]::IsNullOrWhiteSpace(($status | Out-String)) -eq $false
}

function Test-ContainerExists([string]$Name) {
    $status = & $script:DockerExe ps -a --filter "name=^/$Name$" --format "{{.Names}}`t{{.Status}}"
    return $LASTEXITCODE -eq 0 -and [string]::IsNullOrWhiteSpace(($status | Out-String)) -eq $false
}

function Get-ContainerStatus([string]$Name) {
    $status = & $script:DockerExe ps -a --filter "name=^/$Name$" --format "{{.Status}}"
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return (($status | Out-String).Trim())
}

function Wait-HttpReady([string]$Url, [int]$TimeoutSec, [string]$ContainerName) {
    $deadline = (Get-Date).AddSeconds([Math]::Max(30, $TimeoutSec))
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 5
            return $response
        }
        catch {
            if ($ContainerName) {
                $status = Get-ContainerStatus -Name $ContainerName
                if ([string]::IsNullOrWhiteSpace($status)) {
                    throw "Container '$ContainerName' disappeared before vLLM became ready."
                }
                if ($status -notmatch '^Up\b') {
                    throw "Container '$ContainerName' exited before vLLM became ready. Docker status: $status"
                }
            }
            Start-Sleep -Seconds 5
        }
    }
    throw "Timed out waiting for vLLM runtime at $Url"
}

function Write-ContainerLogsSnapshot([string]$Name, [string]$DestinationPath) {
    try {
        $logs = & $script:DockerExe logs $Name 2>&1
        if ($LASTEXITCODE -eq 0) {
            $logs | Set-Content -Path $DestinationPath -Encoding utf8
        }
    }
    catch {
        # Best-effort log capture only.
    }
}

$null = Require-Command -CommandName "docker" -InstallHint "Install Docker Desktop and enable WSL2 integration for Ubuntu-24.04."
Ensure-DockerBinOnPath
try {
    & $script:DockerExe info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "docker info failed"
    }
}
catch {
    throw "Docker daemon is not available. Start Docker Desktop and make sure WSL2 integration is enabled before launching Gemma 4 runtime."
}

if (-not $HuggingFaceCacheDir) {
    $HuggingFaceCacheDir = Join-Path $env:USERPROFILE ".cache\huggingface"
}

$logDir = Join-Path $PSScriptRoot "openclaw\logs"
$safeContainerSlug = (($ContainerName -replace '[^A-Za-z0-9._-]', '_').Trim('_'))
if ([string]::IsNullOrWhiteSpace($safeContainerSlug)) {
    $safeContainerSlug = "gemma4-cu130"
}
$runtimeLogPath = Join-Path $logDir "$safeContainerSlug.runtime.log"
$launcherLogPath = Join-Path $logDir "$safeContainerSlug.launcher.log"

New-Item -ItemType Directory -Force -Path $HuggingFaceCacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if ($ForceRestart -and (Test-ContainerExists -Name $ContainerName)) {
    "[$(Get-Date -Format o)] force restarting container '$ContainerName'" | Add-Content -Path $launcherLogPath
    Invoke-Docker -Args @("rm", "-f", $ContainerName)
}

if (Test-ContainerRunning -Name $ContainerName) {
    "[$(Get-Date -Format o)] container '$ContainerName' already running" | Add-Content -Path $launcherLogPath
    $response = Wait-HttpReady -Url "http://127.0.0.1:$HostPort/v1/models" -TimeoutSec 60 -ContainerName $ContainerName
    Write-ContainerLogsSnapshot -Name $ContainerName -DestinationPath $runtimeLogPath
    Write-Output "GEMMA4_RUNTIME_ALREADY_RUNNING"
    exit 0
}

if (Test-ContainerExists -Name $ContainerName) {
    Invoke-Docker -Args @("rm", "-f", $ContainerName)
}

$imagePresent = $false
try {
    $null = & $script:DockerExe image inspect $Image 2>$null
    $imagePresent = ($LASTEXITCODE -eq 0)
}
catch {
    $imagePresent = $false
}
if (-not $imagePresent) {
    "[$(Get-Date -Format o)] pulling image $Image" | Add-Content -Path $launcherLogPath
    Invoke-Docker -Args @("pull", $Image)
}

$dockerArgs = @(
    "run",
    "--detach",
    "--name", $ContainerName,
    "--gpus", "all",
    "--ipc=host",
    "-p", "${HostPort}:${ContainerPort}",
    "-v", "${HuggingFaceCacheDir}:/root/.cache/huggingface",
    "-e", "NVIDIA_VISIBLE_DEVICES=$GpuVisibleDevices",
    "-e", "CUDA_VISIBLE_DEVICES=$GpuVisibleDevices"
)
if ($env:HF_TOKEN) {
    $dockerArgs += @("-e", "HF_TOKEN=$($env:HF_TOKEN)")
}
if ($env:HUGGING_FACE_HUB_TOKEN) {
    $dockerArgs += @("-e", "HUGGING_FACE_HUB_TOKEN=$($env:HUGGING_FACE_HUB_TOKEN)")
}
foreach ($arg in $ExtraDockerArgs) {
    if ($null -ne $arg -and "$arg".Trim()) {
        $dockerArgs += "$arg".Trim()
    }
}
$dockerArgs += @(
    $Image,
    "--model", $Model,
    "--served-model-name", $ServedModelName,
    "--host", "0.0.0.0",
    "--port", "$ContainerPort",
    "--max-model-len", "$MaxModelLen",
    "--gpu-memory-utilization", "$GpuMemoryUtilization"
)
if ($null -ne $Seed) {
    $dockerArgs += @("--seed", "$Seed")
}
if (-not $DisableToolReasoningFlags) {
    $dockerArgs += @(
        "--enable-auto-tool-choice",
        "--tool-call-parser", "gemma4",
        "--reasoning-parser", "gemma4"
    )
}
foreach ($arg in $ExtraArgs) {
    if ($null -ne $arg -and "$arg".Trim()) {
        $dockerArgs += "$arg".Trim()
    }
}

"[$(Get-Date -Format o)] starting container '$ContainerName' for model '$Model'" | Add-Content -Path $launcherLogPath
$containerId = ""
try {
    $containerId = ((& $script:DockerExe @dockerArgs) | Out-String).Trim()
}
catch {
    throw "Failed to start docker container '$ContainerName'. Original error: $($_.Exception.Message)"
}
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($containerId)) {
    throw "Failed to start docker container '$ContainerName'."
}

try {
    $response = Wait-HttpReady -Url "http://127.0.0.1:$HostPort/v1/models" -TimeoutSec $HealthTimeoutSec -ContainerName $ContainerName
}
catch {
    Write-ContainerLogsSnapshot -Name $ContainerName -DestinationPath $runtimeLogPath
    throw "Gemma 4 runtime failed to become ready. Check $runtimeLogPath for container logs. Original error: $($_.Exception.Message)"
}
$response | ConvertTo-Json -Depth 8 | Set-Content -Path $runtimeLogPath -Encoding utf8

$modelIds = @()
foreach ($item in @($response.data)) {
    if ($item -and $item.id) {
        $modelIds += [string]$item.id
    }
}
if ($modelIds -notcontains $ServedModelName) {
    Write-ContainerLogsSnapshot -Name $ContainerName -DestinationPath $runtimeLogPath
    throw "Gemma 4 runtime started, but expected alias '$ServedModelName' was not exposed. Found models: $($modelIds -join ', ')"
}

Write-ContainerLogsSnapshot -Name $ContainerName -DestinationPath $runtimeLogPath

Write-Output "GEMMA4_RUNTIME_READY"
