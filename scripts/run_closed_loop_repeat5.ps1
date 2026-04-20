$ErrorActionPreference = 'Stop'
$root = 'C:\Github\ManSim\outputs\2026-04-20\closed_loop_repeat5'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$python = 'C:\Github\ManSim\.venv\Scripts\python.exe'
$repo = 'C:\Github\ManSim'
$runs = @()
for ($i = 1; $i -le 5; $i++) {
  Write-Host ("[repeat {0}/5] starting" -f $i)
  & $python "$repo\main.py" decision=openclaw_adaptive_priority runtime.ui.auto_open_results=false | Out-Host
  $latest = Get-ChildItem "$repo\outputs\2026-04-20" -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
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
function Mean($xs){ if(-not $xs -or $xs.Count -eq 0){ return 0.0 }; return ($xs | Measure-Object -Average).Average }
function Std($xs){ if(-not $xs -or $xs.Count -lt 2){ return 0.0 }; $m = Mean $xs; $sum = 0.0; foreach($x in $xs){ $sum += [math]::Pow(([double]$x - $m),2) }; return [math]::Sqrt($sum / $xs.Count) }
$prod = @($runs.total_products)
$wall = @($runs.wall_clock_sec)
$closure = @($runs.closure)
$avgWall = [double](Mean $wall)
$avgWallMin = [math]::Floor($avgWall / 60.0)
$avgWallSec = [math]::Floor($avgWall % 60.0)
$summary = [ordered]@{
  baseline_reference = [ordered]@{
    source = 'prior empirical baseline'
    avg_products = 20.0
    avg_wall_sec = 228.0
    avg_wall_human = '3m 48s'
    avg_closure = 0.89
  }
  redesigned_repeat5 = [ordered]@{
    avg_products = [math]::Round((Mean $prod), 3)
    std_products = [math]::Round((Std $prod), 3)
    avg_wall_sec = [math]::Round($avgWall, 3)
    std_wall_sec = [math]::Round((Std $wall), 3)
    avg_wall_human = ('{0}m {1}s' -f $avgWallMin, $avgWallSec)
    avg_closure = [math]::Round((Mean $closure), 6)
    std_closure = [math]::Round((Std $closure), 6)
    avg_strategist_calls = [math]::Round((Mean @($runs.strategist_calls)), 3)
    avg_reviewer_calls = [math]::Round((Mean @($runs.reviewer_calls)), 3)
  }
  delta_vs_baseline = [ordered]@{
    products = [math]::Round(((Mean $prod) - 20.0), 3)
    wall_sec = [math]::Round(((Mean $wall) - 228.0), 3)
    closure = [math]::Round(((Mean $closure) - 0.89), 6)
  }
  runs = $runs
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $root 'repeat5_summary.json') -Encoding UTF8
Get-Content (Join-Path $root 'repeat5_summary.json')
