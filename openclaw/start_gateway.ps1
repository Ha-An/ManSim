$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$profileTemplatePath = Join-Path $PSScriptRoot 'profiles\mansim_repo\openclaw.json'
$runtimeProfileDir = Join-Path $env:USERPROFILE '.openclaw-mansim_repo'
$runtimeProfilePath = Join-Path $runtimeProfileDir 'openclaw.json'
$stateRoot = Join-Path $PSScriptRoot 'state'

if (!(Test-Path $profileTemplatePath)) { throw "Repo-local OpenClaw profile template not found: $profileTemplatePath" }

function Escape-JsonPath([string]$PathValue) {
  return $PathValue.Replace('\', '\\')
}

New-Item -ItemType Directory -Force -Path $runtimeProfileDir | Out-Null
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null

$repoRootJson = Escape-JsonPath $repoRoot
$stateRootJson = Escape-JsonPath $stateRoot
$templateText = Get-Content -Path $profileTemplatePath -Raw
$renderedProfile = $templateText.Replace('__REPO_ROOT__', $repoRootJson).Replace('__STATE_ROOT__', $stateRootJson)
Set-Content -Path $runtimeProfilePath -Value $renderedProfile -Encoding utf8

$env:OLLAMA_API_KEY = 'ollama-local'
$env:OLLAMA_BASE_URL = 'http://localhost:11434'
& 'C:\Users\Hanyang University\AppData\Roaming\npm\openclaw.cmd' --profile mansim_repo gateway run --port 18789 --auth none --bind loopback --force *>> 'C:\Github\ManSim\openclaw\logs\gateway.runtime.log'
