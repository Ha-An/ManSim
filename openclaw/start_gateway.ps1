param(
  [string]$BackendModelsUrl = 'http://127.0.0.1:8000/v1/models',
  [string]$ExpectedModelId = ''
)

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$profileTemplatePath = Join-Path $PSScriptRoot 'profiles\mansim_repo\openclaw.json'
$runtimeProfileDir = Join-Path $env:USERPROFILE '.openclaw-mansim_repo'
$runtimeProfilePath = Join-Path $runtimeProfileDir 'openclaw.json'
$stateRoot = Join-Path $PSScriptRoot 'state'
$logRoot = Join-Path $PSScriptRoot 'logs'
$logPath = Join-Path $logRoot 'gateway.runtime.log'
$localNodeDir = Join-Path $repoRoot '.tooling\node'
$localOpenClaw = Join-Path $repoRoot '.tooling\openclaw-cli\node_modules\.bin\openclaw.cmd'

if (!(Test-Path $profileTemplatePath)) { throw "Repo-local OpenClaw profile template not found: $profileTemplatePath" }

function Escape-JsonPath([string]$PathValue) {
  return $PathValue.Replace('\\', '\\\\')
}

function Get-ExpectedModelIdFromProfile([string]$ProfilePath) {
  if (!(Test-Path $ProfilePath)) {
    return ''
  }
  try {
    $profile = Get-Content -Path $ProfilePath -Raw | ConvertFrom-Json
  }
  catch {
    return ''
  }
  $provider = $profile.models.providers.vllm
  if ($null -eq $provider) {
    return ''
  }
  foreach ($entry in @($provider.models)) {
    if ($entry -and $entry.id) {
      return [string]$entry.id
    }
  }
  return ''
}

function Assert-VllmBackendReady([string]$ModelsUrl, [string]$ModelId) {
  try {
    $response = Invoke-RestMethod -Uri $ModelsUrl -Method Get -TimeoutSec 5
  }
  catch {
    throw "vLLM backend is not reachable at $ModelsUrl. Start the default Gemma 4 runtime with .\\start_vllm_gemma4_docker.ps1 before launching OpenClaw gateway. If you intentionally use the legacy Qwen runtime, start .\\start_vllm_wsl.ps1 first. Original error: $($_.Exception.Message)"
  }

  $modelIds = @()
  foreach ($item in @($response.data)) {
    if ($item -and $item.id) {
      $modelIds += [string]$item.id
    }
  }

  if ($ModelId -and ($modelIds -notcontains $ModelId)) {
    $listed = if ($modelIds.Count -gt 0) { $modelIds -join ', ' } else { '<none>' }
    throw "vLLM backend is reachable, but expected model alias '$ModelId' is missing. Exposed models: $listed. Start the default Gemma 4 runtime with .\\start_vllm_gemma4_docker.ps1 or override -ExpectedModelId for a legacy runtime."
  }
}

New-Item -ItemType Directory -Force -Path $runtimeProfileDir | Out-Null
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
if (Test-Path $localNodeDir) {
  $env:Path = "$localNodeDir;$env:Path"
}

$repoRootJson = Escape-JsonPath $repoRoot
$stateRootJson = Escape-JsonPath $stateRoot
$templateText = Get-Content -Path $profileTemplatePath -Raw
$renderedProfile = $templateText.Replace('__REPO_ROOT__', $repoRootJson).Replace('__STATE_ROOT__', $stateRootJson)
Set-Content -Path $runtimeProfilePath -Value $renderedProfile -Encoding utf8

if (-not $ExpectedModelId) {
  $ExpectedModelId = Get-ExpectedModelIdFromProfile -ProfilePath $runtimeProfilePath
}
Assert-VllmBackendReady -ModelsUrl $BackendModelsUrl -ModelId $ExpectedModelId

$openClawCmd = $null
foreach ($candidate in @(
  $localOpenClaw,
  (Get-Command openclaw.cmd -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
  (Get-Command openclaw -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
  (Join-Path $env:USERPROFILE 'AppData\Roaming\npm\openclaw.cmd')
)) {
  if ($candidate -and (Test-Path $candidate)) {
    $openClawCmd = $candidate
    break
  }
}

if (-not $openClawCmd) {
  throw "openclaw.cmd not found. Install it under .tooling or put it on PATH."
}

$env:VLLM_API_KEY = 'vllm-local'
& $openClawCmd --profile mansim_repo gateway run --port 18789 --auth none --bind loopback --force *>> $logPath
