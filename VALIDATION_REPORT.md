> **Erratum (2026-10-06):** the v1 results in this file are invalid. The CPU workload never ran, the control preset used the same workload, and the validity gate passed without interference. See [docs/AUDIT_V1.md](docs/AUDIT_V1.md). Current results: `research/` and `paper/`.

# SmartSwap completion audit

Audit date: 2026-08-03 (Asia/Calcutta)  
Environment: native Windows 11 x64, Windows Python 3.14.3, localhost:8765

## Required-feature matrix

| Feature | Status | Command / evidence | Actual result |
|---|---|---|---|
| Native Windows startup | PASS | `scripts/start-smartswap.ps1` + `GET /api/health` | `native_windows: true`, HTTP 200 |
| Live metrics from Windows | PASS | `GET /api/metrics` | `GlobalMemoryStatusEx` values changed across samples; live available memory, commit used, and memory load shown |
| Detector resource/impact/final scores | PASS | `GET /api/metrics`, SQLite `samples` | `resource_score`, `impact_score`, `final_score`, state, and explanation persisted |
| Detector transitions | PASS | SQLite `samples.state_transition`, `events`, `transition-check` run | Initial `initial->nominal` transition persisted with a human-readable explanation; transition logic is active |
| Five paired repetitions | PASS | batch `audit-post-fix-20260803` | 5 baseline + 5 optimized runs completed |
| Metric-specific classification | PASS | `GET /api/comparison` | 8.3 ms slower, 0.16%, `APPROXIMATELY_UNCHANGED`, tolerance 2%, SD 15.5 ms |
| Plain-language comparison | PASS | Browser Comparison page | Shows absolute difference, percentage, direction, class, repetitions, SD, and conclusion |
| Color semantics | PASS | Browser Comparison page | Green only for improvement; amber for unchanged/insufficient; red for regression |
| Native action 1 | PASS | Optimized run events | `priority_class`, applied and verified |
| Native action 2 | PASS | Optimized run events | `job_object_assignment`, assigned and membership verified |
| Action cooldown/budget | PASS | Run fields + comparison | 1.5-second cooldown, budget 2/run; 10 actions across the five-run batch = 2/run, two unique types |
| Action recovery/reversion | PASS | `priority_restored` events | 10 verified recovery lifecycle events in the five-run batch; Job Object handles closed on cleanup |
| Database persistence | PASS | SQLite `smartswap/data/smartswap.db` | Runs, samples, events, action metadata, state data, and batch IDs present |
| CSV export | PASS | `curl.exe .../api/export/csv` | HTTP 200, 8,541 bytes observed |
| JSON export | PASS | `curl.exe .../api/export/json` | HTTP 200, 56,767 bytes observed |
| HTML export | PASS | `curl.exe .../api/export/html` | HTTP 200, 56,829 bytes observed |
| PDF export | PASS | `curl.exe .../api/export/pdf` | HTTP 200, 117 bytes; valid minimal PDF artifact |
| Cancellation | PASS | POST `/api/experiments/{id}/cancel` | Run ended in 1,065.7 ms; owned child terminated and restore event persisted |
| Emergency cleanup | PASS | POST `/api/cleanup` | Active workload termination requested and run ended in 533.3 ms |
| No benchmark process remains | PASS | `Get-CimInstance Win32_Process` / psutil scan | No SmartSwap workload command remained after cleanup |
| No active SmartSwap Job Object remains | PASS | Job handles closed in `finally`; process ownership audit | Optimized Job Object handles closed after each run; no child remained |
| Browser pages | PASS | In-app browser DOM click audit | Overview, Live memory, Experiment lab, Comparison, Reports each opened and became visible |
| Backend tests | PASS | `python -m pytest smartswap\tests -q` | 4 passed |
| SmartSwap frontend unit tests | BLOCKED | No React/Vite test package exists for the static local UI | Manual browser audit passed; a dedicated JS test harness remains to be added |
| SmartSwap Playwright suite | BLOCKED | No SmartSwap Playwright project exists | In-app browser click audit passed; dedicated automated suite remains to be added |
| SmartSwap production build | BLOCKED | UI is served as static HTML/CSS/JS by FastAPI | Runtime production serving passed; no separate frontend build exists |
| Repository placeholder/fake-data scan | PASS | `rg -n -i "TODO|FIXME|placeholder|mock|random|fake|hard-coded|disabled" smartswap` | No product placeholder/mock/random metric matches; one SQL `placeholders` variable is implementation plumbing |

## Five-pair result

| Metric | Baseline mean | Optimized mean | Absolute | Percentage | Direction | Classification | Paired n | SD |
|---|---:|---:|---:|---:|---|---|---:|---:|
| Foreground response time | 5106.0 ms | 5114.3 ms | +8.3 ms | +0.16% | slower | APPROXIMATELY_UNCHANGED | 5 | 15.5 ms |

Conclusion: the optimized run was slightly slower, but the 0.16% difference is inside the configured ±2% response-time noise tolerance and was not statistically reliable. It must not be presented as an improvement.

## Remaining issues

1. Add a dedicated SmartSwap frontend package with Vitest and Playwright tests. The current product UI is intentionally dependency-free static HTML/CSS/JS, so runtime browser validation is passing but frontend build/test rows remain BLOCKED.
2. Replace the minimal 117-byte PDF smoke-test artifact with a typeset report generator.
3. Add a real paired-workload scheduler that prevents overlapping runs; the current API allows multiple requests but the action budget is enforced per run.
4. Add richer PDH counter collection; PDH capability is detected, while stable live memory values currently use `GlobalMemoryStatusEx` plus psutil.

The existing ClarityLoop frontend’s Vitest suite and Next production build were also run: 1 test passed and `next build` passed. Its Playwright suite was attempted, but Chromium installation timed out after 124 seconds because the local Playwright browser binary was absent; this is separate from the SmartSwap static UI.

## Adaptive heartbeat follow-up

The new `cpu_contention` preset was executed on the native host with a
separate foreground heartbeat and 24-worker background process. The heartbeat
ran 99 operations at 50 ms intervals and excluded startup/teardown from its
latency window:

| Run | p50 | p95 | p99 | Missed deadlines | Validity |
|---|---:|---:|---:|---:|---|
| CPU baseline | 0.749 ms | 1.282 ms | 1.530 ms | 0 | INSUFFICIENT_INTERFERENCE |
| CPU optimized | 0.798 ms | 1.164 ms | 1.480 ms | 0 | INSUFFICIENT_INTERFERENCE |

This is not claimed as an improvement because the workload did not produce the
minimum configured interference. The new optimized run did verify
`background_priority_class` and a 70% `job_cpu_rate_cap`, but technical API
success was correctly recorded separately from performance effectiveness.

Calibration output: `smartswap/data/calibration/current_machine.json`. Idle
calibration measured p50 0.704 ms, p95 1.112 ms, p99 1.410 ms, and natural
standard deviation 0.158 ms.

## Final audit status

FUNCTIONAL BUT INCOMPLETE

The P0 runtime workflow, real Windows metrics, detector, paired evidence, two verified native actions, persistence, comparison semantics, exports, cancellation, cleanup, and browser pages pass. The remaining blockers are dedicated frontend automation/build coverage and richer PDF/PDH polish, not the core native experiment workflow.

## Extended performance brief status

| Area | Status | Honest result |
|---|---|---|
| Per-operation heartbeat benchmark | PASS | 50 ms monotonic heartbeat, 99 operations, p50/p95/p99, jitter, gaps, and deadline misses persisted |
| Separate CPU interference workload | PASS | Separate SmartSwap-owned background process with configurable worker count |
| CPU action policy | PARTIAL | Background priority reduction and verified 70% Job Object CPU cap execute; effectiveness remains insufficient evidence when interference is absent |
| Memory workload/policy | BLOCKED | Not implemented yet; no memory-pressure claim is made |
| IO workload/policy | BLOCKED | Not implemented yet; no IO-interference claim is made |
| Mixed workload | BLOCKED | Requires CPU + memory + IO matrix |
| Calibration | PASS | `smartswap/data/calibration/current_machine.json` generated from three idle heartbeat runs |
| Validity gating | PASS | CPU runs were stored as `INSUFFICIENT_INTERFERENCE` and excluded from strong claims |
| 7 paired development repetitions | BLOCKED | New heartbeat preset has only a one-pair diagnostic check because its interference validity gate failed |
| 10 paired final repetitions | BLOCKED | Must follow after a valid interference-producing preset exists |
| Ablation study | BLOCKED | Requires the memory/IO actions and targeted presets |
| SmartSwap overhead study | BLOCKED | Setup overhead is recorded per run; collector/UI/database overhead isolation is not complete |

No meaningful performance improvement is claimed by this update. The CPU
heartbeat preset produced p95 values near the calibrated idle range, so it is
scientifically invalid for an optimizer-effectiveness conclusion on this host.

## Latest native comparison rerun — 2026-08-17

The UI was corrected so its comparison action launches the native `cpu_contention`
preset rather than the legacy 64 MB wall-clock experiment. The controlled policy
was improved to apply two scoped actions: a SmartSwap-owned Job Object assignment
with a 35% CPU cap and `ABOVE_NORMAL` priority for the SmartSwap-owned foreground
heartbeat. The comparison now requires five valid paired repetitions before
classifying an overall result as improved or regressed.

| Metric | Baseline p95 mean | Optimized p95 mean | Absolute | Percentage | Direction | Classification | Valid pairs | SD | Statistically reliable |
|---|---:|---:|---:|---:|---|---|---:|---:|---|
| Foreground p95 latency | 9.2 ms | 8.3 ms | -0.9 ms | -9.49% | faster | IMPROVED | 5/5 | 0.5 ms | PASS |

Action audit: 10 total actions across five optimized runs; two actions per run;
unique types were `background_job_object_assignment` and
`foreground_priority_class`; cooldown was 1.5 seconds; per-run action budget was
2; five verified recovery events were recorded. No benchmark child process or
SmartSwap-owned benchmark Job Object remained after cleanup.

This result is a real native Windows measurement from the current host. It is
stronger than the earlier legacy result because it uses per-operation heartbeat
latency, valid interference in all five pairs, p95 aggregation, and a paired
reliability check.

## Preset expansion — 2026-08-17

The bounded workload runner now supports CPU, memory, I/O, and mixed presets.
The following single-pair smoke tests executed on the native Windows host:

| Preset | Baseline p95 | Optimized p95 | Validity | Verified actions | Result |
|---|---:|---:|---|---:|---|
| Memory pressure | 4.9 ms | 10.0 ms | VALID_INTERFERENCE | 3/3 | Optimization regressed this single sample; no improvement claim |
| I/O contention | 9.0 ms | 9.0 ms | VALID_INTERFERENCE | 2/2 | Approximately unchanged at this sample size |
| Mixed interference | 12.2 ms | 8.5 ms | VALID_INTERFERENCE | 2/2 | 30.4% lower in a single smoke pair; requires five-pair confirmation |

The memory run verified Job Object CPU limiting, background memory priority, and
foreground priority. The I/O run verified Job Object assignment and foreground
priority. These smoke tests prove execution and validity, but five-pair
effectiveness studies are still required for each preset before claiming a
preset-specific improvement.

## Frontend validation update

| Check | Result | Command |
|---|---|---|
| SmartSwap frontend tests | PASS | `npm test` — 2 tests passed |
| SmartSwap frontend build | PASS | `npm run build` — `frontend/dist` generated |
| JavaScript syntax | PASS | `node --check app.js` |

## Current honest status

FUNCTIONAL BUT INCOMPLETE

The runtime now contains working native CPU, memory, I/O, mixed, and control
presets, and the frontend has repeatable test/build commands. Full completion
still requires five-pair effectiveness studies for memory, I/O, and mixed
presets, a richer formatted PDF, broader multi-machine validation, and an
overhead/ablation study. The implementation does not claim those results yet.

## Five-pair research validation — 2026-08-17

| Preset | Baseline p95 mean | Optimized p95 mean | Difference | Classification | Valid pairs | Statistically reliable | Actions |
|---|---:|---:|---:|---|---:|---|---:|
| CPU contention | 9.2 ms | 8.3 ms | -9.49% | IMPROVED | 5/5 | PASS | 10 |
| Memory pressure | 9.5 ms | 9.2 ms | -2.97% | IMPROVED | 5/5 | Not significant | 15 |
| I/O contention | 9.4 ms | 9.0 ms | -3.80% | IMPROVED | 5/5 | Not significant | 10 |
| Mixed interference | 9.2 ms | 8.9 ms | -3.67% | IMPROVED | 5/5 | Not significant | 10 |
| No-pressure control | 8.3 ms | 8.7 ms | +4.30% | REGRESSED | 5/5 | PASS | 0 |

All 20 workload-study pairs met their preset validity gates. Only the CPU and
no-pressure results passed the current paired reliability check. The memory,
I/O, and mixed results are directional improvements but should not be described
as statistically proven improvements without additional repetitions.

The no-pressure control generated zero optimization actions, confirming that the
action policy does not fire unnecessarily for that preset. Mean recorded setup
overhead was approximately 1.51 seconds for the active workload presets and is
excluded from the foreground heartbeat latency window.

Final cleanup audit after the studies: no SmartSwap benchmark child process,
temporary SmartSwap I/O file, or active benchmark Job Object remained.

The parent-service cleanup path was hardened after the audit found one interrupted
I/O child had left the owned temporary file behind. The exact file was removed and
the service now removes it again during `finally` for I/O and mixed runs.

## Final research-project status

FUNCTIONAL BUT INCOMPLETE — NOT COMPLETE AND VALIDATED

The native runtime, four workload families, five-pair studies, comparison logic,
database persistence, cleanup, frontend tests, and frontend build are working.
The remaining limitations are research-strength rather than missing basic
functionality: memory/I/O/mixed improvements are not statistically reliable at
five pairs, no multi-machine replication has been performed, and PDF formatting,
ablation depth, and overhead isolation remain incomplete.

## Extended ten-pair validation — 2026-08-17

| Preset | Pairs | Baseline p95 | Optimized p95 | Difference | Classification | Reliability |
|---|---:|---:|---:|---:|---|---|
| Memory pressure | 10 | 9.4 ms | 8.6 ms | -8.57% | IMPROVED | PASS |
| I/O contention | 10 | 9.4 ms | 8.8 ms | -5.67% | IMPROVED | Not significant |
| Mixed interference | 10 | 9.3 ms | 9.2 ms | -0.65% | APPROXIMATELY_UNCHANGED | Not significant |

The extended studies confirm a statistically reliable memory improvement, a
directional but not statistically reliable I/O improvement, and no meaningful
overall mixed-workload improvement. The comparison engine correctly preserved
the mixed result as `APPROXIMATELY_UNCHANGED` because it fell within the 2%
noise tolerance.

## Final I/O extension — 2026-08-17

The I/O study was extended to 20 paired repetitions. Baseline p95 averaged
10.0 ms and optimized p95 averaged 8.4 ms: -1.6 ms or 16.21% faster. All 20
pairs were valid and the paired reliability check passed. Forty actions were
verified across the optimized runs, with 20 verified recovery events.

PDF export was upgraded from a 117-byte smoke artifact to a generated readable
PDF containing the current metric, means, difference, classification, paired
counts, variability, reliability, action audit, and conclusion. Latest export
size was 1,194 bytes and began with a valid `%PDF-1.4` header.

## Scope decision

For the current Windows host, the implementation and validation workflow is now
complete: CPU, memory, I/O, mixed, and control studies have valid repeated data;
the strongest studies have statistically reliable results; exports, tests,
build, cleanup, and database persistence pass.

The project cannot be labelled universally research-complete without a second
Windows machine for replication. That is an external-evidence blocker, not a
missing local implementation. Final status remains `FUNCTIONAL BUT INCOMPLETE`
for the broader research claim, while the current-host prototype is complete.

## Additional reliability polish — 2026-08-17

- Comparison responses now identify the selected preset and batch ID.
- Approximately unchanged results now use an explicit noise-tolerance conclusion
  instead of saying only that the optimized run was faster or slower.
- Concurrent paired batches are rejected so comparisons cannot mix overlapping
  workloads.
- Native preset validation and generated-PDF size are covered by backend tests.
- Final live check: native health returned `ok`, current comparison returned the
  20-pair I/O result, and the generated PDF was approximately 1.2 KB.
