# Algorithms and providers

EmuFlow combines upstream EDA engines with project-specific system algorithms.
A provider name identifies an implementation and its contract, not a universal
quality-of-result guarantee. The phase descriptions distinguish direct tool
integration, implementations based on published methods, and EmuFlow-specific
extensions.

## Phase 1: RTL synthesis and technology mapping

EmuFlow invokes [Yosys](https://github.com/YosysHQ/yosys) to run the RTL
synthesis flow: RTL elaboration, process and memory lowering, logic
optimization, LUT mapping, and mapping to supported device primitives.
The mapping profile determines whether RAM, DSP, and carry structures use
architecture-specific hard blocks or soft logic. These choices must agree
with the physical backend.

EmuFlow's contribution is the frontend integration and EmuIR representation:
stable cell/net identities, connectivity, primitive parameters, and resource
demand shared by the later stages. Production mapping targets the supported
Xilinx UltraScale+ primitive namespace; alternative compile profiles are retired.

## Phase 2: timing extraction

[OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) analyzes the mapped
netlist under the declared timing model and clock constraints. EmuFlow exports
the resulting path population, endpoint identities, and slack into
TimingPathDB, then derives criticality information for partitioning, routing,
and TDM optimization. This upstream producer is shared by both maintained
Xilinx physical backends. The independent Vivado timing producer is retired;
vendor post-route measurement belongs to Phase 7.

This is pre-physical timing: inter-FPGA route, TDM events, and routed FPGA delays
are resolved later. Phase 7 performs the final global analysis.

## Phase 3: initial partitioning and post-processing

The default sequence is **TritonPart initial partitioning → PATRON
post-processing**, operating on the same atomic-cluster hypergraph.
[TritonPart in OpenROAD](https://openroad.readthedocs.io/en/latest/main/src/par/README.html)
produces the resource-balanced initial partition through multilevel
coarsening, initial partitioning, and uncoarsening with refinement. Multiple
vertex weights represent LUTs, registers, memories, and DSPs. Exact per-device
resource bounds are supplied directly to the native solver; all resource
comparisons are componentwise, and only feasible candidates can be selected.
FM rollback retains only prefixes that satisfy every resource bound, including
when initialization starts from an unbalanced assignment.
EmuFlow enables native ILP for coarse graphs of up to 1,000 vertices. Each
native ILP first establishes resource feasibility with a 10-second budget when
no legal incumbent exists, then optimizes cut for up to 30 seconds. It accepts
either optimal or feasible integer solutions and retains the legal incumbent
when cut optimization exhausts its budget. Independent resource checks still
gate acceptance.
If packing requires a finer graph beyond the cut-ILP threshold, native
resource-only ILP remains available up to 10,000 vertices; it borrows the
existing vertex weights, omits connectivity variables, and hands the legal
seed back to FM and uncoarsening. It does not repair a published partition.
There is no external balance legalizer. EmuFlow checks
and repairs configured hop constraints before handing the initial assignment
to PATRON. See the [TritonPart project and publication](https://github.com/ABKGroup/TritonPart).

Only PATRON v14 is supported in production. The older native global-best and
critical-flow variants, v10 hop-guard scoring, v11 measured-residual feedback,
legacy trace/provider dispatch and full reference-search runner are retired.
Old results require their matching Git revision. The shared model's v6 schema
is still current: its storage version is independent of the search algorithm.
The v14 typed wire contract, min-cut/ejection kernels and independent complete
model/objective/assignment validation remain. Detailed qualification merges
moves and flow batches as ordered subsequences using independently checked,
strictly improving objective ranks. Sweep labels describe a local pass rather
than a global event order. Each source/target/fixed/resource transition and the
complete initial/final objectives are reconstructed; production still publishes
summary-only typed results. A bounded fixture comparison is
not a replacement for real-design physical/global timing acceptance.

**PATRON is EmuFlow's communication-aware post-processing algorithm.** Its
native C++ local search starts from that assignment and considers single
atomic-cluster moves and bounded paired moves, including moving another
cluster out of the destination or moving two connected clusters together.
Candidates must preserve resource limits, fixed placements, board
reachability, and hop constraints.

For each candidate partition, PATRON incrementally updates the affected
communication-route estimates and directed link loads, estimates TDM ratios
from those loads and link capacities, and recomputes communication delay on
affected timing paths. The core estimate is
`predicted path slack = Phase 2 path slack − estimated communication delay`.
Thus, changing a partition changes the communication estimate used to score
it; this is more than optimizing a fixed set of net weights.

PATRON accepts feasible improvements in lexicographic order. It first minimizes
the maximum cumulative physical-hop count over the supplied critical paths.
Among assignments with the same maximum, it minimizes
`max_path(sum_crossing_net_ratio)`: each critical path sums one predicted TDM
ratio for every crossing net, then PATRON takes the maximum over the complete
supplied path set. For a net whose estimated system route has multiple physical
hops, the net ratio is the maximum link ratio on that route and is counted once
in the path sum. The path attaining the maximum ratio sum may differ from the
path attaining the maximum hop count; candidate generation tracks both maxima
as independent frontiers. The path-frontier batch search is selected from
those paths' domain exposure and does not require an estimated link load to
cross a TDM-capacity threshold before it runs. A legal batch that strictly
reduces the maximum path-hop objective is accepted before searching another
board edge; the tail search then applies the ratio-sum objective and remaining
tie breakers to that assignment. Lower-priority tie breakers maximize the worst normalized
predicted slack, improve the sum of negative normalized slacks toward zero,
then minimize negative paths, maximum estimated TDM ratio, maximum estimated
link load, path transitions or snaking, bit-hops, and cut bits. Slacks use the
TimingPathDB clock-normalization policy. These are partition-search proxies,
not final physical WNS/TNS.

Estimated per-link capacity pressure is reported by Phase 3 but is not a hard
PATRON acceptance gate. Phase 3 uses a fixed shortest-route estimate and cannot
prove that a physical link is overloaded because Phase 4 may reroute a net
through another FPGA and Phase 5 assigns the concrete TDM ratios and events.
The platform's declared ratio domain is authoritative throughout these
estimates and later legalization. For the bundled XCVU19P cable model it is
`R in {8, 16, ..., 256}`; `frame_slots` describes the event schedule and is
never substituted for the maximum legal ratio.
PATRON does not run a capacity-closure repair loop; failure to improve its soft
path objective retains the legal TritonPart assignment. Per-FPGA resource
capacity, any explicitly configured balance dimensions, fixed placement, board
reachability, and the configured per-net route-hop bound remain structural hard
constraints. The
current selection certificate records provider
`patron-path-objective-selection-v2`; the earlier
`patron-predictive-timing-selection-v1` identifier remains historical and does
not describe this ordering.

PATRON post-processes the initial solution rather than restarting a complete
multilevel partitioning flow. Its route and TDM estimates do not
invoke or feed back actual Phase 4 routing, Phase 5 schedules, or Phase 7
physical results. Any claim of final timing improvement therefore requires a
separate complete-flow comparison. PATRON is the project's algorithm name,
not a claim to reproduce one particular paper.

RePart and MFSPart initialization, replication and post-refinement routes are
retired. Greedy partitioning is retained only as a small independent test oracle.
TritonPart remains the PATRON initializer and a direct comparison
capability. Historical implementations are available through Git.
Boundary policy is a separate choice: sequential-only and generalized
Static Exact admit different cut locations. Exact transport-event legality is
established after routing and TDM assignment, not by a Phase 3 timing proxy.

## Phase 4: system-level multicast routing

The sole Phase 4 production provider, `timing-aware-global-candidate-v1`,
connects each cross-FPGA net's source to its sinks
on the BoardDB graph. It supports shortest-path and delay-demand-balanced tree
construction, additional Steiner-style candidates, and iterative rerouting.
Candidate costs account for delay, congestion, shared link capacity, and TDM
pressure, with separate treatment of intra-device SLL and inter-FPGA links
when the platform provides that distinction.

The research basis includes Wang et al.'s **Synergistic Die-Level Router**
(DAC 2025) and Chen et al.'s **Timing-Aware Optimization of Die-Level Routing
and TDM Assignment** (ASP-DAC 2026); links are in the
[reference list](#research-references). The in-tree implementation
extends these ideas with EmuFlow's route contracts and candidate selection.
System routing selects FPGA/die paths; it does not route wires inside an FPGA.

Shortest-path, delay-demand-balanced, Steiner, metric-closure, shallow-light
and adaptive-hop trees are candidate generators inside this engine. They are
not separate supported routing providers. The restricted master and conflict
batch refinement select and improve complete legal multicast routes. Independent
capacity, direction, hop-bound, timing and candidate checks remain in force.

The timing-oblivious policy uses the same candidate engine without STA costs,
then independently attaches the original normalized timing records when given.
It does not select a second load-balanced or post-evaluation provider. A
zero-demand assignment produces an empty route artifact under the same provider.
The small exhaustive route oracle lives only in tests.
Legacy routing input versions, standalone routing executables and the old
routing/TDM baseline-versus-upgrade comparison commands are retired.



### Retired external routing feedback

Phase 4 consumes the selected assignment, BoardDB constraints and upstream timing
population. Standalone TDM/physical feedback documents, feedback price injection,
and the unused clock/protocol compatibility exporter are retired. The native
global-candidate router retains its own congestion history, candidate generation,
timing weights and TDM cost model. Historical feedback runs require their matching
Git revision; there is no alternate production reader or fallback.


## Phase 5: TDM ratio, lane, and slot assignment

The production algorithm is `deterministic-dependency-earliest-slot-v1`, an
EmuFlow scheduler for the Static Exact cut-dependency DAG. It processes ready
cuts in deterministic dependency order, assigns a legal lane and earliest TX
slot, and propagates actual RX arrival before releasing dependent transfers.
It preserves relay latency, sampled virtual-wire settle/commit constraints,
and absolute TX events used by downstream timing analysis.

The complete native `phase5 run` stage consumes the current typed
RouteStore and the Phase 3 source-bound contract. It constructs the schedule
and readiness certificate directly in ScheduleStore. Independent cursor/join
validation completes before atomic report-last publication; there is no
Python result import or size-dependent reference fallback.

Whole communication banks are allocated between directions using directional
load. Hardware ratio limits, compatibility, shared capacity and the calibrated
per-hop delay model remain constraints even though independent ratio/KKT,
timing-DAG and slot-refinement providers are retired. Chimew consumes these
concrete lane/slot/ratio facts and does not depend on the retired optimizers.
See the [hardware platform and link model](HARDWARE_PLATFORM.md).

The old registered-boundary producer, split scheduling/publication protocol,
and cross-stage ratio-feedback optimizer are removed. A bounded earliest-slot
oracle lives in tests only; supported explicit artifacts retain independent
legality checks. Minimum-frame search reruns the same production scheduler
and records `emuflow.frame-search/v5`, without optimizer-selection parameters.

## Phase 6: pin planning, netlist splitting, and transport

The **baseline** consumes the Phase 5 lane/slot assignment directly. The
maintained optional provider is **Chimew**. The independent region-based
placement-aware planner, its native executable and its comparison arm are
retired; necessary pin-plan, direction, electrical and slot validators remain.

The optional **Chimew** provider implements methods from *TDM Signal Grouping
and Package Pin Assignment for 2.5D Multi-FPGA Systems with Lookahead Placement*
(FPGA 2026): crossing-encoding grouping, position refinement, and two-stage
bank/channel assignment using min-cost flow. Indexed placement from the selected
Xilinx physical backend and RUDY estimates supply physical guidance. The implementation keeps
paper-derived kernels separate from EmuFlow's electrical, slot, and transport
adapters. Academic region estimates must be distinguished from actual SLR/SLL
data, and package binding requires a declared pin/electrical model.

For shared-bank platforms, physical lane remapping must remain inside the
direction banks allocated by Phase 5. Phase 6 rejects mappings that cross those
bank ranges or combine opposite directions in one bank.

The fixed-board Chimew electrical adapter resolves v3 wire references through
[the existing HP-bank LVDS inventory](HARDWARE_PLATFORM.md#fixed-virtual-board-wiring),
including both polarities at both ends. This is an EmuFlow integration contract;
it does not change the paper-derived grouping or min-cost-flow kernels.
The integrated producer queries original instance IDs in the source/mapped
owners and actual placed descendants, with boundary registers for forwarded
hops. It exposes only fixed wires eligible under the concrete Phase 5 bank
directions. Former synthetic pins, lane-index geometry and opposite-direction
TDM bundling are removed from this producer. Timing guards belong to the
crossing input; the Phase 5 hop store is referenced without cloning it.
These integration-policy changes require separate physical/QoR qualification.

All providers feed the EmuFlow netlist splitter and transport generator.
Static TDM communication, shadow state, and frame synchronization implement
the declared virtual-cycle semantics. These runtime mechanisms and their
equivalence checks are project implementations, not part of Chimew.

## Phase 7: physical implementation and global timing

The physical backends share a result contract while using different device
models and implementation engines. The default Xilinx research route combines
RapidWright device data, native OpenPARF placement, validated site/BEL binding,
and RWRoute detailed routing. Vivado is the other maintained implementation.
The VTR/open engine and its architecture, packing, routing and timing adapters
are retired; shared source identity, coverage and physical validators remain.

AMF and DREAMPlaceFPGA feasibility routes are retired, including their native
runners and adapter-specific validation. Shared physical contracts continue to
serve the maintained Xilinx routes; upstream code used by OpenPARF is retained.
The packed-cluster OpenPARF guidance plus Python greedy legalizer and
single-SLR selection are also retired. Native OpenPARF is the sole RapidWright
placement producer. Shared physical legality rules remain in production;
bounded packing/site fixture oracles are test-only. The capability probe
describes the atomic path and no longer offers the retired packed-cluster smoke.

See the [Xilinx physical-flow contract](XILINX_PHYSICAL_FLOW.md) for placement,
routing, fixed differential I/O, and timing acceptance criteria. Board topology,
link capacity, fixed package-pin wiring, and the TDM delay formula belong to the
separate [hardware platform model](HARDWARE_PLATFORM.md).

## Source and attribution

Upstream revisions, incorporated sources, licenses, and algorithm references
are recorded in [the component inventory](../OPEN_SOURCE_COMPONENTS.md) and
[the source manifest](../SOURCE_MANIFEST.json). Preserve those records when
changing an integration. A paper-informed extension must not be labelled a
faithful reproduction without equation-level and implementation validation.

## Implementation and reference map

The [research references](#research-references) below provide publication
links. This map identifies where those methods enter the implementation so
contributors can distinguish an upstream engine from an EmuFlow algorithm or
adapter.

| Component | Relationship to upstream work | Implementation entry point |
| --- | --- | --- |
| Yosys / ABC synthesis | Direct upstream tool execution; EmuFlow mapping profiles and import | [yosys.py](../src/emuflow/yosys.py) |
| OpenSTA path extraction | Direct STA-engine integration; EmuFlow path contract | [opensta.py](../src/emuflow/opensta.py) |
| TritonPart | Upstream initializer integration; shared assignment/legality contracts | [partition.py](../src/emuflow/partition.py) |
| PATRON | EmuFlow path-aware K-way refinement initialized by TritonPart; not an external paper implementation | [patron_refiner.cpp](../src/native/patron_refiner.cpp) |
| Static Exact semantic analysis | EmuFlow structural dependency/capture traversal with a Python reference and native large-design implementation | [static_exact_analyzer.cpp](../src/native/static_exact_analyzer.cpp) |
| System router | EmuFlow implementation drawing on DAC 2025 delay-demand balancing and ASP-DAC 2026 timing-aware routing, with additional candidate trees | [tlr_router.cpp](../src/native/tlr_router.cpp) |
| Static Exact TDM scheduler | EmuFlow deterministic dependency ordering, earliest legal TX events, and independent readiness validation | [tdm_exact_scheduler.cpp](../src/native/tdm_exact_scheduler.cpp) |
| Chimew initial grouping | Paper Algorithm 1 crossing-encoding grouping | [chimew_signal_grouper.cpp](../src/native/chimew_signal_grouper.cpp) |
| Chimew position refinement | Paper Section 3.3.2 with explicitly documented implementation choices | [chimew_refinement.py](../src/emuflow/chimew_refinement.py) |
| Chimew bank/channel assignment | Paper Section 3.4 two-stage min-cost-flow kernel | [chimew_bank_channel_assigner.cpp](../src/native/chimew_bank_channel_assigner.cpp) |
| Chimew integration | EmuFlow electrical and slot binding around the paper-derived output | [chimew_phase6.py](../src/emuflow/chimew_phase6.py) |
| Split netlists and transport | EmuFlow runtime implementation and validation | [phase6.py](../src/emuflow/phase6.py) |
| OpenPARF/RWRoute / Vivado | Physical-tool integrations selected by backend and device contract | [physical_backend.py](../src/emuflow/physical_backend.py) |
| RapidWright / RWRoute | Device-resource, site/BEL, and native routing integration | [rapidwright_provider.py](../src/emuflow/rapidwright_provider.py) |
| Global OpenSTA | EmuFlow timing-model exporter and upstream STA execution | [global_sta.py](../src/emuflow/global_sta.py) |

In particular, neither the Chimew paper's reported timing gains nor a placement
engine's published benchmark results transfer automatically to EmuFlow. Those
claims require complete physical runs under the same declared workload,
platform, constraints, and metric definitions.

## Research references

These references explain implemented methods and upstream foundations. Exact
source revisions and licenses are maintained in
[the component inventory](../OPEN_SOURCE_COMPONENTS.md); implementation entry
points are mapped in [implementation and reference map](#implementation-and-reference-map).

- **TritonPart** — multilevel, multi-constraint partitioning, ICCAD 2023.
  [Author project and paper](https://github.com/ABKGroup/TritonPart).
- **Synergistic Die-Level Router for Multi-FPGA System with Time-Division
  Multiplexing Optimization** — Wang et al., DAC 2025.
  [Author manuscript](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf).
- **Timing-Aware Optimization of Die-Level Routing and TDM Assignment for
  Multi-FPGA Systems** — Chen et al., ASP-DAC 2026.
  [Author manuscript](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf).
- **Chimew: TDM Signal Grouping and Package Pin Assignment for 2.5D Multi-FPGA
  Systems with Lookahead Placement** — Wang et al., FPGA 2026.
  [Paper](https://yibolin.com/publications/papers/FPGA_ISFPGA2026_Wang.pdf),
  [DOI](https://doi.org/10.1145/3748173.3779199).
- **OpenPARF: An Open-Source Placement and Routing Framework for Large-Scale
  Heterogeneous FPGAs with Deep Learning Toolkit** — 2023.
  [Paper](https://arxiv.org/abs/2306.16665), [source](https://github.com/PKU-IDEA/OpenPARF).
- **RapidWright: Enabling Custom Crafted Implementations for FPGAs** — FCCM 2018.
  [Official publication page](https://www.rapidwright.io/docs/Papers.html).
- **RWRoute: An Open-source Timing-driven Router for Commercial FPGAs** —
  [Author manuscript](https://www.rapidwright.io/docs/_downloads/3858e01e500602e00f7d5dda065b19ec/RWRoute_final_submitted.pdf).
