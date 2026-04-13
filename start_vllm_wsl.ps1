# Legacy Qwen vLLM launcher.
# - 기본 경로는 start_vllm_gemma4_docker.ps1 이다.
# - 이 스크립트는 decision=llm_planner_qwen_legacy 조합에서만 사용한다.
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$VenvPath = "~/vllm-env",
    [string]$Model = "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8",
    [string]$ServedModelName = "mansim-qwen3-30b-a3b",
    [int]$Port = 8000,
    [string]$ListenHost = "0.0.0.0",
    [int]$MaxModelLen = 32768,
    [double]$GpuMemoryUtilization = 0.90,
    [int]$TensorParallelSize = 2,
    [bool]$EnableAutoToolChoice = $true,
    [string]$ToolCallParser = "hermes",
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$extraParts = New-Object System.Collections.Generic.List[string]
if ($EnableAutoToolChoice) {
    $extraParts.Add("--enable-auto-tool-choice")
}
if ($ToolCallParser) {
    $extraParts.Add("--tool-call-parser")
    $extraParts.Add($ToolCallParser)
}
foreach ($arg in $ExtraArgs) {
    if ($null -ne $arg -and "$arg".Trim()) {
        $extraParts.Add("$arg".Trim())
    }
}
$extra = [string]::Join(' ', $extraParts)
$wslCommand = @'
set -e
if [ -f ~/vllm.pid ]; then
  existing_pid=$(cat ~/vllm.pid 2>/dev/null || true)
  if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
    echo VLLM_ALREADY_RUNNING
    exit 0
  fi
fi
source __VENV_PATH__/bin/activate
nohup __VENV_PATH__/bin/vllm serve "__MODEL__" --served-model-name "__SERVED_MODEL_NAME__" --host "__LISTEN_HOST__" --port "__PORT__" --max-model-len "__MAX_MODEL_LEN__" --gpu-memory-utilization "__GPU_MEMORY_UTILIZATION__" --tensor-parallel-size "__TENSOR_PARALLEL_SIZE__" __EXTRA_ARGS__ > ~/vllm.log 2>&1 &
echo $! > ~/vllm.pid
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:__PORT__/v1/models" >/dev/null 2>&1; then
    echo VLLM_READY
    exit 0
  fi
  current_pid=$(cat ~/vllm.pid 2>/dev/null || true)
  if [ -z "$current_pid" ] || ! kill -0 "$current_pid" 2>/dev/null; then
    echo VLLM_FAILED
    exit 1
  fi
  sleep 3
done
echo VLLM_TIMEOUT
exit 1
'@
$wslCommand = $wslCommand.Replace('__VENV_PATH__', $VenvPath)
$wslCommand = $wslCommand.Replace('__MODEL__', $Model)
$wslCommand = $wslCommand.Replace('__SERVED_MODEL_NAME__', $ServedModelName)
$wslCommand = $wslCommand.Replace('__LISTEN_HOST__', $ListenHost)
$wslCommand = $wslCommand.Replace('__PORT__', "$Port")
$wslCommand = $wslCommand.Replace('__MAX_MODEL_LEN__', "$MaxModelLen")
$wslCommand = $wslCommand.Replace('__GPU_MEMORY_UTILIZATION__', "$GpuMemoryUtilization")
$wslCommand = $wslCommand.Replace('__TENSOR_PARALLEL_SIZE__', "$TensorParallelSize")
$wslCommand = $wslCommand.Replace('__EXTRA_ARGS__', $extra)

$tempScriptPath = Join-Path $env:TEMP "mansim_start_vllm.sh"
[System.IO.File]::WriteAllText($tempScriptPath, $wslCommand.Replace("`r`n", "`n"), [System.Text.UTF8Encoding]::new($false))
$drive = $tempScriptPath.Substring(0, 1).ToLowerInvariant()
$rest = $tempScriptPath.Substring(2).Replace('\', '/')
$wslScriptPath = "/mnt/$drive$rest"

try {
    wsl.exe -d $Distro -- bash $wslScriptPath
}
finally {
    Remove-Item -LiteralPath $tempScriptPath -Force -ErrorAction SilentlyContinue
}
