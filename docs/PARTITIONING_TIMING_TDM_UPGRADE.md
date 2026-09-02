# Timing- and TDM-aware partitioning upgrade

## Goal and promotion gate

This work upgrades Phase 3 without replacing the independently checked Phase
4--7 contracts.  The new route is promoted only when a frozen, real-RTL
comparison completes Phase 7 and improves **both** complete-global WNS and TNS
over the current TritonPart-based flow.  Phase 3 cut, hop, or predicted-delay
metrics are diagnostics, not acceptance evidence.

The primary comparison is Koios DLA medium on the EDA 2023 case 6 topology,
physical seed 1.  It reuses one validated Phase 1--2 ancestor and otherwise
holds RTL, constraints, BoardDB, TimingPathDB, architecture, tools, Phase 4--7
options, and physical seed fixed.  Case 7 and case 9 are replication points
after the primary gate passes.  Every arm is a content-addressed experiment
node, so unchanged ancestors and completed baseline nodes are reused rather
than rerun.

## Primary complete-flow acceptance result

The primary gate passed with source commit
`21757abadf8ebfe735a129927bc8dda532e097f8`.  This is the complete-global
Phase 7C result, not per-FPGA physical timing or a Phase 3--5 proxy:

| metric | frozen existing flow | PATRON | candidate - baseline | deficit reduction |
|---|---:|---:|---:|---:|
| target-clock WNS (ns) | -95.310052262 | -82.4981025395 | +12.8119497225 | 13.4424% |
| target-clock TNS (ns) | -499,996.7265938718 | -324,776.89798473305 | +175,219.82860913873 | 35.0442% |
| negative paths | 7,965 | 8,803 | +838 | regression (diagnostic) |
| per-FPGA physical WNS (ns) | 10.8935 | 12.4171 | +1.5236 | diagnostic only |
| per-FPGA physical TNS (ns) | 0.0 | 0.0 | 0.0 | diagnostic only |
| unrouted nets / DRC violations | 0 / 0 | 0 / 0 | unchanged | required closure |

Both arms use the same EmuIR digest
`ba852c74929b00b1c7844827bdd55bedecc348ae1b670ee63a3d979aee8d666f`,
Chimew Phase 6 manifest, physical seed 1, eight workers, route channel width
300, and the same routed-staging-chain-plus-link/TDM timing qualification.
Each independently covers all 195,532 original TimingPathDB paths exactly once
with 100% coverage.  The partition, route, and effective schedule digests are
different by design: those are outputs of the variable under test, not frozen
Phase 6-provider controls.

The compact acceptance certificate has SHA-256
`6f4583a8cec071fe3d26886b3780e2e11c4918e1fee4040c1a7e5f9be36f7f52`;
its independently replayed evidence manifest has SHA-256
`9b485fce18b97774998dfa4509900e7215eb4464b6e08fc4b522fd5fb6af16d7`.
The evidence bundle contains the baseline and candidate Phase 7 checkpoint
reports, runtime QoR, physical-summary, and physical-flow reports plus their complete
file digest table; transient server paths and physical scratch are not part of
the claim.

This satisfies the branch's primary acceptance criterion because both WNS and
TNS improve and both physical arms close legally.  It does not claim every
secondary metric improves: the negative-path count increases even while total
negative slack decreases substantially.  Case7/case9 topology replication
remains the gate before changing the repository-wide default provider.

## Controlled four-arm baseline-provider diagnostic

The previously pending v6, v9, v6-to-v11, and v9-to-v11 diagnostic completed
the full Phase 7 path on `koios-dla-medium-l5__eda2023-case6`.  It deliberately
uses the baseline Phase 6 provider, physical seed 1, one physical worker, and
route channel width 300.  This is a controlled algorithm diagnostic, not the
canonical baseline/placement-aware/Chimew provider matrix and not a replacement
for the Chimew/eight-worker acceptance result above.

| arm | target WNS (ns) | target TNS (ns) | negative paths | per-FPGA physical WNS (ns) | unrouted / DRC |
|---|---:|---:|---:|---:|---:|
| v6 | -95.390825202 | -264,633.1135519021 | 6,271 | 7.02073 | 0 / 0 |
| v9 | -95.390825202 | -264,633.1135519021 | 6,271 | 7.02073 | 0 / 0 |
| v6 to v11 | -97.289380855 | -260,656.0599069793 | 6,051 | 10.1567 | 0 / 0 |
| v9 to v11 | -97.289380855 | -260,656.0599069793 | 6,051 | 10.1567 | 0 / 0 |

All four arms independently cover all 195,532 original TimingPathDB paths,
with no fallback or discontinuous compressed paths.  They use EmuIR digest
`cb6c74059d40279442b5c1c2f272fea6ad5ea7a1e7c1084f9bfe402ebcae5e99`
and original path-ID digest
`a2fb7551b567957f38fe4159605886bee5ee60d861875cd1cdc8a2fc215bba8b`.
The cold arms ran from source commit
`512ad2f1443a9cd2d0d27234f063d32a2cd29e59`; the feedback arms ran from
`2e986c9ee056f7fbb1129f86e45dc49d6dc9e2a6`.

The cold assignments converge to one physical result, and both feedback
descendants converge to another.  Relative to either parent, v11 improves TNS
by `3,977.053644922824 ns` (1.5029% deficit reduction) and removes 220 negative
paths, but worsens WNS by `1.8985556530000025 ns` (1.9903% deficit increase).
It therefore fails the declared dual-metric promotion gate under this
configuration.  This qualified negative result does not contradict the
Chimew/eight-worker gate because the Phase 6 provider and physical worker
contract differ.

The independently validated compact Phase 7 report and evidence-manifest
identities are:

| arm | Phase 7 report SHA-256 | evidence manifest SHA-256 |
|---|---|---|
| v6 | `6d1fa34fd2d17c104d028d3a80ea006b0b8c89aad00e490bf9a1c12cb1292465` | `db4208c78f452f6cdbf815485b913ef7d1686328a7c8cf96a929b1a4a49dcc32` |
| v9 | `7e27d4c9511dd2973f4c8f41365a7406db421a4b9f794c12a79eb43fc67cae4e` | `260025a1308a5e6fcdec48b880bd2aab6222435711e2d113eefdac6072381d0a` |
| v6 to v11 | `f24036bac8952afbd41942640a71298f80294cea3afe0c0337a0a89e1ba6d9f4` | `1917a6c0c3aa4dd57f6f754ea66880cda2ee96e12217b6bf5d589ab710d07324` |
| v9 to v11 | `017981d498e18e0dfe0d443fd9466cf5a7b7051cf73b35ea952d49af7f9f91ac` | `9ca78aa4deb7ca90e7b6a29ee684e9c9db14834cca3d26810e5fafcc136edda1` |

The two complete feedback archives remain outside the source repository.  The
v6-to-v11 archive contains 343,798,596 bytes and has SHA-256
`fb69a0bdcff9f18124be0717e5e84ebc37b327ba1d6e2b17c4c05d73b901c8aa`;
the v9-to-v11 archive contains 343,808,333 bytes and has SHA-256
`f198dd971effad98ea7dfcb1f96cf87062b337d4898b857500351399cac32ed1`.
The large routed outputs, experiment manifests, and machine-readable reports
remain external evidence rather than source-tree inputs.

## What the literature actually optimizes

There is no single universal multi-FPGA partitioning SOTA.  Recent work leads
on different objectives, which is why this design treats them as complementary
signals instead of declaring one paper a universal winner.

| Work | Principal contribution | Relevant lesson for EmuFlow |
|---|---|---|
| [TritonPart, ICCAD 2023](https://vlsicad.ucsd.edu/Publications/Conferences/401/c401.pdf) and its [official source](https://github.com/ABKGroup/TritonPart) | Open-source multilevel, multi-constraint hypergraph partitioning; its netlist mode adds critical-path slack propagation, cut penalties, and path-snaking cost | Preserve its strong multilevel candidate, but do not reduce ordered paths to one scalar weight per net |
| TopoPart, ICCAD 2021 and MaPart, TCAD 2024 ([DOI](https://doi.org/10.1109/TCAD.2024.3392758)) | Partitioning against non-fully-connected multi-FPGA topologies | Reachability and hop limits belong inside candidate construction/refinement, not only in a later repair |
| [EasyPart, ICCAD 2024](https://numbda.cs.tsinghua.edu.cn/papers/iccad24.pdf) | Interconnection-aware coarsening/refinement plus FPGA remapping under multi-resource constraints | Pair/domain pressure and remapping must influence the move target, not just a target-independent net weight |
| [MFSPart, TCAD 2026](https://zhiyaoxie.com/files/TCAD26_MFSPart.pdf) | Generalized multilevel framework for driver-sink cut, connectivity, mean-hop, and low-hop constraints; ensemble cut-overlay recombination | Reuse the source-complete EmuFlow provider and consider multiple structurally different candidates |
| [RePart](https://arxiv.org/abs/2604.00780) and its [official source](https://github.com/Welement-zyf/RePart) | Topology-aware multilevel partitioning with constrained logic replication | Keep replication as an explicit candidate whose area and legality remain independently checked |
| HoPart, DATE 2026 ([paper](https://past.date-conference.com/proceedings-archive/2026/DATA/679.pdf)) | Hop-constrained timing-driven refinement with congestion-aware routing; optimizes routed path delay | Use path delay and congestion pressure directly, but validate predicted routing against actual Phase 4/5 results |
| [Integrated partitioning and TDM optimization, ASP-DAC 2023](https://www.aspdac.com/aspdac2023/archive/pdf/6D-1.pdf) | Reduces maximum pair cut and then the required TDM ratio | The maximum directed link/domain load is more predictive than total cut alone |
| [Timing-driven TDM-aware partitioning, ISPD 2020](https://ispd.cc/ispd2026/slides/2020/Timing_Driven_Partition_for_Multi_FPGA_Systems_with_TDM_Awareness.pdf) | Couples timing paths and TDM demand rather than optimizing cut size alone | Timing criticality must multiply concrete transport pressure and preserve path order |
| [Network Flow-Based Refinement, SEA 2018](https://arxiv.org/abs/1802.03587) and [Parallel Flow-Based Hypergraph Partitioning, 2022](https://arxiv.org/abs/2201.01556) | Builds localized flow problems on block pairs; the parallel version scales the strongest iterative refinement to hypergraphs with up to a billion pins | A capacity-local minimum of single-vertex FM should be crossed by a bounded block-pair transaction, not by accepting an illegal intermediate move |
| [Deterministic Parallel High-Quality Hypergraph Partitioning, 2025](https://arxiv.org/abs/2504.12013) | Makes Jet and flow refinement deterministic while retaining state-of-the-art quality | Reproducible VLSI refinement can use strong pair/block neighborhoods without sacrificing deterministic evidence |
| [FPGAPart, FPGA 2025](https://vlsicad.ucsd.edu/Publications/Conferences/415/c415.pdf) | Combines FPGA pre-packing, timing-path pattern mining, and neighborhood-influence ILP cuts; reports routed Fmax and wirelength gains | Preserve FPGA grouping and final routed timing as the authority; a cut-only objective is insufficient |
| [SHyPar](https://arxiv.org/abs/2410.10875), [K-SpecPart](https://arxiv.org/abs/2305.06167), and [MedPart](https://research.nvidia.com/publication/2024-03_medpart-multi-level-evolutionary-differentiable-hypergraph-partitioner) | Spectral/flow, preconditioned spectral, and evolutionary differentiable generic hypergraph search | They are useful portfolio generators, but do not by themselves close multi-resource, topology, TDM, or final physical timing |

The cited results are motivation and comparison points.  EmuFlow does not
claim faithful reproduction unless the corresponding provider documentation
explicitly says so.

## Current implementation and the missing information

The current default is a serious baseline, not a trivial greedy partitioner:

1. OpenROAD/TritonPart performs deterministic multi-constraint multilevel
   hypergraph partitioning and seed selection.
2. OpenSTA path criticality produces a power-law per-net weight.
3. Every provider assignment is checked and refined for BoardDB reachability,
   maximum hops, multi-resource capacity, fixed/group constraints, and minimum
   used FPGA count.
4. Optional Phase 3--5 feedback executes real routing and TDM scheduling,
   independently reconstructs all TimingPathDB paths, and accepts only a
   lexicographically better candidate.

The main loss is representation.  An ordered timing path is collapsed into
independent scalar net weights before partitioning.  A scalar cannot express:

- that moving a cell to target A loads a different link or capacity domain
  than moving it to target B;
- that two cut nets on one path accumulate transport delays;
- that cutting the same net at different source/sink partitions changes route
  length and contention;
- that many individually acceptable cuts create the maximum TDM ratio or the
  critical directed pair;
- that a path leaves and later re-enters a partition (path snaking).

The existing Phase 3--5 feedback observes these effects after routing, but
feeds them back as another target-independent per-net scalar.  That is the
specific gap addressed here.

## Proposed route: path- and pressure-aware portfolio refinement

The implemented provider name is `patron`.  It has two deliberately separate
layers.  A wider multi-provider portfolio remains a follow-on search-space
extension rather than a prerequisite for checking PATRON itself.

### 1. Diverse, checked candidate generation

The currently implemented candidate transaction contains:

- the current timing-weighted TritonPart candidate, always retained as the
  frozen fallback;
- a PATRON refinement of that exact assignment;
- additional feedback-weighted TritonPart+PATRON candidates when cross-stage
  iterations are requested.

MFSPart, RePart, and native TritonPart netlist timing mode are documented
future portfolio generators.  They are not described as part of the current
production transaction until their provider-neutral identity and fair exact
promotion paths exist.

No candidate is trusted because of its provider.  The existing independent
Phase 3 validator checks every assignment.  The portfolio therefore cannot
regress silently: if all new candidates are worse, it selects the old
TritonPart result.

### 2. PATRON direct K-way refinement

PATRON means **Path-Aware Topology and Routing-pressure Optimized Network
partitioning**.  It is an EmuFlow algorithm inspired by the papers above, not
a claimed reproduction of any one of them.

The current default combines PATRON with generalized Static Exact v2.  PATRON
derives transported net classes from the sealed cluster policy, which includes
real combinational cut nets in the endpoint-exact path and routing-pressure
model.  After refinement the common assignment builder reconstructs the
Static Exact dependency contract, and the independent checker revalidates that
contract together with the native transition trace and complete metrics.  The
legacy sequential policy remains an explicit comparison mode.

For each valid assignment, a deterministic direct K-way pass evaluates a move
of cluster `v` from partition `a` to `b` with a lexicographic objective:

1. hard violations: fixed/group, multi-resource capacity, reachability, and
   maximum-hop constraints;
2. predicted worst normalized path slack;
3. predicted total negative normalized path slack (TNS proxy);
4. maximum directed link/capacity-domain load and implied TDM ratio;
5. path snaking and ordered partition-transition count;
6. timing-weighted routed bit-hops, connectivity, and cut bits;
7. replicated LUT cost, when replication is enabled.

Before Phase 4 is available, predicted transport uses deterministic
direction-feasible shortest paths, BoardLinkTimingDB delay bounds, link width,
fabric frequency, demand width, and the aggregate directed domain load.  It is
explicitly marked `predicted`; it never substitutes for Phase 4/5 or final
physical timing.

The refiner maintains these values incrementally:

- per-net source and sink partition counts;
- per-directed-link/domain predicted bit load;
- per TimingPathDB path ordered partition transitions and transport penalty;
- per-partition resource usage;
- incident-net and incident-path indexes.

A move updates only incident nets, affected routed domains, and paths that
contain those nets.  Gains are stably quantized before deterministic tie
breaking; raw values remain in the trace.  Compact graphs use global-best
direct K-way and atomic ejection selection and are reproduced move-for-move by
the Python exhaustive oracle.  Large graphs use a timing-criticality-ordered native
sweep: each cluster chooses its globally best legal target under the current
proxy, then updates only its incident indexes.  Once direct moves converge,
PATRON v6 searches a bounded ejection neighborhood inspired by block-pair flow
refinement.  A critical cluster enters a target block while a low-exposure
donor from that block moves to either the critical cluster's source or a third
block.  Both moves are atomic; the final resource vectors, topology, timing
paths, and TDM domains are evaluated together, so no capacity-illegal
intermediate assignment is created.  This bounded neighborhood is not claimed
to be maximum flow.  It avoids pretending that an
`O(moves * nodes * targets)` Python/global scan is a scalable production
algorithm.  Actual Phase 4/5 scoring remains the promotion authority for both
modes.

PATRON v7 adds an opt-in block-pair hypergraph-flow refinement after the v6
direct/ejection pass.  `--patron-flow-refinement` writes a versioned `FLOW`
record into the native input; corridor distance, maximum corridor population,
piercing order, legal-candidate count, and polish bound are therefore part of
the task identity instead of ambient process state.  Bidirectional piercing
enumerates capacity-feasible cuts and a bounded incremental polish pass may
accept only moves that preserve the worst-slack rank while strictly reducing
the TNS rank.  The final multi-cluster change is committed atomically only when
both primary timing ranks strictly improve.  A Python certificate checker
reconstructs every source/target change, capacity and fixed-node legality, and
the complete before/after endpoint objective.  A compact four-block fixture
also enumerates the complete relevant target space independently and requires
the native batch rank and repeated-run output to match.  This remains a
candidate algorithm until complete-global Phase 7 WNS and TNS both beat the
frozen v6 result.

V11 adds an iterative, path-specific correction rather than a uniform
pre-route guard.  A prior independently validated Phase 7
`system-timing/v2` artifact is joined to the complete pressure path population
and its observed assignment.  Each exact cross-FPGA path contributes

```text
max(0,
    physical_logic - (period - prepartition_slack)
  + physical_interface - preplacement_fixed)
```

only when the current candidate uses the same source/sink FPGA pair.  The
artifact and its scale are part of the algorithm identity and canonical DAG
input set.  This is measured iterative feedback, not fabricated placement and
not a claim that an old physical route remains valid after repartitioning.
Moving either endpoint disables the old pair-specific residual, and the next
physical iteration may generate a new artifact.  The formal promotion gate is
unchanged: the candidate must beat the accepted complete-global Phase 7 WNS
and TNS on a frozen canonical comparison.  On the canonical DLA/EDA2023-case6
seed-1 gate, V11 improved complete-global WNS from `-82.4981025395 ns` to
`-82.496046406 ns` and TNS from `-324,776.89798473305 ns` to
`-104,893.24759878777 ns`; negative-slack paths fell from 8,803 to 2,641, with
zero unrouted nets and zero DRC violations.  This passes the two-metric gate,
but the `0.0020561335 ns` WNS margin remains explicitly qualified as narrow.

### 3. Endpoint-exact PATRON v2

The accepted v1 result above is the frozen comparison point for the next
iteration.  Inspection found that v1 represented each multi-fanout net by its
slowest source-to-sink transition and charged that transition to every timing
path containing the net.  This is conservative, but it loses the identity of
the sink actually used by a path.  It can therefore charge a local capture
path for an unrelated remote fanout branch and weaken correlation with final
Phase 7 timing.

PATRON v2 preserves the TimingPathDB launch and capture instances in the
pressure model.  For a path with structured endpoints and single-driver
transported nets, it walks the path nets backwards from the capture FPGA.  At
each transported net it selects only the source-to-current-capture branch,
checks that the branch reaches a real sink partition, accumulates that route's
BoardDB delay and current global TDM wait, and advances to the source FPGA.
The recovered chain must terminate at the path's launch FPGA.  Paths without
enough endpoint information remain explicitly labelled
`conservative-net-worst-v1`; they are not silently treated as exact.

The TDM ratio is still computed from all routed fanout demand, not merely from
the timing path being evaluated.  Thus v2 changes path attribution without
under-counting shared-link contention.  The scalable native refiner maintains
both net-to-path and capacity-domain-to-path indexes.  A move recomputes paths
containing an incident net and every current path whose delay changes when a
domain crosses a TDM-ratio threshold.  This also corrects the v1 scalable
proxy's omission of TDM wait from its path objective.

Compact mode remains step-for-step identical to the independent exhaustive
Python oracle, including atomic ejections.  Scalable mode is checked by a
separate critical-ejection
replay on reduced graphs and by a near-linear certificate checker on large
graphs.  A capacity-release fixture proves that a cluster rejected early in
one pass is reconsidered after a later move frees capacity.
The fanout regression proves that a local sink receives zero transport delay
while a remote sink on the same net receives its concrete routed delay; the
legacy endpoint-free copy of that fixture deliberately retains the old
worst-fanout charge.

The v6 success gate is stricter than the original branch gate: on the same
canonical DLA/case6/seed-1 ancestor it must improve both complete-global Phase
7 WNS and TNS relative to the already improved v1 values of
`-82.4981025395 ns` and `-324,776.89798473305 ns`.  Phase 3 proxy improvements
alone do not satisfy this gate.  Endpoint-exact v2 failed that gate because
its complete-global WNS regressed despite a substantial TNS improvement;
fanout-only v3 was rejected before physical execution after its frozen Phase 3
diagnostic preserved the original proxy WNS and worsened proxy TNS.  V4's
second fanout-aware sweep added 256 moves but worsened the independently
recomputed original negative-slack objective further to
`3,855.7736748538337`; it was therefore also rejected before Phase 7.  V5 set
the fanout surrogate scale to zero, but four frozen critical/donor windows all
returned 257 direct moves, zero swaps, unchanged worst-slack proxy, and only a
small negative-slack improvement from `3,232.26875` to
`3,230.8125185219201`.  V5 was not promoted.  V6 retains the accepted original
timing/TDM objective and permits only strict improvements, but lets the donor
of an atomic pair move to a third block.  This is the smallest extension that
models a two-vertex ejection path across a three-block capacity cork without an
illegal intermediate assignment.

V7 introduced the sealed FlowCutter-style corridor batch; V8 added a bounded
endpoint-exact repair of the current worst proxy rank.  Their canonical
seed-1 Phase 7 results (`-88.0781786658 ns`/`-111,714.59245574732 ns` for v7
and `-86.8172150516 ns`/`-110,390.50669188293 ns` for v8) improved TNS but did
not beat the accepted v1 WNS.  The v8 diagnosis exposed a local-search control
error: one immovable exact-worst rank terminated repair even though lower
ranks still contained legal moves that improved TNS without changing WNS.
V9 therefore keeps the exact-worst search as its fast path and expands to a
incrementally maintained deterministic 256-path critical window only after
that path stalls.
It accepts at most 256 moves, and each move must strictly improve the same
complete lexicographic objective.  This is ranked-frontier closure, not a
claim of globally optimal multilevel partitioning.  The frozen large-design
run accepted 154 repair moves and reached `-0.83226518752689782` worst
normalized slack and `-2914.1576328328747` total negative normalized slack.
The 256-, 4096-, and 16384-path windows produced byte-identical native output,
so 256 is the smallest tested bound that reaches this fixed point.  The
canonical V9 seed-1 result was `-83.891239479 ns` WNS,
`-108,268.13829710225 ns` TNS, and 2,544 negative paths with zero unrouted
nets and zero DRC violations.  It substantially improved TNS and path count,
but its WNS was `1.3931369395 ns` worse than V1, so it failed the unchanged
two-metric gate.

V9's worst complete-global path crossed two BoardDB hops.  Phase 7 attributed
`13.891239479 ns` to routed physical stages while the Phase 3 proxy had only
`2.442974 ns` of corresponding fixed delay, leaving an `11.448265 ns`
pre-route residual.  V10 therefore adds a deterministic physical-interface
risk guard of `5 ns` per routed arc to concrete endpoint-exact transport.
This follows the hop-constrained timing lesson from HoPart while preserving
EmuFlow's independently routed Phase 4/5 and complete-global Phase 7 as the
authorities.  The term is sealed in the V10 native input and trace, reproduced
by an independent Python objective, and absent from legacy V7--V9 contracts.
It is an experimental calibration hypothesis, not a physical-timing claim;
only a canonical result that improves both V1 WNS and TNS can promote it.

## Exact Phase 4/5 promotion

The predictor is only a search heuristic.  Each surviving portfolio/refinement
candidate is passed through the existing checked Phase 4 router, Phase 5 ratio
planner and scheduler, virtual-runtime/path evaluator, and independent
cross-stage validator.  Promotion uses the existing exact lexicographic
candidate key:

1. minimum feasible frame slots;
2. maximum virtual-clock timing margin;
3. worst normalized original-clock slack;
4. total negative normalized original-clock slack;
5. negative path count;
6. maximum TDM ratio and completion slot;
7. link bit-hops, cut bits, and replica LUTs.

This makes the new Phase 3 optimizer route- and schedule-aware without copying
Phase 4/5 logic into an approximate partitioner.

## Artifacts and independent validation

Implemented artifacts are versioned and hash-bound:

- `partition-pressure-model/v6`: TimingPathDB paths, structured launch/capture
  clusters when available, explicit exact/fallback transition semantics,
  predicted route/domain costs, a source-derived logarithmic remote-sink
  fanout surrogate, immutable constraints, and source hashes;
- `partition-pressure-trace/v6`: every selected move or atomic ejection, raw and
  ranked objective deltas, feasibility certificate, best prefix, and final
  assignment hash;
- `partition-pressure-report/v6`: the selected native provider, sealed model
  and trace, final assignment, and independent validation summary;
- `partition-physical-feedback/v1`: positive path-specific physical residuals,
  exact observed endpoint pairs, complete source hashes, and independent
  reconstruction metrics for an iterative v11 run;
- `partition-pressure-trace/v11`: the v11 feedback artifact hash and scale,
  exact endpoint-matching contract, raw/ranked objective chain, batches, and
  final assignment hash;
- the existing checked cross-stage report records the frozen seed candidate,
  exact Phase 4/5 score, rejection reason, and selected candidate;
- the existing canonical QoR comparison records frozen arm identities plus
  complete Phase 7 global WNS/TNS and routing/DRC checks.

The Python oracle enumerates all targets and recomputes the full objective for
small designs.  Large-design validation independently rebuilds the immutable
model, checks every assignment transition and its deterministic sweep order,
validates the objective chain, and recomputes complete initial/final capacity,
topology, cut, path, load, and assignment metrics.  It deliberately does
**not** claim that the scalable heuristic chose the global-best move.  Exact
global-best full replay is retained for compact graphs; a second Python
optimizer is not placed in the large production path.

## Implementation and validation stages

1. Freeze schemas, predictor equations, deterministic ordering, and a Python
   exhaustive reference.  Add adversarial tests for directed/asymmetric
   links, shared capacity domains, path snaking, zero/local paths, fixed/group
   constraints, multi-resource boundary equality, and tie stability.
2. Implement the native incremental PATRON refiner and independent checker.
   Prove compact global-best move-for-move equality with the exhaustive oracle;
   validate scalable transition ordering, legality, source seals, and complete
   endpoint metrics; and add 10k/100k complexity regressions.
3. Add `patron` orchestration without changing the global default.  Validate
   deterministic candidate isolation, baseline retention, failure containment,
   source hashes, and exact Phase 4/5 promotion.
4. Run full repository tests, strict native compilation, source audit, schema
   tamper tests, and normalized deterministic reruns.  Update README and public
   CLI documentation.
5. Use the experiment cache to run the canonical DLA/case6 seed-1 baseline and
   candidate from the same frozen ancestor.  Complete Phase 7 and require zero
   unrouted connections, zero DRC violations, independently validated reports,
   and strictly better complete-global WNS **and** TNS.
6. If either final metric does not improve, diagnose path attribution and
   adjust only the search model or candidate set; do not weaken the gate.
   Repeat on the reusable Phase 1--2 ancestor.  After the primary gate passes,
   run case7/case9 replications and publish the new default.

## Claim boundary

Passing unit tests, matching the oracle, improving cut/hop/TDM proxies, or
winning the exact Phase 3--5 score is necessary but insufficient.  The user-
visible claim is made only from the final independently checked Phase 7
complete-global timing result.  Internal per-FPGA WNS/TNS and cross-stage
predictions remain separately labeled diagnostics.
