# ADP Rollout Process Benchmark

This benchmark measures CPU rollout throughput while keeping the ADP checkpoint, episode seeds,
worker count, simulation horizon, epsilon, and compact Monte Carlo sample collection fixed. Only
the process count changes. Each setting processes the same 20 episodes; smaller process counts use
more waves.

```powershell
.\.venv\Scripts\python.exe experiments\adp_rollout_process_benchmark\benchmark.py `
  --checkpoint outputs\adp_training_worker3_conservative\<timestamp>\best.pt `
  --process-counts 1 2 5 10 15 20
```

Results are written under `outputs/adp_rollout_process_benchmark/<timestamp>/`. Compare
`wall_sec` or `episodes_per_hour` in `benchmark_summary.csv`. The benchmark also verifies that
product and decision counts are identical across process counts for every fixed seed.

To measure one simultaneous wave at each process count, use `--single-wave`. This runs one episode
per process: one episode with one process, two episodes with two processes, and so on.

```powershell
.\.venv\Scripts\python.exe experiments\adp_rollout_process_benchmark\benchmark.py `
  --checkpoint outputs\adp_training_worker3_conservative\<timestamp>\best.pt `
  --process-counts 1 2 5 10 15 20 --single-wave
```
