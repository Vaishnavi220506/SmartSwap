# SmartSwap v3: paper plan

## Thesis

Windows tells software how busy the machine is, not how much time interactive work is losing, or to whom. SmartSwap measures that loss from user mode, without privileges. It then names the contended resource *and the application responsible*. It proves the blame with a **randomized controlled micro-experiment run on the live desktop**, and it applies the cheapest verified fix.

The headline idea is to treat interference attribution as a causal-inference problem and to answer it by experiment, not by correlation. Randomized assignment makes the verdict immune to load that drifts during the test. A factorial design tests several suspects at once. And the estimated effect is a per-application "interference bill" in milliseconds per second.

## Contributions (what each one claims, and the evidence that must back it)

| # | Contribution | Novel because | Evidence (design → metric) | Pre-registered |
|---|---|---|---|---|
| C1 | **WSI**: a PSI-style stall index for Windows, measured by a low-duty canary with no admin rights, drivers, ETW or hardware counters | Linux has PSI; Windows has no equivalent, privileged or not | `main`, `realapp`: Spearman ρ between WSI and a *real app's* UI latency (Notepad `WM_NULL` round trip); ROC AUC; false alarms; canary CPU share | H2 |
| C2 | **Differential attribution** from the canary's own timing components (most specific symptom first) | No hardware counters needed | `main` (observe-only): per-period accuracy for CPU, memory bandwidth and storage | n/a (v2 result, replicated per host) |
| C3 | **Causal culprit blame by randomized factorial micro-experiments.** Passive ranking (share of matched usage while stalled × (1 + Spearman ρ)) picks up to 3 suspects. Each is then nudged in a random, balanced half of 8 short slots (about 5 s), independently of the others. The difference in mean WSI estimates each suspect's causal effect, and a permutation test gives its p-value. The effect also gives a per-app interference bill in ms/s | Task Manager ranks by usage. Interference work (Bubble-Up, CPI², Heracles, PARTIES) is datacenter-side and needs counters or cooperating apps. Here, blame is a *randomized experiment on an unmodified desktop*: it is robust to load drift, unlike one-at-a-time tests (shown in simulation, `research/simulate_blame.py`), and it is parallel over suspects | `blame`: precision@1 of the *kept* blame, probe nudges spent, time to culprit, p95 vs. oracle. 4 ground-truth scenarios: one where the top-CPU app is innocent, an IDLE-priority "polite hog", and a phased culprit that drifts mid-test. Compared with top-CPU, passive, and one-at-a-time intervention. Plus the drift simulation | H1 |
| C4 | **Resource-matched, benefit-checked ladder**: priority → E-core steering → EcoQoS → AIMD cap, each rung rolled back if the canary shows no benefit | It compares against, and builds on, the OS's own Efficiency mode | `ecoqos`, `hybrid`, `real`: p95 ratios vs. Efficiency mode, vs. static priority, and vs. none, plus background work kept | H3, H4, H5 |
| C6 | **Cross-OS validation against a kernel oracle.** The same unprivileged canary, controller and blame code run on Linux (`backend/posix.py`, `research/linux_study.py`), where PSI is ground truth | Windows has no stall oracle, so this is the only way to check the canary against kernel accounting. Kept to one table plus the blame replication | Linux `validity`: per-period ρ(WSI, PSI); AUC of each; which better predicts app p95. Linux `blame` and `mitigation` | secondary |
| C5 | **Verified control** (every action and revert is read back from the kernel) and a methodology for checking that injected interference is real (v1 audit) | Most systems papers assume their actuators worked; `docs/AUDIT_V1.md` shows why that fails | Ledger totals in every design | n/a |

## Protocol

1. Commit `research/hypotheses.json` **before** collecting v3 data. That commit is the pre-registration.
2. Run `scripts/run-v3-studies.ps1` on **at least 3 different machines**, ideally:
   - one Intel hybrid laptop (P- and E-cores);
   - one AMD or homogeneous desktop;
   - one low-end, 4-core, SATA or HDD machine.

   Record AC power, the power plan, and the Windows build (the manifest captures them).
3. `python -m research.pool <all study dirs>` → `POOLED.md`. Report H1–H5 exactly as they come out, including any that fail.
4. Secondary results (per-design `RESULTS.md`) are exploratory, with Holm within each design.
5. `blocks_for_80pct_power` in each comparison says whether a null result is a real null or underpowered.

## Threats to validity (write these into the paper)

- **Intervention confounding**: if load changes during a test window, blame can be wrong. Mitigations: one suspect at a time, an 8-period window, retesting while the stall persists, exoneration cooldown. Measured by: wrong nudges, and verdict accuracy against ground truth.
- **Memory-bandwidth blame uses CPU time as a proxy** (bandwidth is not observable per process in user mode). `membw_polite` tests the case where this proxy misleads.
- **App trees stop at shells and terminals**, so a build started from VS Code's terminal groups under the shell, not under VS Code. A browser's helper processes group under the browser.
- **The foreground metric** (a `WM_NULL` round trip) measures message-pump responsiveness, not rendering. Frame-time metrics for games are future work.
- **Real tools' "work done"** is the CPU time of their process tree, not task completion.
- **Safety**: on the live desktop, governing is opt-in, lowering-only, protected-list, foreground-exempt and journaled. Stress-test crash recovery (kill SmartSwap mid-nudge, restart, check that priorities are restored).

## Candidate venues

| Venue | Fits |
|---|---|
| EuroSys, USENIX ATC | If H1–H4 hold across 3 or more hosts |
| SYSTOR, Middleware, APSys | With 2–3 hosts |
| HotOS or a EuroSys workshop | WSI plus interventional blame as a short vision paper, even from pilot data |

## Paper outline (12 pages)

1. Introduction: a trace showing Task Manager blaming the wrong app while Notepad stutters
2. Background: PSI; Windows scheduling, Job Objects, CPU sets, EcoQoS
3. WSI (C1) and attribution (C2)
4. Interventional blame (C3)
5. The ladder and verified control (C4, C5)
6. Methodology: block design, interference validation, v1 pitfalls, pre-registration
7. Evaluation: H1–H5 pooled across hosts; ablations; overhead; power
8. Discussion, limitations, threats
9. Related work: PSI/oomd/senpai; Bubble-Up/Bubble-Flux; CPI²; Heracles; PARTIES; Caladan; EcoQoS; desktop QoS
10. Conclusion
