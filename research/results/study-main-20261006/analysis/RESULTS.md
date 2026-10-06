# SmartSwap study study-main-20261006

Host: Intel(R) Core(TM) Ultra 9 285H, 16 logical CPUs, 31.4 GB, Windows-11-10.0.26200-SP0, AC power: True.
Trials: 340 (0 with errors), 10 randomised blocks.

Non-study host CPU load just before trials: median 30%, p90 43%, max 71%.

## Interference validity (unmitigated vs. no-interference p95)

| Workload | idle p95 | loaded p95 | inflation | Wilcoxon p |
|---|---:|---:|---:|---:|
| CPU | 3.95 | 34.92 | 8.91x | 0.004 |
| Memory bandwidth | 3.95 | 10.84 | 2.60x | 0.084 |
| Storage reads | 3.95 | 4.98 | 1.26x | 0.232 |
| Mixed | 3.95 | 29.90 | 6.52x | 0.002 |

## Paired comparisons (median per-block ratio, 95% bootstrap CI, Holm-adjusted exact Wilcoxon)

| Workload | Policy | vs | metric | n | ratio [CI] | p (Holm) | BG kept |
|---|---|---|---|---:|---|---:|---:|
| No interference | Static priority | None | p95_ms | 10 | 0.89 [0.72, 1.00] | 0.469 | - |
| No interference | SmartSwap v1 (static cap) | None | p95_ms | 10 | 0.80 [0.56, 1.07] | 1.000 | - |
| No interference | SmartSwap v2 (adaptive) | None | p95_ms | 10 | 0.79 [0.56, 0.98] | 0.742 | - |
| No interference | SmartSwap v2 (adaptive) | SmartSwap v1 (static cap) | p95_ms | 10 | 1.01 [0.75, 1.11] | 1.000 | - |
| No interference | SmartSwap v2 (adaptive) | Static priority | p95_ms | 10 | 0.92 [0.77, 1.07] | 1.000 | - |
| CPU | Static priority | None | p95_ms | 10 | 0.13 [0.03, 0.17] | 0.072 | 66% |
| CPU | SmartSwap v1 (static cap) | None | p95_ms | 10 | 0.09 [0.02, 0.13] | 0.072 | 51% |
| CPU | SmartSwap v2 (adaptive) | None | p95_ms | 10 | 0.13 [0.03, 0.16] | 0.072 | 70% |
| CPU | SmartSwap v2 (adaptive) | SmartSwap v1 (static cap) | p95_ms | 10 | 1.17 [0.92, 1.60] | 1.000 | 70% |
| CPU | SmartSwap v2 (adaptive) | Static priority | p95_ms | 10 | 0.99 [0.86, 1.14] | 1.000 | 70% |
| Memory bandwidth | Static priority | None | p95_ms | 10 | 0.57 [0.44, 0.97] | 0.469 | 98% |
| Memory bandwidth | SmartSwap v1 (static cap) | None | p95_ms | 10 | 0.57 [0.40, 0.79] | 0.105 | 76% |
| Memory bandwidth | SmartSwap v2 (adaptive) | None | p95_ms | 10 | 0.60 [0.43, 0.81] | 0.146 | 61% |
| Memory bandwidth | SmartSwap v2 (adaptive) | SmartSwap v1 (static cap) | p95_ms | 10 | 1.02 [0.95, 1.08] | 1.000 | 61% |
| Memory bandwidth | SmartSwap v2 (adaptive) | Static priority | p95_ms | 10 | 0.95 [0.87, 0.99] | 0.574 | 61% |
| Storage reads | Static priority | None | p95_ms | 10 | 0.58 [0.46, 0.81] | 0.072 | 27% |
| Storage reads | SmartSwap v1 (static cap) | None | p95_ms | 10 | 1.01 [0.96, 1.06] | 1.000 | 101% |
| Storage reads | SmartSwap v2 (adaptive) | None | p95_ms | 10 | 0.52 [0.47, 0.75] | 0.105 | 24% |
| Storage reads | SmartSwap v2 (adaptive) | SmartSwap v1 (static cap) | p95_ms | 10 | 0.55 [0.48, 0.71] | 0.072 | 24% |
| Storage reads | SmartSwap v2 (adaptive) | Static priority | p95_ms | 10 | 0.94 [0.78, 1.22] | 1.000 | 24% |
| Mixed | Static priority | None | p95_ms | 10 | 0.14 [0.08, 0.19] | 0.072 | 67% |
| Mixed | SmartSwap v1 (static cap) | None | p95_ms | 10 | 0.14 [0.08, 0.19] | 0.072 | 67% |
| Mixed | SmartSwap v2 (adaptive) | None | p95_ms | 10 | 0.14 [0.09, 0.21] | 0.072 | 67% |
| Mixed | SmartSwap v2 (adaptive) | SmartSwap v1 (static cap) | p95_ms | 10 | 1.17 [0.82, 1.33] | 1.000 | 67% |
| Mixed | SmartSwap v2 (adaptive) | Static priority | p95_ms | 10 | 1.10 [0.90, 1.22] | 1.000 | 67% |
| CPU | v2 w/o ladder | SmartSwap v2 (adaptive) | p95_ms | 10 | 4.82 [3.38, 7.01] | 0.072 | 30% |
| CPU | v2 w/o AIMD | SmartSwap v2 (adaptive) | p95_ms | 10 | 1.03 [0.95, 1.20] | 1.000 | 67% |
| CPU | v2 w/o benefit check | SmartSwap v2 (adaptive) | p95_ms | 10 | 0.92 [0.83, 1.32] | 1.000 | 67% |
| Memory bandwidth | v2 w/o ladder | SmartSwap v2 (adaptive) | p95_ms | 10 | 1.07 [1.00, 1.37] | 0.469 | 53% |
| Memory bandwidth | v2 w/o AIMD | SmartSwap v2 (adaptive) | p95_ms | 10 | 1.11 [1.00, 1.18] | 0.742 | 97% |
| Memory bandwidth | v2 w/o benefit check | SmartSwap v2 (adaptive) | p95_ms | 10 | 0.98 [0.89, 1.02] | 1.000 | 61% |
| Mixed | v2 w/o ladder | SmartSwap v2 (adaptive) | p95_ms | 10 | 3.84 [2.94, 7.87] | 0.072 | 40% |
| Mixed | v2 w/o AIMD | SmartSwap v2 (adaptive) | p95_ms | 10 | 0.97 [0.86, 1.32] | 1.000 | 69% |
| Mixed | v2 w/o benefit check | SmartSwap v2 (adaptive) | p95_ms | 10 | 0.97 [0.90, 1.13] | 1.000 | 66% |
| No interference | Observe-only | None | p50_ms | 10 | 0.95 [0.80, 1.13] | 1.000 | - |
| No interference | Observe-only | None | p95_ms | 10 | 0.84 [0.67, 0.97] | 0.879 | - |
| No interference | Observe-only | None | p99_ms | 10 | 0.85 [0.59, 0.94] | 0.879 | - |

## Attribution (observe-only runs)

- CPU: 305/330 periods correct (92.4%)
- Memory bandwidth: 259/302 periods correct (85.8%)
- Storage reads: 280/281 periods correct (99.6%)
- No interference: 1.2% of periods above the engage threshold

## Is WSI a proxy for foreground latency? (observe-only trials)

Spearman rho between trial-mean WSI and foreground p95: 0.96 (p=<0.001, n=50).
False-positive rate at the engage threshold on no-interference periods: 1.2%.
- CPU: AUC 0.965, detected in 95.1% of periods at the engage threshold
- Memory bandwidth: AUC 0.997, detected in 89.3% of periods at the engage threshold
- Storage reads: AUC 0.995, detected in 82.9% of periods at the engage threshold
- Mixed: AUC 1.000, detected in 100.0% of periods at the engage threshold

## Action ledger

1575/1575 actions verified by kernel read-back; 322/322 reverts verified; 10 caps rolled back by the benefit check.

## Host-load robustness (Spearman, pre-trial host CPU vs unmitigated p95)

- No interference: rho=0.38, p=0.271, n=10
- CPU: rho=0.83, p=0.004, n=10
- Memory bandwidth: rho=0.52, p=0.126, n=10
- Storage reads: rho=0.16, p=0.645, n=10
- Mixed: rho=0.03, p=0.945, n=10
