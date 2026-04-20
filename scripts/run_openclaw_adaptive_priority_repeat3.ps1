param(
  [string]$Label = "iter",
  [int]$RepeatCount = 3,
  [double]$BaselineProducts = 20.0,
  [double]$BaselineWallSec = 228.0,
  [double]$BaselineClosure = 0.89
)

$ErrorActionPreference = 'Stop'

$repo = 'C:\Github\ManSim'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$dayRoot = Join-Path $repo ("outputs\" + (Get-Date -Format 'yyyy-MM-dd'))
$root = Join-Path $dayRoot ("adaptive_tuning_" + $Label)
New-Item -ItemType Directory -Path $root -Force | Out-Null

$runs = @()

function Mean($xs){ if(-not $xs -or $xs.Count -eq 0){ return 0.0 }; return ($xs | Measure-Object -Average).Average }
function Std($xs){ if(-not $xs -or $xs.Count -lt 2){ return 0.0 }; $m = Mean $xs; $sum = 0.0; foreach($x in $xs){ $sum += [math]::Pow(([double]$x - $m),2) }; return [math]::Sqrt($sum / $xs.Count) }

for ($i = 1; $i -le $RepeatCount; $i++) {
  Write-Host ("[repeat {0}/{1}] starting" -f $i, $RepeatCount)
  & $python "$repo\main.py" decision=openclaw_adaptive_priority runtime.ui.auto_open_results=false | Out-Host
  $latest = Get-ChildItem $dayRoot -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
  $kpiPath = Join-Path $latest.FullName 'kpi.json'
  $kpi = Get-Content $kpiPath -Raw | ConvertFrom-Json
  $runs += [pscustomobject]@{
    run = $i
    output_dir = $latest.FullName
    total_products = [double]$kpi.total_products
    wall_clock_sec = [double]$kpi.wall_clock_sec
    closure = [double]$kpi.downstream_closure_ratio
    strategist_calls = [double]$kpi.llm_transport_metrics.by_phase.manager_shift_strategist.calls
    reviewer_calls = [double]$kpi.llm_transport_metrics.by_phase.manager_daily_reviewer.calls
  }
}

$prod = @($runs.total_products)
$wall = @($runs.wall_clock_sec)
$closure = @($runs.closure)
$avgWall = [double](Mean $wall)
$summary = [ordered]@{
  baseline_reference = [ordered]@{
    avg_products = $BaselineProducts
    avg_wall_sec = $BaselineWallSec
    avg_closure = $BaselineClosure
  }
  candidate = [ordered]@{
    avg_products = [math]::Round((Mean $prod), 3)
    std_products = [math]::Round((Std $prod), 3)
    avg_wall_sec = [math]::Round($avgWall, 3)
    std_wall_sec = [math]::Round((Std $wall), 3)
    avg_wall_human = ('{0}m {1}s' -f [math]::Floor($avgWall / 60.0), [math]::Floor($avgWall % 60.0))
    avg_closure = [math]::Round((Mean $closure), 6)
    std_closure = [math]::Round((Std $closure), 6)
    avg_strategist_calls = [math]::Round((Mean @($runs.strategist_calls)), 3)
    avg_reviewer_calls = [math]::Round((Mean @($runs.reviewer_calls)), 3)
  }
  delta_vs_baseline = [ordered]@{
    products = [math]::Round(((Mean $prod) - $BaselineProducts), 3)
    wall_sec = [math]::Round(($avgWall - $BaselineWallSec), 3)
    closure = [math]::Round(((Mean $closure) - $BaselineClosure), 6)
  }
  runs = $runs
}

$summary["candidate_repeat$RepeatCount"] = $summary["candidate"]
$summary.Remove("candidate")

$out = Join-Path $root ("repeat{0}_summary.json" -f $RepeatCount)
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $out -Encoding UTF8
Get-Content $out
