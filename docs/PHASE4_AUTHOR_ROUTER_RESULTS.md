# Author-derived Phase 4: validation results

Results recorded on 2026-10-21. Phase 4 uses the supplied authors' connection
ordering and shared-tree maze search; Phase 5 retains EmuFlow's existing TDM
optimizer. The current default is `synergistic-author-route-v1` with
`author-pressure-v1` and `hard-connections-v1`. The previous router is
`timing-aware-global-candidate-v1`.

## Public contest cases

These are complete, unmodified EDA2023 communication inputs from the
[contest catalog](../benchmarks/contest_validation_matrix.json). They test
routing, TDM and independent solution validation, not RTL physical timing.
Each reported completed arm passed validation. Old and author arms use the
same inputs and Phase 5 settings: 100 optimizer iterations, 2000 refinement
iterations and exact-domain limit 2048. Maximum routing weight is the largest
sink-path sum of SLL delay and `ratio + 0.5` per wire hop; lower is better.

| Case | FPGAs / dies | Total nets | Old router weight | Author pressure weight | Optional SLL scarcity weight | Finding |
|---|---:|---:|---:|---:|---:|---|
| case1 | 2 / 8 | 5 | 6.5 | 6.5 | — | Equal |
| case2 | 2 / 8 | 86 | 7.5 | 7.5 | — | Equal |
| case3 | 2 / 8 | 84 | 11.5 | 11.5 | — | Equal after cost-model repair |
| case4 | 2 / 8 | 449 | 18.5 | 18.5 | — | Equal |
| case5 | 3 / 12 | 5,083 | 130.5 | 130.5 | — | Equal |
| case6 | 3 / 12 | 145,660 | 165.0 | 165.5 | 170.0 | Default slightly worse; scarcity also regresses |
| case7 | 4 / 16 | 76,258 | 74.5 | 70.5 | 71.5 | Default improves; scarcity loses part of the gain |
| case8 | 4 / 16 | 86,139 | 105.5 | 108.5 | 102.5 | Default regresses; scarcity improves |
| case9 | 4 / 16 | 871,588 | 138.0 | 127.0 | 126.5 | Both author policies improve |
| case10 | 5 / 20 | 3,324,963 | — | 4331.5 | — | Author-only scalability pass; no old-router comparison |

An em dash means no completed measurement for that arm. Case3 initially scored
14.5 when the adapter used quantized ratio buckets as routing costs. Continuous
pre-insertion pressure restored 11.5; feasibility still respects quantization.
The mixed case6–9 results reject unconditional SLL scarcity as the default.

Selected Phase 4 runtimes, in seconds:

| Case | Old router | Author pressure |
|---|---:|---:|
| case6 | 226.27 | 44.53 |
| case7 | 75.33 | 22.18 |
| case8 | 84.21 | 26.51 |
| case9 | 2029.56 | 258.69 |

Case9's observed Phase 4 speedup is 7.85x. Phase 5 took 169.98s / 212.42s
(old / author), so it became slower. Total arm time including independent
evaluation was 2271.19s / 538.59s; sampled process-tree RSS peaks were
14.49 / 7.05 GiB. These are single-run observations with some overlapping
workloads, not controlled hardware speedup guarantees. Earlier short-budget
timeouts are excluded from the completed-arm comparison.

Case10 routed all 7,278,805 cross-die connections. Phase 4, Phase 5 and
independent evaluation took 1442.44s, 1681.31s and 389.67s respectively;
supervised arm time was 3515.86s, with a sampled RSS peak of 43.29 GiB.
Its final maximum TDM ratio was 4328. This is a legality/scalability result,
without an old-router quality comparison or physical-timing claim.

## Complete RTL flow

Intact upstream PicoRV32 (`87c89acc18994c8cf9a2311e871818e87d304568`), using
the [registered run spec](../benchmarks/runs/picorv32_l2.json), was compiled
through physical implementation and global OpenSTA validation on four academic
VTR FPGAs. The 32x32 comparison fixes logical inputs, assignment, topology,
timing population, backend settings and physical seed 1.

| Configuration | Target WNS (ns) | Target TNS (ns) | Runtime TNS (ns) | Result |
|---|---:|---:|---:|---|
| 32x32, old router | -954.222 | -213643.518 | 0 | Complete flow passes; target timing unmet |
| 32x32, author pressure / default API | -954.083 | -215423.380 | 0 | Complete flow passes; target TNS 0.833% worse |
| 32x32, author pressure + STA incidence ordering | -954.215 | -214532.174 | 0 | Recovers 50.1% of the TNS gap; still 0.416% worse than old |
| Fixed 17x17, author default | -953.802 | -215187.312 | 0 | Complete flow passes at 73.94–78.18% CLB site utilization per FPGA |

The source target remains 10ns and is unmet; the declared virtual runtime
period is 1280ns and closes. The fixed 17x17 devices were materialized before
partitioning; this is a separate geometry qualification, not a routing A/B.
Overall resource utilization is not inferred from CLB site utilization.

Each complete Pico arm retains 896 original timing paths, 1123 physical
realizations and 428 output copies. There are 1181 naturally selected cut nets,
including 516 combinational cuts. Equivalence mismatches, DRC violations,
unrouted nets and transport-event failures are all zero. Output copies are
independently checked and original-path TNS is counted once. Board links use
model delays; these results are not vendor or hardware signoff.

## Revisions and integration checks

Contest measurements span `4e241c79` through `faedba50`, including the
continuous-cost repair in `5565d803`. Completed unchanged arms were reused
after identity checks of the relevant inputs, tools and Phase 5. Case9 and
case10 use `faedba50`, whose author-router and TDM native sources are byte-identical to
`967aff14`. Current default/API and compact-JSON changes were separately
validated, not attributed retroactively to those case runtimes.

The old-router and author-policy Pico comparison started at `34074ba2`;
incidence ordering was tested at `c4fd0ae5`. Fresh default32 and dense17 runs
use `967aff14`. That functional revision passed 253 targeted tests with no
skips, an author-only build without the legacy router, and independent reading
of old Author/GLOBAL complete-flow artifacts. The reusable contest comparison
entry point is [phase4_author_compare.py](../scripts/phase4_author_compare.py).
