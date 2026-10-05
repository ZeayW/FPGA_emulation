# Academic algorithm research and implementation roadmap

## 1. Objective

EmuFlow will pause further heuristic-provider development until each major
optimization stage has a literature-backed technical specification. The
target is not to attach paper names to familiar primitives such as Dijkstra,
FM, Hungarian matching, or min-cost flow. A paper-backed provider must
reproduce the paper's:

- problem definition and assumptions;
- decision variables and hard constraints;
- objective function and normalization;
- principal optimization procedure;
- continuous-to-discrete legalization;
- post-refinement procedure; and
- evaluation metrics and ablations.

The implementation may extend a paper to support EmuFlow-specific semantics,
such as multicast transport, two communication rounds, a global frame barrier,
or heterogeneous boards. Every extension must be isolated from the faithful
paper model and independently checked.

This document is a technical roadmap, not an experiment log. Machine
configuration, commands, raw results, artifact hashes, and benchmark tables
remain local.

## 2. Literature review protocol

Implementation of a stage starts only after its literature gate is complete.
For each stage, the review must contain:

1. at least one classical formulation;
2. at least three modern directly relevant methods where the literature
   exists;
3. the best available open-source implementation;
4. a comparison of variables, constraints, objectives, complexity, and
   scalability;
5. a list of assumptions that do not hold in EmuFlow;
6. a selected primary algorithm and at least one independent baseline; and
7. a mathematical test oracle for small instances.

Each selected paper is classified as:

- **direct integration**: upstream source is imported and built in-tree;
- **faithful reproduction**: EmuFlow implements the published formulation;
- **paper-informed extension**: the paper is a starting point, but the model
  is materially changed; or
- **background only**: the paper informs design choices but is not claimed as
  an implemented algorithm.

No provider is described as a reproduction until its equation-level
specification and small-instance oracle agree.

## 3. Common implementation standard

Every optimization stage follows the same implementation sequence:

1. write a provider-neutral mathematical model;
2. implement an exact small-instance oracle using ILP, CP-SAT, dynamic
   programming, or exhaustive enumeration as appropriate;
3. reproduce the selected paper algorithm without EmuFlow-specific
   enhancements;
4. compare the reproduction against the oracle on small adversarial cases;
5. implement a scalable C++ provider;
6. add EmuFlow extensions behind separate options;
7. reload the artifact with an independent checker;
8. validate on small, medium, 100K-cell, and NVDLA-scale connected RTL;
9. compare against the frozen baseline and paper ablations; and
10. promote the provider only if it improves the declared primary objective
    without weakening correctness.

Python remains the artifact, orchestration, reference-model, and checker
layer. Performance-critical optimization is implemented in C++ or CUDA.

### Stage selection summary

| Stage | Faithful primary route | Independent alternatives/oracle |
| --- | --- | --- |
| Architecture/timing | VTR XML + provider-neutral TimingDB + OpenSTA | FPGA Interchange/ECP5 adapters; Vivado comparison |
| Synthesis/mapping | Yosys/ABC9 | Mapping Fusion experiment; equivalence oracle |
| Hypergraph model | ASP-DAC 2025 adaptive modeling | complete flattening |
| Partitioning | MFSPart-Ensemble + RePart replication | TritonPart, SHyPar, exact ILP |
| System routing | DAC 2025 die-level router + ASP-DAC 2026 timing refinement | candidate-tree MIP |
| TDM | TODAES 2020 LR + exact schedule realization | SLIP 2019, CP-SAT |
| Pin planning | Chimew | exact matching/min-cost-flow oracle |
| Placement | OpenPARF + LEAPS/TD-Placer mechanisms | Vivado comparison |

## 4. Stage 0 - Architecture, timing, and board models

### Why this stage comes first

Timing-driven partitioning, routing, TDM, pin planning, and placement cannot
be academically meaningful when they optimize unrelated delay proxies. The
current Vivado timing-path adapter is useful for comparison, but an open,
provider-neutral timing and architecture model is required before timing
algorithms are promoted.

### Literature and source foundations

- VTR/VPR architecture XML and timing infrastructure;
- OpenSTA and OpenROAD timing infrastructure;
- FPGA Interchange DeviceResources as an optional real-device adapter;
- *Challenges in Large FPGA-Based Logic Emulation Systems*, ISPD 2018;
- UltraScale+/multi-die placement literature describing SLR, SLL, clock
  region, and heterogeneous-resource constraints.

### Technical route

1. Import the public VTR flagship architecture into provider-neutral
   ArchitectureDB and Architecture TimingDB artifacts. This increment is
   implemented for layout, relaxed capacity, primitive/block arcs, switches,
   segments, and directs.
2. Preserve mutually exclusive VTR modes and equivalent sites in an exact
   packing contract; connect generic Yosys/ABC mapping to those modes.
3. Translate Architecture TimingDB cell and interconnect arcs into OpenSTA,
   retain stable EmuIR path identities, and project maximum path criticality
   into power-law TritonPart hyperedge weights. This pre-placement increment
   is implemented for LUT, FF, multiplier, and synchronous RAM mappings.
   Candidate selection independently recomputes the weighted cut objective
   across all requested seeds and an automatic unweighted baseline, preventing
   timing mode from silently regressing below its same-seed baseline.
4. Build the VTR routing-resource graph and integrate source-built VPR detailed
   routing after OpenPARF placement.
5. Extend BoardDB from link-level capacity to direction groups, cable/SLL
   delay, lane clock, bank location, and physical channel constraints.
6. Produce one partition-independent timing-path database with stable EmuIR
   net identities.
7. Add ECP5 and FPGA Interchange/UltraScale+ adapters behind the same
   contracts. Commercial results may compare or calibrate an optional backend,
   but never define the default open model.
8. Version every model and record its confidence/qualification level.

### Acceptance gate

- OpenSTA path identity and clock domains agree with the mapped netlist.
- Timing paths are independent of a candidate partition.
- The selected architecture's region, resource, and routing constraints are
  present in the open model.
- Optional real-device comparison error is reported by path class; it is not
  silently absorbed into the academic model.

## 5. Stage 1 - Logic synthesis and FPGA technology mapping

### Reviewed directions

- Yosys and ABC;
- ABC9 timing-aware FPGA mapping;
- *Narrowing the Synthesis Gap: Academic FPGA Tools vs. Industry*, DATE 2023;
- *Mapping Fusion: Improving FPGA Technology Mapping with ASIC Mapper*,
  2025;
- classical cut-based LUT mapping and FlowMap;
- Lakeroad sketch-guided mapping for heterogeneous hard primitives.

### Decision

Do not reproduce a general logic synthesizer. Continue direct integration of
Yosys/ABC, but evaluate ABC9 and Mapping Fusion as timing/area mapping
experiments before choosing the default mapping path. Hard-block mapping
research is optional and follows stable LUT/FF mapping.

### Technical route

1. Freeze the current `synth_xilinx` result as the compatibility baseline.
2. Add an ABC9 provider with architecture and multi-clock delay information.
3. Preserve stable sequential-boundary identity through synthesis.
4. Add formal/sequential equivalence between provider outputs.
5. Compare LUT count, depth, critical delay, hard-block inference, and
   downstream partition cut quality.
6. Reproduce the Mapping Fusion experiment as an optional provider:
   interleave ASIC-cell mapping and LUT mapping, first with a deterministic
   search policy and only then with its learned design-selection policy.
7. Investigate Lakeroad only for DSP/BRAM/complex primitive coverage that
   Yosys mapping demonstrably misses.

### Acceptance gate

- Formal equivalence passes.
- No unsupported primitive enters EmuIR.
- ABC9 improves timing or area on real RTL without degrading partition
  legality.

## 6. Stage 2 - Hypergraph construction and safe clustering

### Core literature

- Karypis et al., multilevel hypergraph partitioning, DAC 1997/1999;
- *Efficient Hypergraph Modeling of VLSI Circuits for the MFS-Based
  Emulation and Simulation Acceleration*, ASP-DAC 2025;
- timing-driven circuit-partitioning work using path-based objectives;
- TritonPart and RePart input models.

### Current gap

The current union-find clustering is a correctness-preserving flat-netlist
transformation. It does not provide adaptive hierarchical flattening, clock
modeling, path-aware net construction, or a controlled tradeoff between
hypergraph size and lost partition freedom. Because only sequential boundary
classes are transportable today, a large combinational connected component
can also force an explicitly reported balance relaxation. No downstream
partitioner can repair that loss of freedom without extending the runtime and
equivalence semantics for controlled combinational cuts.

### Technical route

1. Separate semantic atomicity from scalability clustering.
   Semantic clusters preserve state, combinational-cut policy, macros, and
   user groups; scalability clusters remain reversible.
2. Define a controlled combinational-cut contract with multi-phase settling,
   deterministic scheduling, and cycle-equivalence checks before allowing
   those cuts to create new partition freedom.
3. Reproduce the ASP-DAC 2025 adaptive hierarchical flattening and parallel
   clock-modeling methods for large hierarchical RTL.
4. Represent:
   - multi-dimensional FPGA resources;
   - ordered timing paths;
   - clock domains;
   - replication eligibility;
   - fixed and grouped cells;
   - topology candidate sets; and
   - transportable versus forbidden cuts.
5. Preserve a lossless map from every hypergraph vertex and hyperedge back to
   EmuIR.

### Acceptance gate

- Hypergraph construction scales to multi-million-cell hierarchical designs.
- Every semantic cluster is preserved.
- Flattening choice is reproducible and resource aware.
- Partition QoR is compared with complete flattening at equal constraints.

## 7. Stage 3 - Multi-FPGA partitioning

### Core literature

- TritonPart, *An Open-Source Constraints-Driven General Partitioning
  Multi-Tool for VLSI Physical Design*, ICCAD 2023;
- RePart, *Efficient Hypergraph Partitioning with Logic Replication
  Optimization for Multi-FPGA System*, 2026;
- MFSPart and MFSPart-Ensemble, generalized multi-FPGA partitioning with
  decoupled coarsening/propagation and cut-overlay ensembling, TCAD 2026;
- TopoPart, topology-driven partitioning, ICCAD 2021;
- *Timing Driven Partition for Multi-FPGA Systems with TDM Awareness*,
  ISPD 2020;
- MaPart, multi-FPGA-system-aware partitioning, TCAD 2024;
- EasyPart, FPGA-emulation hypergraph partitioning, ICCAD 2024;
- SpecPart/K-SpecPart spectral and cut-overlay improvement;
- SPARK partitioning/routing, GLSVLSI 2023;
- BlasPart deterministic parallel partitioning, DAC 2025;
- SHyPar, effective-resistance spectral coarsening plus flow-based community
  refinement, TCAD 2025/2026;
- HySpecPro, GPU spectral-embedding projection optimization, 2026 preprint;
- preference-guided parameter tuning, ASP-DAC 2026.

### Baselines

- in-tree TritonPart multilevel hypergraph mode;
- in-tree RePart unique-owner mode;
- exact ILP/CP-SAT oracle for small hypergraphs.

### Implemented topology-feasibility increment

The common Phase-3 adapter now derives directed shortest-hop domains from
BoardDB whenever `max_route_hops` is present. The greedy baseline uses those
domains during initialization, and all providers pass through a native C++
topology-constrained FM audit/refinement with best-prefix rollback, pairwise
swaps, and a small exact fallback. Its objective is lexicographic feasibility
before weighted hop and cut cost; an independent checker reconstructs every
cut-net endpoint distance. This is a TopoPart/DATE-2024-informed enabling
increment, not the selected MFSPart reproduction. It becomes the legality
gate that the multilevel candidate-propagation work below must preserve.
The current FM move search is intentionally capped at 50,000 clusters for an
illegal input; larger assignments must arrive hop-legal from the constructive
provider rather than silently entering a quadratic post-repair pass.

### Selected primary route

Reproduce MFSPart's generalized multilevel framework as the primary
multi-FPGA engine, and integrate RePart's logic-replication mechanism as a
separate refinement operator. This replaces the earlier assumption that
RePart alone should be the primary algorithm:

1. **Decoupled coarsening, propagation, and initialization**
   - MFSPart affinity coarsening before candidate-FPGA propagation;
   - margin coarsening around fixed nodes;
   - driver-sink decomposition while retaining the original hyperedge view;
   - delayed candidate-FPGA propagation from the coarsest hypergraph;
   - two-phase probabilistic feasible assignment and deterministic violation
     repair;
   - fixed/group/multi-resource constraints.
2. **Multi-objective refinement**
   - MFSPart driver-sink cut, connectivity, mean-hop, and low-hop constraints;
   - distance-aware FM gains and restricted-neighborhood moves;
   - maximum pairwise cut and predicted TDM pressure;
   - path-based timing penalty, not only per-net criticality;
   - direct K-way FM, pairwise FM, and hyperedge refinement;
   - MFSPart-Ensemble cut-overlay recombination across selected uncoarsening
     levels.
3. **Logic replication**
   - RePart replication restricted by EmuFlow's semantic proof;
   - exact replica capacity charge;
   - timing/cut benefit evaluated after materialization.
4. **Quality escape**
   - compare SHyPar spectral coarsening, SpecPart/K-SpecPart, and MFSPart
     cut-overlay refinement under identical multi-FPGA constraints;
   - keep HySpecPro experimental until it supports nonuniform node weights
     and multi-resource legality; use it initially only to generate candidate
     solutions for constrained repair;
   - deterministic parallelism investigated after the serial reference is
     correct.

TritonPart's native OpenSTA timing-aware netlist mode must be evaluated
separately from EmuFlow's weighted-hypergraph adapter. They are not treated as
the same algorithm.

### MFSPart reproduction boundary

The primary reference is Zhu et al., *MFSPart: A Generalized Partitioning
Framework for Multi-FPGA Systems and Its Ensemble-Based Extension*, TCAD 2026,
DOI `10.1109/TCAD.2026.3656070`. The authors publish a companion repository at
<https://github.com/hkust-zhiyao/MFSPart>, but the repository has no explicit
software license as of pinned revision
`d1941db4580306c3d68b750953f9f7573ba899a0`. EmuFlow therefore cannot vendor
or derive from those implementation files. The in-tree engine is an
independent, source-visible reproduction from the paper's equations,
algorithms, and disclosed parameters; provenance records both the paper and
the companion link without treating the latter as a build dependency.

The serial reference implementation is divided into independently testable
increments:

1. **Paper data model and affinity hierarchy**
   - retain original directed hyperedges for connectivity objective Eq. 2;
   - build aggregated driver-sink edges for cut/hop objectives Eq. 1;
   - implement affinity coarsening Eq. 4 with stable seeded tie-breaking,
     explicit multi-resource cluster limits, lossless fine/coarse maps, and
     identical-hyperedge aggregation;
   - add fixed-node radius/margin coarsening only after the non-fixed hierarchy
     and oracle pass.
2. **Delayed propagation and initial partitioning**
   - propagate candidate FPGAs only on the coarsest graph;
   - use the complete propagation theorem and real-time maintenance direction
     from Li et al., DATE 2024 (DOI `10.23919/DATE58400.2024.10546878`);
     that paper states the zero-hop (`Hmax = 1`) recurrence while MFSPart
     permits general `Hmax`, so the open implementation explicitly uses the
     theorem-derived bound `FPGA_distance <= circuit_distance * Hmax` and
     labels this generalization as an inference rather than quoted pseudocode;
   - implement priority Eq. 5, probabilistic feasible assignment Eqs. 6--7,
     and deterministic empty-domain repair Eq. 8;
   - record the random seed and every candidate-domain contraction so a run is
     replayable.
3. **Uncoarsening and refinement**
   - project assignments level by level and implement direct K-way FM
     Eqs. 9--10 with best-prefix rollback;
   - enforce capacity and fixed-node legality on every tentative move, restrict
     target FPGAs to graph distance two by default, and apply the disclosed
     ineffective-move early-stop rule;
   - update driver-sink cut, connectivity, mean hop, and violation severity
     incrementally, while an independent checker recomputes them from scratch.
4. **Ensemble extension**
   - after the serial reference passes, maintain five deterministic-seed
     solutions and apply cut-overlay clustering every six uncoarsening levels;
   - preserve only structures common to paired parents, then rerun propagation,
     initialization, and FM on the overlay graph before remapping.

Paper-faithful metrics and EmuFlow extensions remain separate. The MFSPart
score reports driver-sink cut, connectivity, mean hop, maximum-hop violations,
capacity, and fixed-node legality exactly as defined by the paper. Path slack,
TDM pressure, heterogeneous-resource vectors, semantic cut legality, and
minimum-used-FPGA requirements are optional EmuFlow extension terms and are
never presented as part of the published MFSPart formulation.

The first acceptance oracle exhaustively enumerates compact assignments and
recomputes Eqs. 1--3 plus maximum-hop and fixed-node constraints. Each later
increment must preserve oracle agreement before it is enabled in Phase 3.
Large-design comparison is deferred until the constructive multilevel engine
can replace, rather than merely precede, the current 50,000-cluster post-FM
scale gate.

The affinity hierarchy, fixed-node margin coarsening, and
delayed-propagation/two-phase-initialization increments are implemented as
first-party C++ engines. Fixed anchors assigned to the same FPGA are premerged;
the original-graph radius protects nearby nodes; and a batched 0/1-shortest-path
repair checks the selected contraction set against the
FPGA-distance-plus-margin lower bound. Violating shortest paths release enough
lowest-affinity contractions in one round, including alternate paths, instead
of rebuilding the complete graph for every candidate merge. The native engine
records protected nodes, premerges, repair rounds, distance searches, and
rejection reasons. Its Python
adapter independently recomputes original-graph protection and contracted-graph
anchor distances in addition to reconstructing every hierarchy transformation,
replaying every seeded assignment and domain contraction, recomputing the paper
metrics, and providing a bounded exhaustive assignment oracle for compact
acceptance cases. They are not yet the default Phase 3 provider; promotion
waits for Phase 3 integration of the now-complete serial hierarchy.

The serial uncoarsening increment is also implemented: assignments are
projected through each lossless fine/coarse map and refined by direct K-way FM
using Eqs. 9--10, distance-two candidate moves, ineffective-move early stop,
and best-prefix rollback. Its native priority queue invalidates gains only for
driver-sink neighbors, pins of incident hyperedges, and nodes whose exact
multi-resource feasibility changes as a source/target FPGA load changes.
Compact cases retain both the exhaustive and independent incremental Python
move-for-move replays. Large levels instead use a separate first-party C++
certificate checker with a multidimensional orthant range-maximum tree. That
checker recomputes local Eq. 10 gains independently and proves the selected
move is the globally best legal candidate after every step; changing an FPGA's
remaining capacity only changes the range-query bound and does not invalidate
every node crossing a resource threshold. Python still reparses the sealed
native output and recomputes initial/final capacity, fixed, cut, connectivity,
hop, and violation metrics in linear time. This keeps strict global-best,
gain, early-stop, and best-prefix verification without running a second Python
FM optimizer on every large uncoarsening level.

The complete serial chain is now available as an explicit, non-default
`mfspart` Phase 3 provider. Its adapter derives directed driver-sink records,
active multi-resource vectors, fixed FPGA indices, symmetric BoardDB hop
distances, and independently checked balance capacities before invoking
coarsening, initialization, and level-by-level FM. The resulting assignment is
converted back through the common Phase 3 artifact builder and validator. A
small sequential RTL case passes both the Phase 3 CLI and the affected
multi-FPGA flow through routing, TDM, split-netlist generation, and runtime
equivalence. Asymmetric or disconnected BoardDBs are rejected explicitly in
paper mode. A separate first-party C++ legalizer enforces EmuFlow's
minimum-used-FPGA contract after uncoarsening without presenting that extension
as an MFSPart paper contribution. It incrementally maintains capacity, part
occupancy, and incident-net partition counts; ranks legal moves by exact
driver-sink cut and connectivity deltas; and cannot empty the source part or
move fixed nodes. A Python oracle recomputes every candidate objective from
the complete graph and replays the selected moves. Promotion to the default
provider still requires larger sparse-complexity evidence for fixed-node margin
coarsening and a frozen large-design downstream QoR comparison.

### Primary objective

Lexicographic feasibility first:

1. semantic cut legality;
2. multi-resource capacity;
3. topology and maximum-hop feasibility;
4. worst predicted path slack;
5. maximum pairwise cut/TDM pressure;
6. weighted connectivity and replica area.

### Acceptance gate

- Exact oracle agreement on small cases.
- Zero semantic, capacity, fixed/group, and topology violations.
- Paper-level ablations for coarsening, initialization, refinement, and
  replication.
- Better frozen downstream routing/TDM result, not only lower raw cutsize.

## 8. Stage 4 - System-level and die-level routing

### Core literature

- 2019 ICCAD CAD Contest system-level FPGA routing problem;
- *Time-Division Multiplexing Based System-Level FPGA Routing for Logic
  Verification*, DAC 2020;
- *Routing Topology and TDM Co-Optimization for Multi-FPGA Systems*,
  DAC 2020;
- *Multi-FPGA Co-Optimization: Hybrid Routing and TDM Assignment*,
  ASP-DAC 2021;
- ALIFRouter, DATE 2021;
- SPARK, GLSVLSI 2023;
- MaPart's layered-graph router, TCAD 2024;
- [*Synergistic Die-Level Router for Multi-FPGA System with Time-Division
  Multiplexing Optimization*](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf),
  DAC 2025;
- [*Timing-Aware Optimization of Die-Level Routing and TDM Assignment for
  Multi-FPGA Systems*](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf),
  ASP-DAC 2026;
- Steiner-tree and shallow-light-tree methods including KMB/Mehlhorn and
  SALT.

### Implemented milestone

The C++ provider now constructs six independently checkable topology
candidates: the original source-rooted shortest-path tree, a DAC
2025-informed delay-demand-balanced connection tree. The latter connects
high-cost sinks incrementally through legal Steiner attachment points and
uses distinct SLL and cable/TDM congestion costs, and a deterministic directed
Takahashi-Matsuyama nearest-terminal Steiner alternative, a directed
metric-closure Prim expansion, a criticality-gated shallow-light tree, and an
adaptive-hop tree derived from a direction-feasible hop lower bound. Phase 4
exports all six plus the selected refined tree in the versioned
`emuflow.route-candidate-pool/v1` contract.  A separate checker independently
reconstructs every tree's topology, direction locks, hop bound, latency, and
delay.  The provider selects the
better complete solution with the ASP-DAC 2026 worst-normalized-slack
objective before selective critical-path rip-up/reroute. A separate Python
oracle exhaustively enumerates direction-feasible directed arborescences and
global tree combinations on small graphs.

For reproducible ablation, `timing-aware-load-balanced-v1` executes only the
shortest-path/TLR candidate, while
`timing-aware-route-tdm-cooptimized-v1` enables both candidates and the
lexicographic selector. They no longer alias the same native execution mode.

This is classified as a **paper-informed extension**, not yet a faithful DAC
2025 reproduction: the public EmuFlow BoardDB model is more general than the
contest topology, and paper benchmark/result reproduction is still pending.
The exported pool and exact-oracle coverage gate are implemented. Following
the corrected whole-design Koios DLA Phase 7 A/B gate, the
`timing-aware-global-candidate-v1` provider is the timing-enabled default and
solves the restricted master
exactly when the candidate product has at most 200,000 combinations and uses
deterministic batch-conflict LNS above that boundary. Its Python oracle
independently enumerates compact products and reconstructs capacity, route
delay, quantized TDM delay, normalized slack, utilization, and bit-hops.
The planned directed metric-closure, shallow-light, and adaptive-hop columns
are now in the checked global-master pool. Batch-conflict LNS is implemented: a
deterministic coloring groups only path moves whose union candidate capacity
domains and affected STA paths are disjoint, native workers generate proposals
from a shared immutable snapshot, and stable serial commit rechecks the global
objective and capacity. A separate Python reconstruction checks the batches,
while workers=1/2/N regressions require byte-identical public artifacts.
Concrete Phase
5 feedback is now implemented as `emuflow.tdm-feedback/v1`: an independent
reconstruction binds the exact prior routes and schedule to per-domain
occupancy, wait, capacity, affected timing paths, and deterministic routing
prices. Phase 4 requires those source artifacts and reruns the checker before
passing prices to the native candidate generator; source, domain, or path
tampering fails before routing. This is a checked single feedback edge, not
yet the final iterative trust-region controller.
Phase 7 can close a second checked edge. The
`emuflow.physical-route-feedback/v1` artifact requires complete endpoint-keyed
TX/RX `boundary-timing/v1` coverage and binds it to the exact routes, concrete
schedule, runtime, and physical-summary hashes. It aggregates routed boundary
delay per capacity domain, normalizes the result by the BoardDB fabric slot,
and adds that price to the reconstructed schedule price. Phase 4 accepts the
combined price only after independently revalidating every source artifact;
changed physical delay or cross-run mixing is rejected. This is routed-
interface feedback, not a claim that final per-FPGA WNS/TNS decomposes
losslessly into an arc cost. Full Phase 7 A/B remains the acceptance metric.

### Selected primary route

1. **Candidate topology generation**
   - shortest-path tree baseline;
   - KMB/Mehlhorn Steiner approximation;
   - shallow-light-tree candidates for bounded source-to-sink delay;
   - DAC 2025 delay-demand-balanced negotiated path search with distinct SLL
     and TDM edge timing models;
   - congestion-perturbed and direction-feasible alternatives;
   - multicast sharing retained explicitly.
2. **Global tree selection**
   - restricted master formulation over candidate trees;
   - capacity, direction group, SLL, cable, and hop constraints;
   - Lagrangian/column-generation-style pricing or deterministic
     large-neighborhood refinement;
   - exact MIP oracle for small instances.
3. **Timing-aware refinement**
   - DAC 2025 connection ordering plus multithreaded Lagrangian TDM-ratio
     estimation and margin-aware legalization as a reproducible baseline;
   - ASP-DAC 2026 lossless timing-path compression;
   - majority-flow direction initialization;
   - adaptive timing/load edge weights;
   - selective rerouting of trees on the worst normalized-slack paths;
   - accept/rollback using independently reconstructed path slack.
4. **Routing/TDM interface**
   - pass multiple route candidates or marginal congestion prices to Stage 5;
   - do not collapse the problem to a single scalar predicted ratio too early.

### Primary objective

Maximize worst normalized path slack subject to exact reachability, direction,
hop, and channel-capacity legality. Maximum channel utilization, maximum TDM
pressure, total bit-hops, and runtime are secondary objectives.

### Acceptance gate

- Exact tree-selection oracle agreement on small graphs.
- Reproduction of DAC 2020/ASP-DAC 2021/ASP-DAC 2026 ablations.
- Multicast, half-duplex, unavailable-link, SLL/cable, and asymmetric
  direction tests.
- Improvement survives exact Stage 5 scheduling.

The small multicast exact-oracle gate is implemented. Contest-scale
reproduction and the larger candidate-pool gate remain open.

## 9. Stage 5 - TDM ratio, grouping, lane, and slot assignment

### Core literature

- virtual wires and phase assignment;
- ICCAD 2018 simultaneous partitioning and signal grouping;
- *An Analytical Approach for Time-Division Multiplexing Optimization in
  Multi-FPGA-Based Systems*, SLIP 2019;
- [*Lagrangian Relaxation-Based Time-Division Multiplexing Optimization for
  Multi-FPGA Systems*](https://cwpui.com/doc/j4.pdf), TODAES 2020;
- DAC 2020 routing/TDM co-optimization;
- ASP-DAC 2021 hybrid routing/TDM;
- *An Integrated Circuit Partitioning and TDM Assignment Optimization
  Framework*, ASP-DAC 2023;
- ASP-DAC 2026 timing-graph-based TDM assignment.

### Important separation

The literature usually optimizes TDM ratios or signal groups. EmuFlow also
requires a concrete, collision-free, multi-hop lane/slot schedule. Ratio
optimization and schedule realization are separate optimization problems and
must have separate oracles.

### Implemented milestone

The in-tree C++ ratio optimizer solves a path-form Lagrangian/KKT continuous
relaxation and performs critical-path swap refinement. Its discrete stage now
implements the TODAES 2020 minimum-feasible maximum-displacement search and
exact total-displacement dynamic program. Interval-cost precomputation reduces
the exact segmentation to `O(lanes * signals^2)` and raises the exact domain
limit to 2,048 signals. Still larger domains retain the deterministic
minimum-wire construction to bound runtime.
Lane groups are additionally separated by a checked transport-compatibility
domain. The default `global-frame-cdc` class records every contributing STA
clock domain but permits sharing because values are committed only at the
global frame barrier. A route may explicitly request a different non-empty
class, for example a source-synchronous protocol; the native legalizer and
independent checker then forbid sharing a physical lane across those classes.
This is a protocol/CDC constraint, not the incorrect assumption that every
logical clock name inherently needs a dedicated transport lane.
Independent exhaustive Python oracles verify the exact displacement objective
on small domains and the realized timing optimum of compact single-round
lane/slot schedules.

For scalable two-round realization, the capacity-only barrier estimate is
computed exactly from monotone `ceil(count / slots)` intervals and per-domain
difference arrays rather than a slot-by-bucket scan. If a native ratio plan
must be promoted to satisfy the split, each candidate uses an incremental
capacity delta and recomputes normalized slack only for paths containing the
promoted bucket; the minimum slack of unaffected paths is retained explicitly.
Small-instance tests compare the selected boundary against exhaustive slot
enumeration, and the independent serialized-artifact validator remains the
acceptance gate.

Exact ratio displacement is not assumed to imply a better concrete schedule.
The Phase 5 driver evaluates both the exact-DP and scalable minimum-wire
legalizations, realizes and checks both lane/slot schedules, and selects
lexicographically by independently reconstructed worst, p01, and median
normalized slack, followed by completion slot and analytical slack. This
guard prevents an improvement in the ratio surrogate from silently degrading
the realized schedule.

The concrete lane/slot realization starts from the ratio-aware deterministic
list scheduler and is improved by a source-built C++ deterministic LNS. For a
delayed hop on the worst path, the bounded neighborhood contains that hop and
up to three immediately preceding blockers on its physical lane; all relative
orders (at most `4!`) are evaluated through the complete multi-round schedule
builder. An optional OR-Tools CP-SAT oracle (`pip install 'emuflow[cp-sat]'`)
models medium fixed-ratio/lane instances on the time-expanded graph and proves
the lexicographic worst-slack/completion/wait optimum using one deterministic
solver worker. Its integer time/score certificate is independently rebuilt
from the public artifacts. The dependency-free exhaustive oracle remains the
small-case reference. Collision, precedence, round-barrier, value simulation,
and timing reconstruction remain independent acceptance gates. Large public
case and complete Phase 7 WNS/TNS qualification are still required before
making the upgraded route/TDM providers default.

### Selected primary route

1. **Continuous ratio optimization**
   - faithfully reproduce the TODAES 2020 Lagrangian-relaxation solver;
   - independently reproduce the SLIP 2019 nonlinear-CG formulation as a
     comparison provider;
   - use path-level timing and per-direction capacity constraints.
2. **Discrete ratio legalization**
   - reproduce binary-search/DP discretization and displacement objective;
   - support legal ratio sets, protocol/CDC compatibility groups, direction
     groups, and channel
     capacity;
   - reproduce swap/post-refinement on critical paths.
3. **Exact schedule oracle**
   - formulate lane/slot assignment as CP-SAT on a time-expanded graph;
   - include lane collision, precedence, latency, multicast, two transport
     rounds, and the global barrier;
   - use it as an oracle for small and medium cases.
4. **Scalable schedule construction**
   - decompose by capacity domain and timing component;
   - use list scheduling only for initialization;
   - apply bounded, deterministic large-neighborhood repair with the same
     time-expanded legality and objective;
   - allow controlled frame-length search.
5. **Timing reconstruction**
   - compute realized wait from concrete slots for every global STA path;
   - use analytical ratio slack only as a bound, never as final QoR.

### Primary objective

Maximize realized worst normalized slack. Secondary objectives are minimum
legal frame length, completion slot, maximum ratio, and lane usage.

### Acceptance gate

- Continuous and discrete paper formulations match their reference equations.
- Exact-oracle agreement on small schedules.
- Zero collision, precedence, round-barrier, or transport-value mismatch.
- Academic provider improves realized schedule timing, not only continuous
  ratio estimates.

## 10. Stage 6 - TDM signal grouping and package-pin assignment

### Core literature

- classical multi-FPGA pin assignment;
- *Pin Assignment Optimization for Multi-2.5D FPGA-Based Systems*,
  ISPD 2018;
- the 2021 TCAD extension for time-multiplexed I/Os;
- *TDM Signal Grouping and Package Pin Assignment for 2.5D Multi-FPGA
  Systems with Lookahead Placement* (Chimew), FPGA 2026.

### Integrated baseline and Chimew routes

The current balanced coloring, pairwise swaps, and Hungarian assignment are a
baseline. They do not faithfully reproduce Chimew's die-crossing encoding,
RUDY congestion gate, two-stage bank/pin assignment, or published edge-cost
model.

The baseline therefore emits provider-neutral identities
`placement-aware-region-grouping-mcf-v1`,
`electrical-package-pin-min-cost-flow-v1`, and
`placement-aware-pin-split-v1`.  Historical artifacts using the former
`chimew-*` identities remain readable for schema-v1 compatibility, but new
artifacts do not claim paper reproduction. The Chimew provider now uses a
distinct identity and passes the paper-specific kernel, certificate, and
electrical-adapter gates below. In the open academic physical flow it is the
default Phase 6 route; the previous static split remains an explicit A/B
baseline.

The first paper-specific kernel is isolated as
`chimew-algorithm1-encoding-grouping-v1`.  It reproduces Algorithm 1's
descending crossing-count order, same/covered/nearest-encoding selection, OR
group encoding, and ratio capacity in first-party C++, with an independent
Python decision replay.  Its input requires
`source-qualified-physical-sll-routing-v1`: explicit source/sink SLL indices,
an independently reconstructed encoding, and hash-sealed physical-routing
provenance. Normalized y regions are rejected by that paper-facing provider.
The open academic flow uses the separate
`academic-virtual-region-routing-lookahead-v1` identity for normalized
OpenPARF regions and therefore makes only an algorithm-integration claim. The
standalone result is marked
`not-a-phase6-pin-plan`; selection occurs only through the integrated pipeline,
which adds the verified position refinement, RUDY gate, two-stage assignment,
and concrete electrical-slot adapter.

The next isolated kernel is
`chimew-section3.3.2-bounded-inference-v1`. Its input requires physical-site
source-y coordinates from `source-qualified-physical-site-lookahead-v1` with
hash-sealed placement provenance; normalized coordinates are rejected. The
kernel only permutes signals that have the same grouping domain, TDM ratio,
and exact SLL encoding, so every group capacity and group SLL encoding remain
unchanged. It orders equal-encoding signals by source y as described in
Section 3.3.2 and accepts a permutation only when the independently replayed
within-group pairwise-y objective does not increase. The paper does not
publish its complete swap schedule or tie breaks, so the deterministic group
anchor order and acceptance objective are explicitly labelled a first-party
bounded inference, not an exact reproduction of unpublished details. This
artifact also remains `not-a-phase6-pin-plan` until the RUDY and two-stage
physical bank/channel gates pass.

The source-qualified congestion kernel is `chimew-section2.3-rudy-gate-v1`.
It accepts only `physical-site-xy` placement/net coordinates, hash-sealed
placement, netlist, and architecture provenance, an explicit routing-capacity
grid, and the physical wire pitch per routing layer. First-party C++ computes
the paper's HPWL wire area, uniform bounding-box density, exact overlap load
for every intersected bin, and peak utilization. Python independently
reintegrates every bin and checks global wire-area conservation. The supplied
maximum utilization is recorded as an EmuFlow qualification threshold, not a
constant attributed to Chimew. Because the paper's displayed formula divides
by bounding-box area without publishing a degenerate-net convention, v1
rejects zero-width or zero-height boxes instead of inventing an epsilon. The
report is still `not-a-phase6-pin-plan` until the two-stage bank/channel gate
passes.

The source-qualified assignment kernel is
`chimew-section3.4-two-stage-assignment-v1`. Stage 1 assigns every TDM group
or singleton common signal to a compatible bank pair with capacity equal to
its available channel count. Stage 2 solves both direction-priority channel
matchings for each used bank pair and selects the lower-cost alternative, so
TDM groups of one direction occupy the first channel interval, the opposite
direction occupies the next interval, and common signals use only the
remaining interval. First-party C++ computes Algorithm 2 costs from physical
bank/pin and source-fanout/sink-fanin coordinates. The displayed Algorithm 2
is implemented literally as the sum, per member signal, of its output
Manhattan distance and its mean input-fanin Manhattan distance; this explicit
interpretation avoids silently double-applying the reciprocal mentioned in
the accompanying prose. Cost ranks use a source-declared fixed scale solely
for deterministic exact optimization, while reports retain physical-site
units.

Each min-cost result includes a primal assignment and residual-graph node
potentials. Python independently reconstructs every legal edge and Algorithm
2 cost, checks capacity and direction intervals, and proves optimality by
requiring non-negative reduced cost on every residual edge. Identifier-free
physical rows with the same dual are canonicalized: Python checks every
selected reverse edge and every distinct row against every eligible channel,
which is exactly equivalent to checking the repeated expanded graph. It does
not replay a second optimizer on large designs. Small fixtures still compare
behavior against
hand-derived optima and reject tampered dual certificates. Dense banks with
repeated identical ranked-cost rows use an exact demand-compressed native
flow; deterministic expansion is accepted only after the same residual-dual
conditions are checked on every original edge. The standalone
result remains `not-a-phase6-pin-plan` until an adapter applies EmuFlow's
electrical and concrete-slot extensions without changing the paper claims.

That boundary is now executable as
`chimew-paper-plus-emuflow-electrical-slot-v1`. The adapter reruns the
certified two-stage native assignment, requires each paper channel to map
bijectively to a BoardDB link/physical lane, and preserves every schedule
entry's route, direction, ratio, logical lane, slot, and cycle meaning. Its
separate source-qualified electrical map binds bank identities, non-reserved
single-ended package pins, supported IOSTANDARDs, and matching bank voltages;
it rejects incomplete channel coverage, package-pin reuse, physical-lane
reuse within one link direction, and placement coordinates outside recorded
FPGA site bounds. Electrical-map v2 admits the same lane index in opposite
full-duplex directions only when BoardDB declares per-direction capacity and
the resources use distinct package pins; shared-bidirectional capacity remains
exclusive;
the final binding checker rederives the direction-qualified lane identity. The
result is a regular `emuflow.placement-aware-pin-plan/v1` accepted by Phase 6,
plus a distinct electrical binding certificate. Chimew paper metrics remain
under the paper provider identity, while voltage, package-pin, and
concrete-slot checks are explicitly labelled EmuFlow extensions.

For baseline schedules without per-entry paper ratios, the explicit
`emuflow-lane-occupancy-ratio-materializer-v1` adapter derives each existing
direction-qualified logical-lane group's exact occupancy, hash-binds the
source schedule, and rejects mixed ratios or lane-slot collisions. This is an
EmuFlow integration transform, not a Chimew paper claim.

The complete lookahead path is sealed separately by
`chimew-paper-kernel-chain-plus-emuflow-provenance-v1`. Its certificate binds
the exact schedule, crossings, both grouping stages, lookahead placement,
RUDY input/report, and bank/channel input/report. Validation reconstructs
group legality, SLL preservation, RUDY loads, assignment costs, and all
cross-artifact hashes without rerunning the grouping or assignment optimizer.
The adapter records `complete-artifact-chain` only when this self-sealed
certificate matches its schedule and bank/channel input; otherwise it is
explicitly `bank-electrical-only` and is not a full Chimew qualification.
The legacy `source-qualified-chimew-phase6-pipeline-v1` orchestration runs each
native kernel once and records every derived input and output digest, but its
source digests are declarations rather than byte-backed evidence. The stronger
`byte-bound-chimew-phase6-pipeline-v2` requires and copies the routing,
placement, netlist, architecture, and package-pin source artifacts, verifies
their byte hashes, seals that manifest into the qualification, pin plan, and
electrical binding, and can be replayed by the in-tree near-linear
`chimew-validate` checker without rerunning an optimizer. This byte binding
does not by itself prove that each derived JSON was semantically generated from
the corresponding source; that stronger claim requires a source adapter replay.
Runs without those five sources are explicitly
labelled `declared-digest-artifact-chain`. The separate kernel and adapter
commands remain available for debugging and external-tool integration.

Phase 6 requires that certificate whenever the Chimew provider is selected.
It independently rechecks schedule and source hashes, exact signal coverage,
direction, BoardDB link/lane bounds, bank/channel identity, package-pin
uniqueness, and IOSTANDARD/voltage compatibility before sealing the certificate
into the split manifest. A Chimew-labelled pin plan cannot bypass this gate.

### Selected primary route: faithful Chimew reproduction

1. Construct the FPGA-level lookahead netlist with die fences, cross-FPGA
   endpoints, cross-die nets, and bypass signals.
2. Run accelerated OpenPARF global placement and compute RUDY congestion.
3. Reject or feed back partitions whose lookahead congestion exceeds a
   qualified threshold.
4. Encode each signal's source/sink SLL-crossing signature.
5. Reproduce encoding-based greedy grouping:
   - same direction and ratio;
   - descending crossing count;
   - nearest compatible encoding;
   - exact group capacity.
6. Reproduce placement-based grouping refinement without increasing the
   crossing objective.
7. Reproduce two-stage package assignment:
   - bank-pair assignment;
   - channel/package-pin assignment;
   - min-cost-flow or equivalent exact weighted bipartite matching;
   - source-fanout and sink-fanin distance costs from lookahead placement.
8. Extend the faithful model separately with:
   - slot conflict;
   - differential pairs;
   - clock-capable pins;
   - forwarded clocks;
   - bank voltage/IOSTANDARD;
   - skew and frequency;
   - GT channels.

### Acceptance gate

- Exact matching oracle agreement.
- Published grouping and edge-cost equations are reconstructed independently.
- Zero electrical, direction, bank, connector, and pin-collision violations.
- Lookahead congestion/SLL metrics correlate with final Vivado results.
- Synthetic BSP results are labelled algorithm validation; hardware closure
  requires a real revision-controlled BSP.

The physical side of the correlation gate is now versioned independently:
Vivado board-flow report v3 seals the raw post-route
`report_design_analysis -congestion` report plus its official CSV output at
congestion level 3 and above, `report_utilization -slr`, and
`report_slr_crossing` outputs. Single-SLR parts receive an explicit
not-applicable crossing marker. The in-tree correlation checker now accepts a
hash-fixed manifest of at least three byte-bound Chimew/Vivado candidate pairs,
revalidates both complete bundles, extracts machine-readable congestion, SLR,
critical-path and WNS evidence, and computes tie-aware Spearman ranks for the
three predicted/final metric pairs. The correlation claim remains pending until
multiple real source-bound candidates pass that gate; synthetic fixtures only
validate the checker.
Relocatable v2 bundles remain readable but do not contain congestion CSV.

## 11. Stage 7 - OpenPARF-native placement and open physical closure

### Core literature

- OpenPARF, ASICON 2023;
- multi-electrostatic heterogeneous FPGA placement, DAC 2022;
- LEAPS multi-die/SLL-aware placement, TCAS-I 2023;
- OpenPARF 3.0 macro placement, ISEDA 2024;
- AMF-Placer 2.0 timing-driven mixed-size placement;
- DREAMPlaceFPGA-MP macro/fence placement;
- TD-Placer critical-path-aware timing-driven global placement, 2025;
- Chimew's accelerated lookahead placement, FPGA 2026.

### Two distinct placement products

1. **Lookahead placement** serves partition, routing, TDM, and pin planning.
   It must be fast and predict congestion, SLL crossing, and endpoint
   location.
2. **Physical placement** is the exact site/BEL assignment consumed by the
   selected detailed router.  It must model packing, hard macros, cascade
   adjacency, clock regions, SLRs, fixed I/O, and control sets itself.  A
   downstream router may verify or route the answer, but may not silently
   replace it with a vendor placer result.

They must not be evaluated by the same acceptance criteria.

### Selected technical route

OpenPARF remains the production implementation route.  DREAMPlaceFPGA and
AMF-Placer are independent candidate branches, not replacements for the
OpenPARF work and not evidence until their public optimization cores run.

1. **A1 atomic qualification.** Run the compiled OpenPARF global placement,
   native resource legalization, and detailed placement on ordinary LUT/FF
   atoms plus singleton DSP48E2, RAMB36E2, and URAM288 resources.  Re-import
   every site/BEL assignment, route it with RapidWright, and time the routed
   result with standalone OpenSTA.
2. **A2 native macro qualification.** Extend OpenPARF's own placement and
   legalization operators for indivisible CARRY8/LUT full-slice groups,
   MUXF7/8/9 relative macros, RAMB18 modes, and typed DSP/BRAM/URAM cascade
   chains.  Consume source-sealed RapidWright device facts; do not infer
   adjacency from `x/y` proximity and do not run a greedy site search after
   OpenPARF.
   The first typed-hardblock implementation is an in-core deterministic,
   conflict-aware whole-chain window allocator driven by global-placement
   displacement. It is not called an exact optimizer. All owned DSP/BRAM/URAM
   instances are removed from singleton MCF and frozen before ISM; an
   independent checker proves the selected windows against source-sealed
   dedicated adjacency. Qualify in the order DSP, BRAM, URAM so each family
   reaches RWRoute and standalone OpenSTA before the next family is promoted.
3. **Device-wide legality.** Enforce clock-region, SLR, fixed-I/O, control-set,
   and independently sourced half-column clock constraints during placement
   and legalization.  A missing authoritative constraint remains
   fail-closed.
4. **Placement objectives.** Add LEAPS-style SLR/SLL objectives, OpenSTA path
   weights, and RUDY/routability pressure only after the corresponding
   legality model is independently checked.  Intermediate HPWL or density is
   diagnostic, never the promotion metric.
5. **Open physical closure.** Convert the exact OpenPARF result to the
   RapidWright route boundary without moving cells, require zero missing sinks
   and zero placement/legalization fallback, extract endpoint-complete routed
   delays, and use standalone OpenSTA for authoritative global WNS/TNS.
6. **Candidate controls.** DREAMPlaceFPGA remains a research control while its
   upstream detailed placement and UltraScale+ macro/clock constraints are
   absent.  AMF-Placer is a secondary candidate only after its public
   optimization core, not merely its adapter, runs through the same route and
   timing gates.
7. **Workload promotion.** Freeze Phase 1--6, the architecture, router,
   resource limits, timing model, and one physical seed; then compare surviving
   candidates on Koios DLA medium through complete Phase 7.

### Acceptance gate

Lookahead:

- congestion and SLL rank correlation against the selected routed backend;
- endpoint-location stability;
- runtime suitable for iterative use.

Physical placement:

- complete compatible placement artifact;
- zero downstream placement displacement or repair;
- zero lost cells, illegal macros, overlaps, missing logical sinks, or
  unrouted required endpoints;
- exact site/BEL/macro revalidation against the same sealed ArchitectureDB and
  native-device-constraint facts;
- measured placement and routing runtime, routed congestion, SLL usage, and
  authoritative standalone OpenSTA global WNS/TNS.

RapidWright routing plus independent routed-delay extraction and OpenSTA is
the selected Xilinx research-qualification tail.  Vivado is optional external
comparison/sign-off evidence and must not be required to make the open Phase 7
placement route internally complete.

## 12. Cross-stage optimization campaign

Cross-stage work begins only after the corresponding single-stage providers
pass their literature and acceptance gates.

### Loop A - Partitioning, routing, and TDM

Foundations:

- ICCAD 2018 simultaneous partitioning/grouping;
- ISPD 2020 TDM-aware timing partitioning;
- ASP-DAC 2023 integrated partitioning/TDM;
- MaPart, EasyPart, and SPARK;
- DAC 2020, ASP-DAC 2021, and ASP-DAC 2026 routing/TDM methods.

Technical route:

1. freeze one partition-independent STA database;
2. solve exact routing and scheduling for the incumbent;
3. derive path-, channel-, hop-, and ratio-specific marginal costs;
4. update partition objectives through a trust-region/proximal step;
5. rerun partitioning, routing, and exact scheduling;
6. accept only an independently reconstructed all-path improvement;
7. roll back infeasible, tied, or regressing candidates.

### Loop B - Pin planning and placement

Foundation: Chimew.

Technical route:

1. generate lookahead placement;
2. group signals and assign banks/pins;
3. insert transport logic and rerun lookahead placement;
4. recompute RUDY, SLL crossing, and endpoint distance;
5. refine grouping/pins or request repartitioning;
6. stop on objective convergence or a strict iteration budget.

### Loop C - Full flow

Only after Loops A and B are independently stable:

1. partition;
2. route and assign TDM;
3. construct transport logic;
4. run lookahead placement;
5. assign pins;
6. evaluate congestion and timing;
7. update the highest-impact upstream decision;
8. accept/rollback with a multi-stage trust region.

A monolithic all-stage optimizer is explicitly deferred until these nested
loops establish reliable objective sensitivities.

## 13. Implementation order

The planned order is:

1. **R0 - Open architecture and timing foundation**
2. **R1 - Hypergraph construction and faithful advanced partitioning**
3. **R2 - Candidate-tree and timing-aware system routing**
4. **R3 - Faithful TDM ratio optimization and exact scheduling**
5. **R4 - Faithful Chimew grouping and package-pin assignment**
6. **R5 - SLR/timing/routability-aware OpenPARF placement**
7. **R6 - Partition/routing/TDM closed loop**
8. **R7 - Pin/placement closed loop**
9. **R8 - Full pre-Vivado outer loop**

Each increment is merged only after its exact oracle, independent checker,
real RTL validation, and frozen-baseline comparison pass.

## 14. Immediate next action

Public-contest qualification now includes a replayable EDA 2025 evaluation
gate. It independently revalidates the pinned fetch and semantic import,
freezes candidate routes/topology, reruns the contest evaluator, compares the
official text outputs byte-for-byte, and seals exact artifact coverage. This
is evaluation evidence only; the academic BoardDB projection and downstream
RTL/physical gates remain separate claims.

R0 now has a source-built C++ VTR XML importer, provider-neutral
ArchitectureDB/Architecture TimingDB contracts, and source-built VPR exact
packing, baseline placement, routing-resource-graph construction, detailed
routing, and analysis. The pinned public flagship model supplies heterogeneous
layout and timing data without commercial device files. The ArchitectureDB
view remains a relaxed planning view across mutually exclusive modes; exact
packing legality comes from VPR consuming the original XML.

R0 also has source-built standalone OpenSTA and optional FPGA Interchange
importers. The older analytical UltraScale+ model and mixed-license
RapidWright-produced region data remain optional compatibility inputs, not the
definition of the open research flow.

The packed-netlist contract is now implemented by a C++ VPR `.net` importer
plus an independent validator. It preserves selected modes, the complete used
pb hierarchy, atom membership, cross-cluster nets, and source hashes.

Packed clusters are now exported with exact VTR site capacities to the
source-built OpenPARF analytical placer. Architecture-defined single-site
resources use OpenPARF's min-cost-flow legalizer; the independently checked
result is emitted as VPR `.place` and accepted by source-built VPR routing.
The routed result is then checked independently of VPR: a streaming C++17
checker matches every route node to the RR graph, verifies exact
edge/switch connectivity and route-tree branch restarts, reconstructs
per-resource occupancy/capacity, and binds net/sink coverage to the packed
contract and placement hash.

The pinned flagship mapping profile now preserves Yosys multiplier and
synchronous RAM inference through eBLIF. Fixed-width mapping cells implement
the architecture's legal multiplier modes, while `memory_libmap` selects its
legal 32-Kibit single/dual-port modes. VPR performs the final mode-aware
packing. ArchitectureDB dimensions are taken from VPR's actual auto-layout
placement rather than a design-specific constant.

The same per-FPGA path is exposed as one checked `vpr fpga-open` transaction. Its
aggregate contract cross-binds the synthesized eBLIF, VPR packed netlist,
ArchitectureDB dimensions, OpenPARF placement, routed RR graph, and independent
route-check report. The explicit commands remain available for algorithm
development, but are no longer the only way to assemble the open backend.

The remaining R0/R1 work is:

- generalize the implemented flagship multiplier/RAM mapper into
  architecture-specific mapping profiles and add remaining hard-block types;
- translate Architecture TimingDB into OpenSTA cell/interconnect models.

Until R0 is complete, existing timing-aware providers remain research
prototypes and are not promoted as definitive paper reproductions.

## 15. Recent online literature additions

The local paper collection remains the main review corpus. The following
newer primary sources were added through online search and have already
changed the route above:

- [MFSPart and MFSPart-Ensemble, TCAD
  2026](https://zhiyaoxie.com/files/TCAD26_MFSPart.pdf): primary generalized
  multi-FPGA partitioning framework.
- [RePart, 2026](https://arxiv.org/abs/2604.00780): logic-replication
  refinement.
- [SHyPar, TCAD 2025/2026](https://arxiv.org/abs/2410.10875): spectral
  coarsening alternative.
- [HySpecPro, 2026](https://arxiv.org/abs/2607.00055): recent GPU spectral
  optimizer; candidate generation only until weighted multi-resource
  legality is supported.
- [Synergistic Die-Level Router, DAC
  2025](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf):
  delay-demand-balanced routing and Lagrangian TDM-ratio baseline.
- [Mapping Fusion, 2025](https://arxiv.org/abs/2507.10912): experimental
  synthesis/mapping provider.
- [TD-Placer, 2025](https://arxiv.org/abs/2512.00038): critical-path-aware
  placement candidate.
- [Chimew, FPGA
  2026](https://magic3007.github.io/data/publications/FPGA_FPGA2026_Wang/FPGA_FPGA2026_Wang.pdf):
  primary signal-grouping and pin-assignment route.

Preprints are marked as such and are not treated as established baselines
without equation-level reproduction and independent validation.
