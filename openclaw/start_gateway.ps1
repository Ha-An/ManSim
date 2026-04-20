param(
  [string]$BackendModelsUrl = 'http://127.0.0.1:8000/v1/models',
  [string]$ExpectedModelId = '',
  [string]$ProfileName = 'mansim_repo',
  [string]$ProfilePath = '',
  [int]$Port = 18789
)

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$resolvedProfilePath = if ($ProfilePath) { $ProfilePath } else { Join-Path $PSScriptRoot ("profiles\{0}\openclaw.json" -f $ProfileName) }
$profileTemplatePath = [System.IO.Path]::GetFullPath($resolvedProfilePath)
$safeProfileSuffix = ($ProfileName -replace '[^A-Za-z0-9_.-]', '_')
$runtimeProfileDir = Join-Path $env:USERPROFILE ('.openclaw-{0}' -f $safeProfileSuffix)
$runtimeProfilePath = Join-Path $runtimeProfileDir 'openclaw.json'
$stateRoot = Join-Path $PSScriptRoot 'state'
$logRoot = Join-Path $PSScriptRoot 'logs'
$logPath = Join-Path $logRoot ("{0}.gateway.runtime.log" -f $safeProfileSuffix)
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

function Assert-VllmBackendReady([string]$ModelsUrl, [string]$ModelId, [string]$ResolvedProfileName) {
  try {
    $response = Invoke-RestMethod -Uri $ModelsUrl -Method Get -TimeoutSec 5
  }
  catch {
    throw "vLLM backend is not reachable at $ModelsUrl. Start the intended runtime before launching OpenClaw gateway. Gemma E4B: .\\start_vllm_gemma4_docker.ps1, legacy Qwen: .\\start_vllm_wsl.ps1. Original error: $($_.Exception.Message)"
  }

  $modelIds = @()
  foreach ($item in @($response.data)) {
    if ($item -and $item.id) {
      $modelIds += [string]$item.id
    }
  }

  if ($ModelId -and ($modelIds -notcontains $ModelId)) {
    $listed = if ($modelIds.Count -gt 0) { $modelIds -join ', ' } else { '<none>' }
    throw "vLLM backend is reachable, but expected model alias '$ModelId' is missing for profile '$ResolvedProfileName'. Exposed models: $listed. Start the matching runtime or override -ExpectedModelId."
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
Assert-VllmBackendReady -ModelsUrl $BackendModelsUrl -ModelId $ExpectedModelId -ResolvedProfileName $ProfileName

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
& $openClawCmd --profile $ProfileName gateway run --port $Port --auth none --bind loopback --force *>> $logPath
