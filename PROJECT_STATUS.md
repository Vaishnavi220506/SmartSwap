> **Erratum (2026-10-06):** the v1 results in this file are invalid. The CPU workload never ran, the control preset used the same workload, and the validity gate passed without interference. See [docs/AUDIT_V1.md](docs/AUDIT_V1.md). Current results: `research/` and `paper/`.

# SmartSwap status

## Delivered

- Native Windows environment and Win32 capability scan
- Real live physical/commit metrics via `GlobalMemoryStatusEx`
- Explainable dual-axis pressure detector
- SQLite persistence for samples, events, and experiment runs
- Bounded baseline and optimized experiments
- Reversible verified priority action on SmartSwap-owned workload
- CSV, JSON, HTML, and PDF export endpoints
- Premium local dashboard with live reservoir visualization
- Safe demo and exact startup instructions

## Honest limitations

PDH is capability-detected but not yet used for localized counter collection. The adaptive CPU preset now applies and verifies a Job Object CPU-rate cap; memory-priority, EcoQoS, and IO-specific policies are not yet implemented. PDF export is a valid minimal file rather than a fully typeset report. Existing unrelated repository deletions were preserved.

## Performance diagnosis update

The original five-second wall-clock metric has been retained for legacy runs
but is no longer the scientific responsiveness metric for new `cpu_contention`
runs. New runs use a 50 ms heartbeat with p50/p95/p99, deadline misses, jitter,
and a separate background workload. The first native CPU preset was honestly
classified `INSUFFICIENT_INTERFERENCE`: the host's 24-worker background load
did not produce material heartbeat degradation. No performance improvement is
claimed. See `docs/PERFORMANCE_DIAGNOSIS.md` and `VALIDATION_REPORT.md`.
