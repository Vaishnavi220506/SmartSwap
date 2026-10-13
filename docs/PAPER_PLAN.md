# CausalShield: paper plan

**CausalShield: privilege-free causal interference diagnosis and minimal intervention for background work on Windows**

## Research question

Not "is my PC experiencing interference, and can I reduce it?" (SmartSwap v2), but:

> **Which background job is actually slowing my application, how can we prove it, and what is the smallest intervention that fixes it?**

SmartSwap becomes the sensing and actuation substrate: a canary stall index, attribution, and kernel-verified controls. CausalShield is the research contribution built on top of it.

| Layer | Module | Role |
|---|---|---|
| Detect | `controller.py` (SmartSwap) | WSI, engagement, contended resource (with learned fingerprints) |
| Blame | `blame.py` | Randomized sequential factorial micro-experiments confirm culprits causally |
| Remedy | `remedy.py` | Cheapest resource-matched action expected to meet the SLO, learned per action |
| Safety | `remedy.py`, `govern.py` | Lowering-only actions, revert if worse, safe static fallback when unexplained |
| Orchestration | `shield.py` | The pure decision loop, shared by every harness |

Boundary: CausalShield manages only **opted-in jobs** (launched through it, or explicitly allowlisted). It does not claim to safely throttle arbitrary Windows processes.

## Contributions

| # | Contribution | Novel because | Evidence | Pre-registered |
|---|---|---|---|---|
| A | **Intervention-based culprit identification**: short, randomized, balanced, reversible nudges of the top-k suspects (a 2^k factorial design). Each suspect's effect is a difference of means with a permutation p-value. Inconclusive suspects get up to 3 pooled rounds, with α split across looks. The effect is reported as an interference bill (ms/s) | Usage-based tools (Task Manager) and the datacenter interference literature (Paragon, Bubble-Up, CPI², Heracles) do not *experimentally* establish per-job causal effects on an unmodified desktop under drifting load | `blame` design (precision of kept blame vs. top-CPU, passive, one-at-a-time); drift simulation | H1 |
| B | **Interference-aware minimal intervention**: per-culprit, resource-matched ladder (priority → EcoQoS → caps at 50/25/10%). Benefit and cost are learned per (resource, action), the cheapest action expected to close the SLO gap is chosen, and it relaxes when load drops | Global ladders (SmartSwap v2, Efficiency mode) apply one policy to all background work. Here only the confirmed culprit pays, and only as much as needed | `tradeoff` design: innocent work kept and culprit work kept at a given foreground p95 | H6, H7 |
| C | **Uncertainty-aware safety**: nothing stronger than a priority hint without a confirmed culprit; revert any action that makes WSI worse; static fallback when the stall stays unexplained | Makes causal testing safe to run on a user's desktop | Ledger (all actions and reverts verified), fallback episodes, revert-on-worse events | n/a |
| S | *Substrate (SmartSwap)*: WSI canary, self-calibrating fingerprint attribution, kernel-verified actuators | Required for A–C; WSI validity is reported, not claimed as the novelty | `main`, `realapp`, `attribution`, `ecoqos` | H2–H5 |

## The go/no-go experiment (`--design tradeoff`)

One culprit job and two innocent jobs, all managed, in 4 scenarios (CPU, storage-vs-CPU, polite IDLE hog, phased culprit). Foreground latency comes from an independent probe process, never from WSI.

| Policy | Protects the foreground? | Keeps innocent work? |
|---|---|---|
| None | baseline | baseline |
| Lower every job (static priority) | measured | measured |
| Efficiency mode on every job | measured | measured |
| SmartSwap v2 ladder on every job | measured | measured |
| **CausalShield** | measured | measured |

**The paper succeeds only if** CausalShield keeps significantly more innocent work than lowering every job (H6) while its foreground p95 is non-inferior within 10% (H7). Beating "no intervention" is not enough. If H6 or H7 fails, report that honestly, or change the question.

Scope: the paper is **Windows-only**. A Linux harness exists in the repo (`backend/posix.py`, `research/linux_study.py`) purely as a development sanity check of the code paths. It is not cited as evidence.

## Design history: the first go/no-go failed (internal check, 4 blocks, not paper evidence)

The original CausalShield acted only on *proven* culprits ("guilty until proven"). On the internal check it failed H7 badly:

| Scenario | Lower every job: p95 vs. none / innocent work kept | Proof-first CausalShield: p95 vs. none / innocent work kept |
|---|---|---|
| CPU | 0.39 / 0.84 | 0.88 / 1.01 |
| Storage vs. CPU | 0.57 / 0.95 | 0.71 / 0.97 |
| Polite hog | 0.73 / 0.99 | 0.94 / 1.01 |
| Phased culprit | 0.40 / 0.86 | 0.98 / 1.10 |

It kept innocent work (the H6 direction), but it protected the foreground 1.2–2.4× worse than lowering everyone. There were two causes:
1. No protection while the proof was gathered.
2. Suspects came from a passive ranking that trusted a mislabelled stall, so the culprit was often never tested.

Two further findings matter for the claims:
- Lowering every job's **priority** cost innocent jobs only 1–16% of their work. Large innocent losses came from **caps** (the v2 ladder kept only 28–81%).
- So H6 against priority-lowering has little headroom by construction, while against cap-based policies it has a lot.

The redesign, **presumed guilty**, inverts the burden of proof:
- every managed job is lowered the moment a stall starts;
- the randomized experiments become *release* tests;
- a job whose release brings the stall back stays lowered, and a job whose release changes nothing is restored;
- an inconclusive job stays lowered (the safe side);
- every managed job is a candidate, so a mislabelled stall can no longer hide the culprit.

### Third iteration: label-free presumed guilty is the first GO signal (internal check, 4 blocks, not paper evidence)

The second run of presumed guilty still failed. Its logs showed the CPU culprit was almost never tested: candidates had to use the *labelled* resource, and on that VM CPU stalls were labelled "storage". The fix is **label-free blame**:
- lower both CPU and I/O priority;
- consider every active managed job;
- let the randomized release test establish causation.

| Scenario | p95 after verdicts (CausalShield ÷ lower everyone) | Innocent work kept (difference) |
|---|---:|---:|
| CPU | 1.07 [1.00, 1.36] | +0.39 [0.30, 0.43] |
| Storage vs. CPU | 0.91 [0.77, 1.04] | +0.05 |
| Phased culprit | 1.00 [0.91, 1.34] | +0.19 [0.04, 0.25] |
| Polite hog | 1.17 [0.52, 1.71] | +0.14 |

- Culprits were never cleared: 11 confirmed, 4 held. All 11 confirmations hit the real culprit.
- Open problems: (1) time to the last verdict is about 18 s, so whole-window p95 is still 1.25–1.86× worse than lowering everyone; (2) innocent jobs that stay inconclusive are held lowered (11 cases).
- 4 blocks are underpowered (Holm p = 1.0 everywhere). The Windows `tradeoff` study, with 10 blocks on 3+ hosts, is the real test of H6 and H7.

## Protocol

1. Commit `research/hypotheses.json` **before** collecting v3 data. That commit is the pre-registration.
2. Run `scripts/run-v3-studies.ps1` on **at least 3 different machines**, ideally:
   - one Intel hybrid laptop (P- and E-cores);
   - one AMD or homogeneous desktop;
   - one low-end, 4-core, SATA or HDD machine.

   Record AC power, the power plan, and the Windows build (the manifest captures them).
3. `python -m research.pool <all study dirs>` → `POOLED.md`. Report H1–H7 exactly as they come out, including any that fail.
4. Secondary results (per-design `RESULTS.md`) are exploratory, with Holm within each design.
5. `blocks_for_80pct_power` in each comparison says whether a null result is a real null or underpowered.

## Method-level evidence already in hand (simulation, OS-independent)

`python -m research.simulate_blame` drives the real decision code against a toy desktop where background load steps during the first test. Settings: 300 runs per cell, culprit effect 6 WSI points, noise sd 1, innocent app ranked first, 90 control periods for both methods.

| Load drift during test | One-at-a-time: innocent blamed | Randomized sequential factorial: innocent blamed | Factorial: culprit missed |
|---:|---:|---:|---:|
| -8 | 51.7% | 2.0% | 3.3% |
| -6 | 44.7% | 2.0% | 0.7% |
| -4 | 33.7% | 3.3% | 0.0% |
| 0 | 0.0% | 3.7% | 0.0% |
| +4 | 0.0% | 3.0% | 0.0% |

Randomization holds false blame near the test's 5% level whatever the drift. Sequential testing does not. Adding sequential rounds (up to 3 pooled rounds, α split across looks) removed the earlier loss of power: misses under the largest drop fell from 49.7% (single round) to 3.3%. The one remaining cost is time, since an inconclusive suspect needs about 5 s per extra round.

## Threats to validity (write these into the paper)

- **Blame inherits attribution errors.** In the internal Linux check, the ordered rule labelled CPU contention as storage about 90% of the time. Every blame method that trusts the label then tested the wrong suspects with the wrong lever, and only the label-free top-CPU heuristic succeeded. Fingerprinting (C2) is the fix, and the Windows `attribution` design checks it per host.
- **Blame overhead.** A full process snapshot every 200 ms cost about 17% of one core in the internal check. Snapshots now run at 1 Hz while calm and at full rate only during an engaged stall. Report the measured `governor_cpu_share` per host.

- **Intervention confounding**: if load changes during a test window, blame can be wrong. Mitigations: one suspect at a time, an 8-period window, retesting while the stall persists, exoneration cooldown. Measured by: wrong nudges, and verdict accuracy against ground truth.
- **Memory-bandwidth blame uses CPU time as a proxy** (bandwidth is not observable per process in user mode). `membw_polite` tests the case where this proxy misleads.
- **App trees stop at shells and terminals**, so a build started from VS Code's terminal groups under the shell, not under VS Code. A browser's helper processes group under the browser.
- **The foreground metric** (a `WM_NULL` round trip) measures message-pump responsiveness, not rendering. Frame-time metrics for games are future work.
- **Real tools' "work done"** is the CPU time of their process tree, not task completion.
- **Safety**: on the live desktop, governing is opt-in, lowering-only, protected-list, foreground-exempt and journaled. Stress-test crash recovery (kill SmartSwap mid-nudge, restart, check that priorities are restored).

## Candidate venues

| Venue | Fits |
|---|---|
| EuroSys, USENIX ATC | If H1, H6 and H7 hold across 3 or more hosts |
| SYSTOR, Middleware, APSys | With 2–3 hosts |
| HotOS or a EuroSys workshop | WSI plus interventional blame as a short vision paper, even from pilot data |

## Paper outline (12 pages)

1. Introduction: the top-CPU job is not the job hurting you; lowering everything wastes innocent work
2. Background: Windows scheduling, Job Objects, EcoQoS / Efficiency mode, PSI, datacenter interference work
3. Substrate (S): WSI canary, fingerprint attribution, verified actuators (brief)
4. Causal blame (A): randomized sequential factorial micro-experiments; the drift analysis
5. Minimal intervention (B) and safety (C)
6. Methodology: block design, independent foreground, pre-registration (H1–H7), v1 audit
7. Evaluation: go/no-go trade-off (H6, H7); blame accuracy (H1); substrate validity (H2–H5); overhead; power
8. Discussion, limitations, threats
9. Related work: PSI/oomd/senpai; Bubble-Up/Bubble-Flux; CPI²; Heracles; PARTIES; Caladan; EcoQoS; desktop QoS
10. Conclusion
