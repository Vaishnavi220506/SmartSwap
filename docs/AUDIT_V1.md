# Audit of the SmartSwap v1 evaluation (2026-10-06)

The v1 results in `VALIDATION_REPORT.md` (for example "CPU contention −9.49%",
"I/O contention −16.21%, statistically reliable") **are not valid evidence** and
must not be cited. Each problem below was reproduced on the original host.

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| 1 | CPU workers never ran. `BACKGROUND_CHILD` started `multiprocessing.Process(target=burn)` from a `python -c` script; on Windows the *spawn* start method cannot re-import `__main__.burn`, so every worker died with `AttributeError` and the background process exited after ~0.12 s. | Re-running the v1 child prints `AttributeError: module '__main__' has no attribute 'burn'` once per worker. | The "CPU contention" and the CPU part of "mixed" applied no CPU load during the measurement window. |
| 2 | The control was not a control. `no_pressure_control` mapped to the same `cpu` workload as `cpu_contention`. | `app.py` v1 preset table. | Both presets measured the same (absent) load: p95 ≈ 7.2 ms vs 8.3 ms. |
| 3 | Validity gate passed on probe execution time, not interference. `p95 >= 5 ms` was met by the probe's own ~4–5 ms compute step; scheduling delay stayed ≈0.5 ms in every preset. | `latency_samples`: mean scheduling delay 0.38–0.60 ms across all presets. | "VALID_INTERFERENCE" was recorded for runs without interference. |
| 4 | "No unnecessary actions" was hard-coded. Actions were skipped by `if preset != 'no_pressure_control'`, not by detection. | v1 `run_heartbeat_experiment`. | The zero-action control result says nothing about the detector. |
| 5 | Reverts were claimed, not done. Closing a Job Object handle does not remove a CPU-rate cap from processes still in the job; v1 logged `job_cpu_rate_cap_reverted` as verified without clearing or reading back the cap. | Win32 Job Object semantics. | Recovery events were not evidence of recovery. |
| 6 | Statistics. The paired interval used t = 1.96 for every n > 5 (too narrow for n = 6–30), baseline always ran before optimized (order effect), and the cost to the background was never measured (`background_throughput` was always NULL). | v1 `comparison()`, `_run_paired_batch`. | Reliability claims were overstated and the protection–cost trade-off was invisible. |
| 7 | Memory "pressure" of 256 MB on a 32 GB host produced no measurable pressure; storage writes from one thread did not contend on NVMe. | Pilot measurements. | Memory/I/O presets measured noise. |

## What v2 changes

- Workers are independent processes (no `multiprocessing`), signal `READY`, and stop together on a named event; their work is counted inside the foreground window.
- Workloads were checked to interfere: CPU, memory-bandwidth (multi-process 64 MiB copies), storage (16 × 1 MiB unbuffered reads), and mixed. A true no-interference control exists.
- The foreground probe records scheduling delay, compute, memory-stream, and unbuffered-read components separately.
- Mitigation is decided by a canary-based controller, never by preset name. Every action and every revert is verified by kernel read-back. Caps are explicitly cleared.
- Statistics use exact Wilcoxon tests, Student t for any n, bootstrap CIs, Holm correction, randomized block order (ABBA in the dashboard's paired mode), and background throughput.

See `smartswap/research/` for the v2 study and `smartswap/paper/` for the write-up.
