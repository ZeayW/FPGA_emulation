# EmuFlow architecture and implementation plan

## 1. Goal and scope

EmuFlow compiles a synchronous RTL design into multiple FPGA implementations
connected by statically scheduled board links. The target is a source-complete
implementation through synthesis, partitioning, system routing, TDM, pin
planning, placement, and FPGA routing. Every default engine must be editable
source in this repository and built from the repository root.

The default research target is a public VTR academic architecture. The same
provider-neutral artifacts support later ECP5 and UltraScale+ adapters.
Vivado is an optional UltraScale+ comparison/sign-off and bitstream backend;
it cannot satisfy the default open-flow completion gate. The current open
gates are additional architecture mapping profiles and post-placement timing
back-annotation. Public VTR TimingDB-to-OpenSTA translation and VPR detailed
routing are implemented. The authoritative machine-checked inventory is
`SOURCE_MANIFEST.json`.

The initial semantic envelope is intentionally narrow:

- one virtual DUT clock;
- synchronous reset;
- partition cuts only at register outputs or primary inputs;
- combinational strongly connected components remain within one FPGA;
- hard/vendor IP is represented as a fixed, indivisible macro;
- board links use deterministic static schedules;
- a global barrier completes before each virtual DUT clock-enable.

Multi-clock designs, runtime packet switching, partial reconfiguration, and
transparent encrypted-IP partitioning are later extensions. Controlled static
exact combinational cuts are an active opt-in extension. Characterization,
legacy depth-1/depth-2 and generalized assignment-derived partition legality,
native-route contract propagation, and dependency-
aware Phase 5 scheduling, Phase 6 exact boundary materialization, and event-
driven macro-cycle equivalence are implemented. Small LUT/FF models receive
complete one-step state/input enumeration, while larger models are honestly
qualified as multi-seed trace validation rather than proof. Phase 7C now binds
physical logic-segment evidence to the semantic contract and independently
checks source-ready and final-capture deadlines. A real routed DLA complete-
flow acceptance first exercised five naturally selected combinational cuts,
covered all 157,811 semantic segments with endpoint-exact routed evidence, and
independently reconstructed whole-design target-clock and virtual-runtime
WNS/TNS over all 195,532 original timing paths. A later historical three-arm
DLA + EDA 2023 case6 experiment exercised 102 generalized cuts at dependency
depth three and completed all physical/equivalence/deadline gates, but
regressed global target-clock WNS/TNS from -84.5812926868 ns /
-277276.1497366623 ns in sequential-only mode to -187.85581036 ns /
-548934.0510065886 ns. This proves complete accounting and causal correctness,
not timing-QoR improvement. The production default therefore remains
sequential-only; generalized v2 stays opt-in until a later cost model passes
the canonical no-regression promotion gate. Both exact policies retain the
explicit single-clock and fail-closed semantic scope.
Canonical Experiment v2 exact-mode evidence additionally requires at least one
independently reconstructed selected combinational cut. A legal zero-cut run
remains useful as a compatibility smoke but cannot satisfy this acceptance
gate. The capacity-limited `static_exact_acceptance` RTL/BoardDB pair is kept
separate from the benchmark registry and exists only to exercise the complete
open physical path without making a QoR claim.

## 2. Architectural layers

```text
RTL / IP / constraints
        |
        v
Global synthesis and EmuIR import
        |
        v
Sequential clustering and multi-resource partitioning
        |
        v
Board-level system routing
        |
        v
TDM scheduling and logical lane assignment
        |
        v
Per-FPGA netlist + transport RTL generation
        |
        v
Provider-selected technology mapping and mode-aware packing
        |
        v
Root-built VPR exact architecture packing
        |
        v
Root-built OpenPARF clustered placement
        |
        v
Root-built VPR routing-resource graph and detailed routing
        |
        v
Open routed physical artifact
        |
        +---- optional real-device backend / Vivado comparison and bitstream
```

The layers communicate through explicit, versioned artifacts rather than
sharing tool-internal data structures.

## 3. Core data models

### 3.1 EmuIR

EmuIR is the board-independent logical hypergraph. It preserves stable
hierarchical names, cell types, primitive parameters, directed net endpoints,
resource vectors, clock/reset classification, and partition-cut eligibility.

The checked-in Phase 1 representation is JSON for inspectability. A Cap'n Proto
encoding can be added later without changing the logical schema.

### 3.2 BoardDB

BoardDB describes FPGA nodes and board links. A virtual BoardDB may omit all
package pins while still providing topology, lane count, frequency, direction,
and latency. A hardware board support package later adds connector, bank,
package-pin, reference-clock, and shell-DCP bindings.

Logical lane assignment and physical pin binding are separate stages:

```text
cut signal -> board link -> TDM slot -> logical lane -> package pin
```

Only the final arrow requires a real board.

### 3.3 FPGA Interchange

FPGA Interchange is the single-FPGA physical boundary. It carries device
resources, the mapped logical netlist, placements, pin mappings, and routing.
It does not represent multi-FPGA topology or TDM and is therefore not the
system-level IR.

## 4. Transient full-flow artifact order

```text
global/design.emuir.json
  -> clustering/clusters.json
  -> partition/assignment.json
  -> system/routes.json
  -> system/tdm_schedule.json
  -> system/lane_map.json
  -> fpga_N/design.netlist + design.xdc
  -> fpga_N/packed.phys
  -> fpga_N/openparf/result.pl
  -> fpga_N/placed.phys
  -> fpga_N/routed.phys
  -> optional/fpga_N/routed.dcp
  -> optional/fpga_N/design.bit
```

These artifacts form the execution order inside one isolated complete run.
They are not a persistent checkpoint DAG: after Phase 7/7C validation, retain
only the compact terminal result summary and delete the Phase 1--7 payloads and
physical work trees. A new experiment starts a fresh complete run.

## 5. Implementation phases

### Phase 1 — Board-independent frontend (implemented)

Deliverables:

- EmuIR v1 model and validator;
- Virtual BoardDB v1 model and validator;
- Yosys JSON importer;
- provider-neutral LUT/FF plus hard-resource classification, with
  vendor-specific extensions isolated behind adapters;
- CLI and Phase 1 report;
- virtual `xcvu3p` two-FPGA platform;
- deterministic regression fixture and unit tests.

Acceptance:

- malformed IR/platform input is rejected with actionable errors;
- the example imports as four LUTs and four FFs;
- nets are classified as clock, reset, register-output, primary-input, or
  combinational;
- the design is compared with effective per-FPGA capacities;
- all tests run with Python 3.9 and no external packages.

### Phase 2 — Provider-neutral physical architecture and placement

The default open increment now implements:

- a C++17 VTR architecture XML importer;
- deterministic auto-layout expansion into ArchitectureDB;
- heterogeneous LUT, FF, carry, multiplier, memory, and I/O capacities;
- a provider-neutral Architecture TimingDB containing primitive and block
  arcs plus routing switches, segments, and directs;
- a pinned and SHA-256-verified public VTR flagship model; and
- independent architecture/timing validators.

The ArchitectureDB capacity policy takes the maximum primitive count across mutually
exclusive VTR modes. It is suitable for early global placement capacity, but
not exact packing legality by itself. The source-built VPR backend consumes
the original XML and performs exact mode-aware packing. A C++ importer now
publishes those clusters in a hash-bound contract without flattening mode,
pb-hierarchy, or atom-membership decisions. Those clusters are exported as
OpenPARF Bookshelf resources using exact VTR site capacities. OpenPARF
performs analytical placement and single-site min-cost-flow legalization; an
independent checker verifies completeness, compatibility, capacity, and
collisions before emitting VPR `.place`.

For the pinned public flagship profile, Yosys techmaps inferred multiplier
and synchronous RAM cells into the exact VTR model ports and legal modes.
VTR's bit-sliced RAM atoms remain visible until VPR packs them into a physical
memory block. ArchitectureDB dimensions are derived from VPR's auto-layout
placement header so the OpenPARF and VPR device views cannot silently diverge.
The `vpr fpga-open` orchestration command executes these contracts in order,
rejects stale output directories, and writes one aggregate report only after
all independent checks pass.

The earlier UltraScale+ risk spike remains an optional backend and implements:

- ArchitectureDB v1 and Placement v1;
- a hash-bound physical-region sidecar with exact SLR/clock-region coverage,
  package-specific I/O-bank inventory, and an independent checker;
- Vivado Site/BEL inventory to ArchitectureDB;
- EmuIR to OpenPARF Bookshelf;
- automatic execution of root-built OpenPARF;
- OpenPARF `x/y/z` result to legal UltraScale+ Site/BEL placement;
- one-instance/one-BEL, compatibility, completeness, and collision checks;
- LOC/BEL XDC generation;
- a real `xcvu3p` Vivado DCP/route validation harness.

The initial compatibility policy exposes only `*6LUT` and primary `*FF` BELs.
This is intentionally conservative: it avoids accepting a placement that
requires LUT input sharing or control-set repair that the flow does not yet
implement.

The optional UltraScale+ acceptance target remains in progress. It uses public
`xcvu3p` FPGA Interchange collateral to implement:

- DeviceResources to cached ArchitectureDB;
- fixed IO/clock/macro placement import;
- detailed pin mapping and intra-site routing repair;
- placed physical-netlist validation;
- RapidWright conversion to placed DCP;
- Vivado routing baseline.

Acceptance:

- an existing mapped `xcvu3p` design survives
  `FPGAIF -> OpenPARF -> FPGAIF -> DCP`;
- all cells have legal Site/BEL assignments;
- Vivado can route the placement without invoking its placer;
- name and logical/physical pin mappings remain consistent.

This phase is deliberately early because site packing, BEL pin permutation,
and intra-site routing are the largest physical-backend integration risks.

### Phase 3 — Sequential clustering and partitioning (implemented)

Implement:

- combinational SCC detection;
- sequential-cone clustering;
- carry, DSP, BRAM/URAM cascade grouping;
- fixed/group constraints for hard IP and board shells;
- TritonPart adapter with multi-dimensional resource weights;
- in-tree RePart multilevel partitioning with legal LUT replication;
- BoardDB-derived candidate FPGA domains, native topology-constrained FM
  refinement, and utilization headroom.

Acceptance:

- every primitive belongs to exactly one partition;
- all group and fixed constraints hold;
- every selected combinational cut satisfies the sealed Static Exact
  dependency and segment-deadline contract;
- every FPGA satisfies its effective resource capacities;
- every routed cut endpoint satisfies `max_route_hops` before Phase 4;
- cut and timing metrics are reproducible for a fixed seed.

Generalized Static Exact v2 plus endpoint-exact PATRON is the default Phase 3
configuration. EmuFlow exports each legality-preserving cluster as a
multi-resource hypergraph vertex, obtains a TritonPart initial assignment, and
then lets PATRON refine it using timing endpoints, BoardDB topology, routing
pressure, and the transported classes declared by the Static Exact policy.
The common assignment builder and independent checker reconstruct the selected
dependency DAG after refinement. TritonPart-only, sequential-only, and greedy
providers remain explicit A/B policies.

The production cycle-correct runtime contract transports register boundaries
and dependency-qualified generalized Static Exact combinational boundaries.
Nets outside those classes are unioned into semantic atomic clusters, so an RTL
design may contain a cluster larger than the requested per-partition balance
target. Phase 3 records both requested and effective balance and never
presents an automatically relaxed result as a strictly balanced one.
Multilevel partitioning cannot
recover freedom that semantic clustering removed.

The EmuIR-semantic-bound
`emuflow.combinational-cut-characterization/v1` report. It reconstructs the
complete EmuIR combinational graph, detects SCCs/self-loops, permits only a
conservative single-driver LUT-only potential-cut set, retains reconvergent
predecessors, and reports depth-1/depth-2 theoretical component splitting.
Its independent validator regenerates the whole report. The production
`--cut-mode static-exact-combinational` Phase 3 path releases all structurally
eligible nets, reconstructs the selected acyclic dependency graph after
partitioning, enforces the configured positive safety cap, emits
`emuflow.static-exact-combinational-cut/v3`, and independently reconstructs
both clusters and contract. Its qualification is structural partition
legality, not schedule feasibility. Phase 4 uses the ordinary multicast router
and binds every original timing path to an ordered sink branch, cut identity,
demand, and routed hop. Phase 5 uses the ordinary timing-DAG ratio/lane/slot
pipeline. For sampled virtual wires it adds source readiness, predecessor
arrival, settle, and final capture/commit constraints to that same solver and
emits an independently reconstructed
`emuflow.sampled-virtual-wire-schedule-certificate/v1`. Timing-aware routing,
ratio optimization, feedback, and slot refinement remain available; Static
Exact does not select a separate Phase 5 provider.

All sampled-virtual-wire stages share the
`tx-sample-before-rx-shadow-update-v1` convention: TX samples the pre-update
value on its assigned edge, RX shadow capture occurs on the edge labelled by
`arrival_slot`, a budget `B` makes the value available at edge `arrival+B`, and
virtual-DUT commit occurs on the `frame_slots-1` edge. The
versioned semantic contract and staged acceptance plan are defined in
`docs/STATIC_EXACT_COMBINATIONAL_CUT.md`. No exact-mode provider may be
promoted until the implemented Phase 5 source-readiness/final-capture gate is
followed by Phase 6 one-macro-step equivalence and Phase 7C routed segment
deadlines plus whole-design target/runtime WNS/TNS.

The RePart replication provider adds a versioned replication artifact without
changing the unique-owner primary assignment. A C++-kernel replicability mask
prevents stateful or unsupported vertices from entering the replication move
queue. Independent checks prove mapped LUT-only, acyclic, fanin-closed replica
clusters; charge every copy to target-FPGA capacity; recompute effective cut
demands; and require Phase 6 to materialize and cycle-check the copies.

When route constraints contain `max_route_hops`, Phase 3 computes directed
shortest-hop distances from BoardDB. The greedy baseline restricts candidate
FPGA domains during initial assignment. A common source-built C++17
`topology-constrained-fm-v1` pass then audits or refines the output of every
provider with a feasibility-first objective: unreachable pairs, hop-limit
violations, weighted excess hops, weighted hops, then cut cost. Moves and
pairwise swaps preserve fixed clusters, hard capacity, independently checked
multi-resource balance, and minimum partition use; small instances also have
an exact fallback. Python independently reconstructs cut-net source/sink hop
legality. Illegal inputs above 50,000 clusters stop at an explicit scale gate
instead of entering the current quadratic repair search; the topology-aware
constructive provider is required at that scale until multilevel candidate
propagation is implemented. This increment is informed by TopoPart and the
DATE-2024 inter-block-constraint formulation. It is not a faithful reproduction of
TopoPart, MaPart, MFSPart, or HoPart; topology-aware multilevel propagation
and paper-level ablations remain planned.

TritonPart search effort is explicit in both its Tcl and provider metadata.
The default remains upstream's 50 initial/10 retained solutions; bounded
validation runs may select smaller values through the common Phase-3,
multi-FPGA compile, and cross-stage interfaces.

Vivado/OpenSTA-derived timing weights, topology-aware repartition feedback,
and resource-specific heterogeneous FPGA capacity ratios are QoR extensions.

### Phase 4 — Board-level route/TDM co-optimization

The source-built C++ router operates over BoardDB with:

- unicast paths and multicast trees;
- per-direction link capacity;
- link latency and unavailable-link constraints;
- rip-up/reroute with historical congestion;
- infeasibility diagnostics.

Acceptance:

- every cut net reaches all sinks;
- no link exceeds modeled capacity;
- route trees contain no cycles;
- the checker independently reconstructs link utilization.

For the opt-in static-exact depth-1/depth-2 path, Phase 4 deliberately permits
only the native route tree, either timing-oblivious or with post-route timing
annotation. The route artifact retains the complete
Phase 3 semantic contract and its canonical SHA-256, while each route repeats
the cut class, dependency level, predecessor-cut set, and combinational depth.
The standalone validator cross-checks all of those fields against the
assignment. Timing-aware and feedback providers remain unavailable until
their objectives consume dependency readiness rather than treating cut nets
as simultaneously launchable.

The `native-load-balanced-v1` mode uses the same C++ kernel without requiring
STA input. Four-FPGA diamond, multicast, unavailable-link,
infeasible-capacity, and half-duplex tests cover non-trivial topology cases.

The comparison `timing-aware-load-balanced-v1` provider adds an in-tree C++17
TLR/TRR kernel based on the routing portion of Chen et al., ASP-DAC 2026. Its
versioned `emuflow.sta-paths/v1` input carries clock domain, period, slack,
fixed delay, ordered cut signature, and cut-net sequence. The adapter:

- normalizes slack across clock domains using the paper's definition;
- losslessly compresses paths only when clock normalization and ordered cut
  behavior are equivalent, retaining the largest-fixed-delay representative;
- derives a fixed predicted delay table from BoardDB or explicit per-link
  overrides;
- distinguishes cable and SLL-class links; and
- represents flexible shared physical direction groups as half-duplex
  BoardDB links.

The C++ kernel applies majority-flow direction locking, timing-aware demand
ordering, criticality/utilization/history-weighted Dijkstra multicast trees,
negotiated congestion, and worst-path selective rip-up/reroute with
accept/rollback. Python does not reproduce the optimization. Its independent
checker reconstructs every tree, capacity domain, direction lock, route
delay, compressed-path signature, slack, and normalized slack from the
returned artifact.

The historical `timing-aware-route-tdm-cooptimized-v1` provider extends that
kernel with the routing/TDM coupling used in the DAC 2020 and ASP-DAC 2021
co-optimization formulations. The default timing-enabled path now exposes all
checked tree columns to the `timing-aware-global-candidate-v1` restricted
master, followed by the ASP-DAC 2026 timing-DAG Phase 5 optimizer and the same
concrete lane/slot legalization. The historical provider and path-Lagrangian
Phase 5 optimizer remain explicit rollback and A/B options. The Phase 4
checker independently reconstructs the proxy; the Phase 5 checkers remain the
exact acceptance gate.

For real STA input, `emuflow sta emit-vivado-cut-map` produces a lossless
UTF-8-hex map from stable EmuIR cut-net IDs to the deterministic
`__emuflow_net_<index>` names in emitted mapped Verilog.
`scripts/vivado/export_cut_timing_paths.tcl` queries Vivado timing-path
objects that traverse those nets and exports clock group, requirement, slack,
data-path delay, and exact cut-net membership. `emuflow sta
import-vivado-tsv` converts that result to `emuflow.sta-paths/v1`; no
human-readable timing-report scraping or heuristic name matching is used.

The route/TDM provider is selected automatically when a timing-path artifact
is supplied. With no STA artifact, `native-load-balanced-v1` runs the same
source-built C++ kernel without timing criticalities.

### Phase 3--5 checked feedback loop

`emuflow cross-stage optimize` treats partitioning, route/TDM
co-optimization, and exact TDM scheduling as one candidate transaction. The
incumbent schedule is converted into checked channel-pressure net weights,
the source-built partitioner generates a new assignment, and the frozen
partition-independent STA database is projected onto that assignment before
the Phase 4 and Phase 5 kernels run.

The optimizer accepts the same `--board-link-timing-db` input as the main
compile command. It overlays direction-exact bounds onto every candidate's
Phase 4/5 constraints, uses them in the scheduled-path objective and feedback,
and records a self-contained database/constraint artifact pair that the report
checker independently reconstructs.

The main `multi-fpga compile` orchestration exposes this loop through
`--cross-stage-iterations`. It promotes the accepted candidate into the
canonical Phase 3--5 artifacts and continues that exact candidate through
Phase 6 netlist/transport generation, the selected physical backend, and
Phase 7C unified system timing. The aggregate validator cross-checks the
selected candidate identity and all three independent stage validations before
the run can pass.
Feedback trials inherit the initial Phase 3 TritonPart seed-sweep and checked
minimum-used-FPGA/multi-resource balance-repair policy. A trial therefore is
not rejected merely because the outer loop silently omitted the legalization
policy used to create its incumbent.
Partition migration is reported both literally and after maximum-overlap label
alignment. Alignment is restricted to exact automorphisms preserving FPGA
resources, link topology, connector roles, availability/capacity classes, and
direction-specific link delay. It is diagnostic only and never rewrites the
selected assignment.
The same exact symmetry group defines a canonical partition class for
fixed-point and cycle detection. A repeated class is still routed and scheduled
once, so heuristic QoR differences remain eligible for acceptance, but it does
not trigger another redundant feedback iteration.

Acceptance uses the concrete schedule rather than the analytical routing
proxy. The candidate checker reconstructs transport delay for every path in
the global database; paths with no candidate cut nets remain in the objective
with their fixed delay. A lexicographically worse, tied, infeasible, or
unchecked candidate is rolled back. The report checker reconstructs all
successful candidates and replays the acceptance sequence. Raw channel
feedback is not applied as an uncontrolled jump: log-space proximal damping
and deterministic descending backtracking generate positive, reproducible
trial weights between the unweighted and full-feedback objectives.

The throughput-first mode additionally performs an exact checked frame search
for every candidate partition. Its primary objective is the minimum feasible
frame length, which directly determines the nominal pausible-clock emulation
frequency. The next objective is scheduled-path margin against that virtual
clock period. Original RTL clock-domain slack is retained for criticality and
diagnostics, but cannot reject an otherwise closed virtual-runtime candidate
or justify a slower frame. The checker requires both a feasible upper bound
and an explicitly infeasible frame immediately below the selected minimum.

### Phase 5 — TDM scheduling and cycle-accurate transport (scheduling increment implemented)

Implemented model:

- selectable continuous ratio providers: the established path-Lagrangian/KKT
  solver and an equation-level ASP-DAC 2026 timing-DAG solver;
- a checked clock/protocol compatibility artifact: the default global-frame
  CDC class may multiplex different STA clocks, while explicit transport
  domains cannot share a physical lane;
- time-expanded links with fixed legal ratio/lane groups;
- C++ timing-path-guided deterministic concrete-slot LNS;
- optional OR-Tools CP-SAT medium-case oracle with independent certificate
  reconstruction;
- exhaustive multi-round small-instance oracle for optimality comparison;
- static schedule ROM generation;
- TX/RX, shadow-register, barrier, and virtual-clock-enable RTL;
- multi-partition event and generated-SystemVerilog simulation.

Acceptance:

- no lane/slot collision;
- all multi-hop precedence constraints hold;
- every frame completes before the virtual clock-enable;
- partitioned and unpartitioned designs are cycle-equivalent.

The first three items also enforce sampled virtual-wire constraints. The sole
production generalized policy derives the actual dependency DAG from the
selected assignment and accepts any positive safety cap. The unified TDM
scheduler uses the shared `tx-sample-before-rx-shadow-update-v1` convention,
computes launch-to-TX,
RX-to-TX, and RX-to-capture readiness from the Phase 3 contract, and stops
with a precise fixed-frame infeasibility diagnostic when any arrival or
capture cannot precede commit. The builder emits a versioned dependency
certificate; the standalone checker reconstructs readiness, arrivals,
collisions, captures, metrics, and the certificate without calling the
scheduler's readiness/capture routines. Phase 6 independently reconstructs
the contract-bound split, preserved boundary identities, shadow-only consumer
connectivity, reset/startup policy, and event-driven macro-cycle behavior.
Three deterministic random traces are required for every run. Small LUT/FF
models also receive complete one-step state/input enumeration; a checked-in
Yosys formal miter is a canonical regression and does not by itself claim
arbitrary-design formal closure.

The initial dependency-aware list schedule is refined by an in-tree C++ engine.
It exhaustively reorders bounded neighborhoods containing a delayed worst-path
hop and up to three preceding lane blockers, accepting an order only when
independently reconstructed worst normalized slack, completion, or total wait
improves lexicographically. The exact exhaustive oracle covers fixed ratio/lane
assignments across multiple transport rounds, including multicast-tree
precedence, link latency, ratio windows, lane collision, global round barriers,
and the reserved runtime-barrier slot. It is deliberately limited to small
instances. The optional time-expanded CP-SAT oracle extends optimality checks
to medium cases; it is also a correctness/QoR reference, not the scalable
production solver.

The ratio legalizer's round split is explicitly a capacity-only estimate; it
does not claim to include multicast-tree precedence. Concrete scheduling
records the realized inter-round source-ready slot, its shift from that
estimate, and checks the resulting precedence, ratio windows, collisions, and
final runtime-barrier reservation. The C++ slot refiner is constrained by this
realized barrier rather than by the relaxed capacity estimate.

The timing-DAG provider builds an exact prefix DAG from ordered compressed STA
paths. Its C++ continuous core implements ASP-DAC 2026 Eqs. 8, 13, 15--17,
19, and 20: forward arrival propagation, normalized path duals, reverse
delay-cost flow, per-domain KKT updates, and residual-capacity scaling. The
paper does not disclose every numerical stabilization detail, so the provider
records the explicit equation set and separately identifies its multiplicative
path-dual update. Python independently reconstructs DAG path delays, path-dual
normalization, Eq. 16/17 flow, capacity, and the final worst delay. The same
TODAES-style discrete legalizer and schedule checker are used for both
continuous providers, enabling controlled A/B comparison.

For large routed designs, Phase 5 constructs the route/hop/timing model once
per invocation and shares that immutable model across continuous optimization,
discrete legalization, concrete scheduling, candidate comparison, and the
in-process checker. Capacity-direction and routed-net membership indices are
built in single passes rather than rescanning all hops or nets for every
domain or timing path. The separately invoked validation command still
rebuilds the model from serialized artifacts, preserving an independent
post-run acceptance gate.

Multi-round ratio legalization also avoids frame-size and candidate-count
cross products. The exact capacity-split boundary is evaluated from the
monotone quotient intervals of the two round occupancies with per-domain
difference arrays. Promotion candidates use incremental domain-capacity
deltas and pre-index each bucket's affected timing paths and delay slopes;
unaffected-path slack remains part of the exact candidate score. These
indices change only execution cost, while the serialized plan is still
accepted by the independent ratio and concrete-schedule checkers.

The one-command flow can also treat `frame_slots` as a feasible upper bound
and run checked monotone bisection across the actual Phase 4/5 providers. Each
candidate must pass routing capacity, ratio legalization, concrete schedule,
transport simulation, and pausible-clock barrier validation. Phase 7C then
reports the selected frame's virtual DUT frequency separately from the
original RTL clock-domain path slack.

The joint G6 cycle-equivalence gate is now closed for the mapped PicoRV32
LUT/FF primitive envelope by the Phase 6 split and shadow-endpoint model.

### Phase 6 — Per-FPGA netlist generation and lane/pin planning (board-independent increment implemented)

Implement:

- logical-netlist splitting;
- transport endpoint insertion;
- logical lane assignment;
- virtual IO-region anchoring;
- hardware BSP schema;
- package pin/bank/IOSTANDARD/GT-quad solver;
- XDC generation.

Acceptance:

- every cut endpoint is connected to one generated transport endpoint;
- logical lane maps agree at both ends of each link;
- hardware BSP pin constraints pass an independent electrical-rule checker.

The board-independent increment is implemented with versioned lookahead
position hints, placement-aware pin plans, per-FPGA netlists,
transport-endpoint maps, logical lane maps, virtual anchors, manifests, and
reports. Its root-built C++17 planner constructs minimum-count
ratio-homogeneous TDM groups, refines a placement-region/dispersion objective,
and performs exact group-to-virtual-pin matching. The independent checker
reconstructs group legality and lane/slot occupancy before Phase 6 generates
schedule-specific mux/capture RTL and rechecks exact instance coverage,
endpoint agreement, and mapped cycle behavior.

For the Chimew provider, the source-qualified electrical certificate is a
mandatory Phase 6 input rather than an advisory side artifact. The independent
validator binds its concrete lanes, pins, banks, direction, electrical standard,
voltage, and source hashes to the accepted pin plan and schedule; the validated
certificate is then carried by the split manifest.

Package-pin, bank, IOSTANDARD, direction, connector, frequency, and skew
binding is implemented as the Phase 6B source-complete min-cost-flow
increment. It is exercised against an explicit synthetic UltraScale+ BSP and
emits per-FPGA XDC with a synthetic-use warning. Reference-clock, differential
pair/GT specialization, real connector data, and vendor electrical/timing DRC
still require a concrete board-revision-controlled BSP; synthetic validation
is not hardware closure.

### Phase 7 — Integrated open placement and routing

Implement source-complete provider interfaces for:

- a selected open detailed FPGA router;
- an openly reproducible UltraScale+ device-resource/timing model;
- optional Vivado route/DRC/timing comparison;
- optional vendor-assisted bitstream generation.

Acceptance:

- a clean checkout builds the selected placer and router from repository
  source using the root build;
- the default runner invokes only those local build products;
- all per-FPGA designs place and route without a proprietary implementation
  tool;
- the independent checker accepts placement and routing;
- setup/hold and board-interface timing are reported separately;
- reproducible QoR reports include placement, route, TDM, and emulation speed.

The Phase 7A artifact adapters, automatic OpenPARF runner, packed-cluster
handoff, and independent placement checker are implemented. The default
runner resolves only the OpenPARF product compiled by the root build.
External placement files and
installations are comparison-only providers and cannot satisfy the release
gate.

OpenPARF's optional experimental router is not the selected detailed-routing
provider because its upstream build requires proprietary GUROBI. Its source is
retained for provenance, but it is excluded from the open default build.

The VPR detailed-routing result is independently checked against VPR's
exported RR graph. The in-tree C++ checker validates route-node identity,
coordinates and PTC, exact edge/switch connectivity, tree-branch restarts,
cross-net resource capacity, packed-net/sink coverage, and the placement
artifact hash. This is separate from VPR's internal route consistency check.
Per-FPGA physical pipelines may execute concurrently because they consume
read-only common inputs and write disjoint output trees. The coordinator
collects every result before emitting summaries and restores BoardDB FPGA
order, so worker completion order cannot change report content.

Physical IO-net preservation, routed DCP validation, timing, and bitstream
generation remain separate gates and are not implied by the placement gate.

Phase 7B emits complete structural primitive Verilog for merged partitions.
Applying placements in Vivado is retained only as optional cross-validation,
not as evidence that the open physical backend is complete.

Phase 7C integrates one lockstep frame controller per transport and closes the
current pausible-clock timing contract. Phase 6 emits a versioned
`boundary-identity/v1` database that binds every scheduled hop's TX/RX endpoint
to its external port bit, merged physical net, DUT source, or transport shadow
register. A physical backend can return `boundary-timing/v1` measurements under
those stable endpoint IDs. The versioned `system-timing/v2` artifact then
reconstructs every timing-aware route from the concrete lane/slot schedule and
combines per-hop routed endpoint delay, board-link/TDM delay, and the post-route
DUT logic bound. It also consumes routed endpoint-exact measurements for every
same-FPGA original TimingPathDB path.  The local and expanded cross-FPGA path
ID sets must be disjoint and their canonical union must exactly match the
sealed source database before the result is called whole-design/global. It
reports original-target-clock and virtual-runtime-clock slack separately.

Board-link delay is supplied through `board-link-timing/v1`, with one record
per legal directed BoardDB arc. The contract preserves the functional
`latency_cycles` used to generate the schedule and transport RTL while allowing
the nanosecond upper bound to advance independently from `model-only` to
vendor-characterized and finally hardware-measured evidence. A cycle-count
change invalidates the existing schedule rather than silently altering only
the final timing report. The main timing-driven compile accepts this database
as an explicit input and projects its bounds into the Phase 4 route cost and
Phase 5 TDM objective/reconstruction before retaining the original directed
records for Phase 7C. The route constraints and both C++ providers preserve
the full `(link, source, sink)` identity, so asymmetric full-duplex link bounds
remain direction-exact through optimization and final timing.

`system-route-constraints/v1` also carries an optional positive
`max_route_hops`. When present, Phase 3 first enforces the same bound on
partition cut endpoints. The native C++ router then performs hop-bounded
source-to-sink search and falls back from an over-depth multicast candidate to
a legal bounded tree. The independent route checker recomputes tree depth and
rejects any violation. The EDA 2024 BoardDB materializer emits this companion
constraint directly from the public case's maximum-hop field.

When the observation-only `shadow_values` top port is removed, Phase 6 keeps
the shadow-register net's internal sinks. This preserves the registered
RX-to-next-hop-TX connection on routing-only FPGAs instead of allowing physical
synthesis to prune a required multi-hop transport register.

The Vivado provider extracts each routed TX source-to-port and RX
port-to-shadow-register path through Tcl. TX lookup is anchored at its stable
output-port bit: the adapter can recover a routed net renamed by synthesis and
constrain a path through a combinational driver that is not itself a legal
timing startpoint. The open provider resolves the same stable endpoints to VPR
atom pins and evaluates their longest routed delays in the Tatum timing graph.
Both therefore supply endpoint-exact interface delay through
`boundary-timing/v1`.

Those stable endpoints also feed a checked optional optimization loop.
`physical-route-feedback/v1` reconstructs every scheduled TX/RX boundary
measurement, aggregates delay onto the exact directed capacity domain, and
combines it with the concrete Phase 5 occupancy/wait price. A repeated global-
candidate Phase 4 run must provide the original routes, schedule, runtime,
physical summary, and feedback artifact; all are revalidated before the C++
router receives a price. This allows post-route lane/interface delay to
influence the next route/TDM iteration while retaining complete Phase 7
WNS/TNS as the non-decomposed QoR gate.

For DUT logic, both physical providers expand the original STA members behind
each compressed cross-FPGA path. A complete member is represented by routed
`launch -> TX`, zero or more `RX -> next TX`, and `final RX -> capture`
segments. VPR evaluates these point-to-point paths with routed Tatum edge
delays; Vivado evaluates the same stable endpoint queries in the routed device
checkpoint. For inferred synchronous RAM, the Vivado adapter retains the
physical RAMB clock launch object while recovering the exact logical output
bit from EmuIR net identity. Phase 7C uses the resulting
`logic-segment-timing/v1` measurements and replaces the matching TX interface
terms instead of adding a whole partition's critical-path maximum. Exact and
fallback path counts are part of `system-timing/v2`. When a coarse
provider-neutral hard-macro arc has no corresponding vendor timing arc, the
Vivado provider constrains the worst physical path through the preserved
cut-net driver and records a distinct cut-net-cone upper bound rather than
claiming endpoint exactness. Structured TimingPathDB
endpoints retain the actual sink of each multicast member and remove local
fanout of globally cut nets, while
legacy or unmapped endpoints remain conservative.

Hardware BSP pin binding, source-synchronous board timing, dedicated
clock-buffer binding, bitstream generation, link training, and a golden
hardware workload remain later gates.

Phase 7D seals that result with a versioned release manifest. It rehashes the
pinned RTL and critical artifacts, cross-checks every boundary from partition
counts through routed timing, and records explicit G0-G9 evidence.

### Phase 8 — Open synthesis/packing completion and hardware bring-up

Replace bootstrap mapped DCPs with:

```text
Yosys synth_xilinx -family xcup
  -> UltraScale+ site packer
  -> FPGA Interchange logical/partial physical netlist
```

Add real-board link training, PRBS, deskew, barrier diagnostics, host control,
and golden-workload testing.

Phase 8A is now implemented as the board-independent readiness increment. It
seals a versioned hardware-BSP requirements artifact from the G0-G9 release,
Phase 6 anchors, and virtual BoardDB. It expands physical lane endpoints,
clock/link-channel bindings, per-FPGA bitstream slots, and pending G10 checks,
then independently reconstructs and byte-reproduces the result. It explicitly
reports `awaiting_hardware_bsp` and does not claim G10.

Phase 8B begins after a board is selected: validate a hardware BoardDB/BSP,
bind package pins/banks/IOSTANDARDs and clocking, apply board IO timing, and
generate the first checked bitstreams. Hardware PRBS/training and a golden
workload remain the following G10 increments.

## 6. Provider interfaces

Algorithms are replaceable providers:

```text
SynthesisProvider
PartitionProvider
SystemRouterProvider
TdmSchedulerProvider
PinAssignmentProvider
PlacementProvider
FpgaRouterProvider
BitstreamProvider
```

Provider inputs and outputs are versioned artifacts. Tool-specific log parsing
must not leak into EmuIR or BoardDB.

Open providers are in-tree source components. Yosys/ABC, OpenPARF,
OpenROAD/TritonPart, and RePart source, upstream licenses, exact revision
provenance, and EmuFlow modifications live under `engines/`. The root
CMake build compiles them with the native EmuFlow engines. A compiled
executable is only a local build artifact, never the published implementation.
The default runtime resolver deliberately does not search `PATH`; it selects
the products of this monorepo build.

Presence of source alone is insufficient. A stage is complete only when its
root build target, automatic runner, versioned artifact contract, independent
checker, and clean-checkout end-to-end test all pass. The source manifest
distinguishes `default-in-tree-build` from `source-present-*-pending`; pending
components may not be described as implemented.

Python is the control plane and independent reference/checking layer.
Performance-critical production providers use native C++/CUDA implementations.
The process boundary keeps the GPL-licensed RePart program separate from the
Apache-licensed EmuFlow control plane, while both implementations remain
visible and buildable in the same repository.

## 7. Verification strategy

| Boundary | Required verification |
| --- | --- |
| RTL -> mapped netlist | equivalence and resource report |
| Yosys JSON -> EmuIR | schema, endpoints, resource classifier |
| EmuIR -> partitions | capacity, grouping, cut legality |
| partitions -> TDM RTL | cycle equivalence |
| BoardDB -> route/schedule | independent capacity and collision checker |
| FPGAIF -> OpenPARF -> FPGAIF | name, BEL, pin-map, site-route legality |
| placed -> routed DCP | CheckPhysNetlist, route status, DRC |
| routed DCP -> bitstream | timing summary and vendor bitstream checks |
| bitstream -> hardware | PRBS, link training, barrier, golden workload |

## 8. Current reference configuration

Until a board is selected:

- architecture: pinned VTR flagship heterogeneous 40 nm academic model;
- virtual device: scalable auto layout, initially 64 by 64;
- default virtual platform: academic VTR-class two-FPGA point-to-point;
- per-FPGA utilization limit: 75%;
- logical link: 32 lanes per direction at 250 MHz;
- modeled link latency: two fabric cycles;
- physical mode: out-of-context, no package-pin binding;
- placement provider: root-built in-tree OpenPARF;
- routing provider: root-built in-tree VTR/VPR;
- current placement/routing bridge: the VPR packed-cluster contract,
  OpenPARF clustered placement, checked VPR `.place` emission, and VPR
  detailed routing work for the heterogeneous VTR flagship backend;
- multi-FPGA entry point: `emuflow multi-fpga compile`, with a hash-bound
  report through per-FPGA split and transport generation; optional
  cross-stage Phase 3--5 optimization can continue its selected candidate
  through physical implementation and Phase 7C;
- per-FPGA open physical entry point: `emuflow vpr fpga-open`;
- optional real-device backend: UltraScale+/Vivado.

The device capacities in the virtual platform are planning values. Phase 2 will
replace them with values derived from the selected FPGA Interchange
DeviceResources file.
