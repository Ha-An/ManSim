# Worker 3 ADP OOD Quick Validation

This experiment compares a short control training run with a treatment that
uses broader exploration and a conservative policy-update gate.

## Conditions

- Scenario: `mfg_flow_shop`
- Objective: `maximize_throughput`
- Workers: 3
- Horizon: 2 simulation days
- Policy updates: 4
- Episodes per update: 40
- Explicit WAIT action: disabled
- Episode events and replay artifacts: disabled

The control uses 20 initial random episodes and epsilon `0.20 -> 0.10`. The
treatment uses 100 initial random episodes, epsilon `0.50 -> 0.20`, and accepts
a candidate only when its mean completed-product count is strictly higher than
the incumbent on the same three fixed validation seeds.

## Run

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train `
  --config configs/adp/mfg_flow_shop_throughput_worker3_ood_control_quick.yaml `
  --no-open-dashboard

.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train `
  --config configs/adp/mfg_flow_shop_throughput_worker3_ood_treatment_quick.yaml `
  --no-open-dashboard
```

Use the timestamp directories printed by the two commands to render the joint
diagnostic dashboard.

```powershell
.\.venv\Scripts\python.exe -m experiments.adp_ood_quick_validation.render_dashboard `
  --control outputs/adp_ood_quick/control/<timestamp> `
  --treatment outputs/adp_ood_quick/treatment/<timestamp> `
  --output outputs/adp_ood_quick/<comparison_timestamp>/comparison_dashboard.html
```

The treatment iteration table records candidate and incumbent production,
accept/reject status, rollback hash checks, and the checkpoint used by the next
rollout. The comparison dashboard reports OOD selection rate, OOD
overestimation excess, incumbent stability, and the final `해결`, `억제`, or
`미해결` diagnosis.
