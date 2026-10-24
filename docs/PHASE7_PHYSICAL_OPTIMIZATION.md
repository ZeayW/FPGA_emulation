# Phase 7 physical-optimization gap and implementation plan

## Finding

The open Xilinx backend currently has a material implementation gap between
technology mapping and detailed routing.  Its flow is:

1. map the split DUT and synthesize the generated transport logic;
2. insert topology-only buffer trees for high-fanout transport nets;
3. export fixed atoms and connectivity to OpenPARF;
4. run analytical placement with high-fanout weights and RUDY/pin-density area
   inflation;
5. run DirectLG and detailed placement;
6. validate site/BEL legality and pass the unchanged physical netlist to
   RWRoute.

The flow does not currently perform placement-aware netlist optimization,
placement-aware repacking, site-pin optimization, or a trial-route feedback
loop.  High-fanout weighting and area inflation can move cells, but cannot
change which driver serves a load, create a local driver, or repair a packed
slice whose external pin demand is intrinsically difficult to route.

This differs from commercial implementation flows.  Vivado's documented flow
contains logic optimization, placement, physical synthesis, detailed
placement, post-placement `phys_opt_design`, routing, and optional post-route
physical optimization.  Its physical-synthesis phase uses the preliminary
placement to perform high-fanout and critical-cell replication, control-set
optimization, retiming, SLR-crossing optimization, and equivalent-driver load
rewiring.  Vivado explicitly states that placement-aware replication avoids
the congestion that blind pre-placement replication can introduce.  See the
[Vivado implementation flow](https://docs.amd.com/r/2024.2-English/ug904-vivado-implementation/Implementing-the-Design),
[physical synthesis phase](https://docs.amd.com/r/2024.2-English/ug904-vivado-implementation/Physical-Synthesis-Phase),
and [`phys_opt_design` reference](https://docs.amd.com/r/2024.2-English/ug835-vivado-tcl-commands/phys_opt_design).

Quartus follows the same principle: its Fitter can duplicate registers using
placement information, and its documentation warns that maximum-fanout
duplication without controlling which sinks are assigned to each copy can
leave every copy driving loads spread across the device.  See
[Quartus manual register duplication](https://www.intel.com/content/www/us/en/docs/programmable/683641/22-3/manual-register-duplication.html)
and
[fan-out control by physical proximity](https://www.intel.com/content/www/us/en/docs/programmable/683641/23-2/duplicate-registers-for-fan-out-control.html).

## What the current components can and cannot provide

| Component | Useful capability | Missing capability for this problem |
| --- | --- | --- |
| OpenPARF | analytical placement, density optimization, RUDY/pin-density area inflation, legalization, detailed placement | no placement-aware DUT driver replication or equivalent-driver rewiring in the current integration; no exact RWRoute-resource feedback |
| DirectLG | assigns atoms to legal sites/BELs | does not change logical connectivity or repack from negotiated-routing feedback |
| RWRoute | exact UltraScale+ routing-resource graph and negotiated-congestion routing | router, not a physical-synthesis engine; the current adapter does not mutate the netlist or placement between attempts |
| RapidWright APIs | exact device, EDIF, cell, net, placement, and routing objects; enough primitives to create cells and nets | no turnkey equivalent of Vivado `phys_opt_design` |
| VPR/VTR | integrated primitive-level packing and placement plus Pathfinder routing | generic architecture flow; it is useful as an algorithm reference, not a drop-in XCVU19P implementation/signoff replacement |

RapidWright is nevertheless the best implementation substrate for this
backend.  Its public `Design` API can create logical/physical cells and nets,
and its EDIF APIs expose the connectivity needed for replication and rewiring.
See the [RapidWright `Design` API](https://www.rapidwright.io/javadoc/com/xilinx/rapidwright/design/Design.html)
and [EDIF APIs](https://www.rapidwright.io/javadoc/com/xilinx/rapidwright/edif/EDIFTools.html).

VTR's current default analytical flow jointly optimizes primitive packing and
placement before routing.  That is the architectural lesson to borrow: do not
freeze packing before enough physical information is available.  See the
[VPR basic flow](https://docs.verilogtorouting.org/en/latest/vpr/basic_flow/)
and [analytical-placement options](https://docs.verilogtorouting.org/en/latest/vpr/command_line_usage/).

## Route feedback and explicit transport qualification

The RWRoute adapter ranks the sixteen logical nets touching the most overused
nodes in each iteration. It records connection uses, shared excess exposure,
local/short-wire/pin-access contributions, SLR distribution, fanout, HPWL, and
source. Ownership comes directly from the router's route-node user maps.

`rewire_transport_buffers` in `src/emuflow/transport_physical_opt.py` is an
explicit qualification API. It consumes an immutable compact transport EmuIR
and a validated preliminary physical handoff. It groups existing leaf LUT1
buffers only when their truth tables prove identity and their input net is
identical. It reassigns sinks with a deterministic capacity-constrained spatial
heuristic, alternating capacitated assignment with median recentering. Farthest
point seeding escapes coincident preliminary buffer positions. It retains the
original assignment unless within-group Manhattan distance strictly decreases,
measured against optimally recentered original groups. Returned median centers
are guidance for fresh placement, not legal site assignments. Every branch keeps
its original fanout; no resource is added and all endpoint fields are preserved.
Distances and centers use the physical slice X/Y lattice rather than potentially
anisotropic architecture coordinates. Non-slice sinks skip their entire group. Missing coordinates
skip the whole group. Each move is recorded with the original and new net
ordinals, input identities, and source/output hashes.

The transform publishes a separate candidate transport EmuIR atomically. It
independently compares every non-endpoint table, every driver and non-leaf
endpoint, each branch's fanout, and the sink multiset projected through proven
equivalent identity LUT1s. This comparison reads the two circuits without
trusting the optimizer's move report and rejects changed truth tables, lost or
duplicated sinks, changed drivers, and branch dependencies before publication. It
must subsequently pass normal lowering, remapping, placement, and routing;
editing a frozen route-input TSV or rebinding an old certificate is not
permitted. The distance improvement is a placement predictor and cannot be
reported as proven routing improvement. This API is not enabled automatically
in the production flow until a complete physical run qualifies it.

## Relevant academic results

The explicit `depopulate_hot_lut_sites` API adds a post-DP experiment for
selective packing depopulation. It streams atom identity and the complete
qualified LUT site domain to `emuflow_xilinx_lut_depopulation`. The C++ mover
selects hot sites by joining their physical site identities to the source-sealed
ArchitectureDB. Its explicit `architecture_path` must match the name map's
architecture SHA-256; region metadata is not stored in the normalized site
database or name-map coordinate header. Solver region reindexing does not
affect this join. The mover
preserves all FFs, hard blocks, fixed atoms, and macro-bearing sites. Only
singleton LUT1--LUT6 atoms in caller-selected hot clock regions can move to
nearby free qualified BELs. The default search target is six LUTs per slice
with a maximum displacement of 128 native-grid Manhattan units. Both are
explicit experiment parameters, not device capacities or signoff limits.

The mover records unrelieved excess when the bounded search cannot find a
destination; it never drops cells or changes nets, and it does not claim
routability. Publication uses a separate candidate placement. Normal exact
site/BEL/control-set/carry/macro validation must certify that new placement
before physical handoff and routing. This experiment is not automatically
enabled in the production flow. Its nearest-vacancy heuristic is an EmuFlow
implementation inspired by selective congestion-aware depopulation, not a
reproduction of the papers below.

`spread_hot_slice_sites` is a second explicit post-DP qualification experiment.
It dilates ordinary occupied slices around the selected regions' physical
slice-coordinate center, then assigns whole slice contents to vacant qualified
sites near those targets. LUT/FF co-location, BEL slots, and control sets stay
intact; fixed, macro-bearing, carry, MUX, and hard-resource sites stay fixed.
The default ratio is 11/10 on each axis, with a bounded target search of 16
physical slice-grid Manhattan units. These are experiment parameters, not
device capacities. An original-site eviction chain guarantees complete group
assignment when the target search has no vacancy. The report records fallback
and actual movement; it does not infer wirelength or routing improvement from
the dilation ratio. The caller must measure actual HPWL, independently certify
the candidate, and trial-route it before acceptance. Neither experiment is
automatically enabled in the production flow.

The read-only `EmuFlowClockPathProbe --audit-site-clocks DEVICE INPUT OUTPUT`
mode independently compares placement region metadata with RapidWright's
device-backed site and tile regions. Input rows contain five tab-separated
fields: case, declared clock region, declared SLR, physical site, and clock
identity. The output seals the input SHA-256, reports mismatched relations,
and counts distinct clocks by declared, actual-site, and actual-tile region.
Its pass status certifies membership agreement only, not clock-routing legality.
The configured OpenPARF clock limit and raw regional clock counts remain
diagnostics: the [UltraScale clock structure](https://docs.amd.com/r/en-US/ug572-ultrascale-clocking/Clock-Structure)
contains segmented routing/distribution tracks, so exact device resources and
completed routing must establish physical acceptance.

The explicit `EmuFlowRWRoute` qualification suffix `--audit-clock-leaf-domains`
materializes the normal frozen input, audits its physical clock sink pins, then
exits without routing. It groups identical sets of two-hop upstream leaf nodes
and compares distinct clock demand with that exact local node count. Overfull
domains prove that ordinary local leaf assignments cannot serve that demand;
they do not prove that every extended fabric clock path is impossible. Pins
without such a domain are counted separately. The report seals the input hash
and retains at most sixteen overfull examples. This read-only audit does not
certify placement, clock skew, or route completion.

`--export-clock-leaf-domains` extends that audit with a SHA-sealed TSV of every
device SLICEL/SLICEM site's two clock-pin domains, modeled slice clock loads,
and fixed loads from other resources. Direct site-pin domains must match the
projected domains of every actual slice sink. This explicit qualification export
is scratch for the active experiment, not a reusable routing certificate.

`complete_ideal_ff_clock_leaf_loads` can seal a separate derived payload for FF
clock pins omitted by the existing `ideal-boundary-clock` routing abstraction.
It traces missing half-slice clock identities to the mapped netlist and accepts
only nets explicitly excluded with that qualification in the frozen route input.
Unknown clocks and conflicting identities are errors. These ideal FF loads are
reserved in local leaf-demand counts, reported separately from physical clock
nets, and rechecked after candidate export. They do not create physical clock
sources/routes or change the backend's ideal-boundary-clock contract. Other
resource loads in this export retain their modeled physical-clock scope.

`repair_clock_leaf_domains` consumes that export, the matching frozen route
input and a certified native placement. It checks source and payload identities
before invoking `emuflow_xilinx_clock_leaf_repair`. The C++ repair selects an
overfull domain and an entire clock's removable slice groups there. It attempts
nearest-vacancy assignments of all donor groups as one transaction, preserving
LUT/FF co-location and BEL slots. Target domains must fit their exact ordinary
leaf counts, and failed transactions restore both placement and clock references.
Fixed, macro, carry, MUX and hard-resource groups remain protected. The bounded
physical-slice search defaults to 128 units; it is an experiment displacement
bound, not a device limit. No net or cell is added, removed or rewired. Remaining
overfull domains are reported explicitly. A zero count is only ordinary local
leaf-demand evidence: new exact placement certification, independent device
reaudit and complete routing/timing are still required. The repair is not
automatically enabled in the production flow.

`add_clock_region_policy` optionally derives a v2 placement-domain payload with
an explicit per-region clock budget. Physical site identities join the same
source-sealed ArchitectureDB used by placement. The payload adds a region domain
for every qualified slice and fixed regional loads from modeled DSP/BRAM/URAM
clock sinks; declared ideal FF reservations remain included. For physical RAM
aliases absent from ArchitectureDB, `physical_handoff_path` must match both
packing and placement SHA identities in the frozen route input. Its typed
assignment-to-cluster join supplies the canonical architecture anchor; aliases
are never inferred from names or coordinate arithmetic. Its region budget
is a conservative qualification policy, not a new claim that all segmented clock
routes above that count are impossible. It does not enable the incompatible
native UTPlaceFX clock model or remove sites.

The native rigid-slice mover enforces both leaf and region domains on every
transaction. Fixed hard-block regional loads prevent a donor-only relocation
from falsely removing that clock from the region. A candidate is still subject
to full placement certification, fresh device/domain reconstruction, and routing.
An unsuccessful domain is retried after another transaction frees destination
capacity; only a complete pass without a successful repair ends the search.
Legacy v1 leaf-only payloads retain their explicit semantics.

After global-clock routing, the adapter independently counts distinct clock
nets on each routed PIP endpoint node before starting signal routing. Repeated
uses by one clock count once. It records shared nodes, maximum distinct users,
unresolved endpoints, intent counts, and at most eight examples. This census
does not replace final source-to-sink/resource validation. Shared ownership or
unresolved endpoints stop execution before signal routing.

Dedicated-clock routing uses a bounded negotiation loop. Ordinary signal
reservations and other clock endpoints remain unavailable on every edge.
Clock route-node overlaps are temporary search states, penalized by present
occupancy and accumulated historical excess. Clocks are processed by descending
fanout with stable name ties. Each iteration rips up and recomputes weighted
source trees, then selects the cheapest join among all nearest reachable sink
leaves. The search includes routing/distribution nodes and `CLK_` connector
wires that the device labels `INTENT_DEFAULT`. A sink's uphill search must
join a leaf reachable from that source; an available but unreachable leaf is
insufficient. The sink tail does not expand back into an unreachable dedicated
clock branch: all source-reachable dedicated nodes have already been explored.
BUFGCE cascade inputs may join another reachable dedicated node. A tail search
also has a 200,000-node work bound. Exhaustion reports the endpoint, depth,
local leaf owners and resource status; it is explicitly a search-budget failure,
not proof that the placement is unroutable. All joins in a BFS layer are scored
before any non-join in that layer expands.
Every negotiation iteration also reports shared-node intent and canonical-tile
clock-region counts, plus at most eight node/owner examples ranked by distinct
clock users and then node name. Canonical tile membership localizes a resource;
it is not a regional demand or capacity certificate for a segmented clock wire.
Clock-tile reservation arrays use each tile's complete device wire domain;
clock exploration must not index a shorter sparse array inferred from another
tile of the same type. Ordinary signal tiles retain upstream sparse allocation.
Only selected paths contribute to clock occupancy. Inter-site clock PIPs enter
the preserved signal-routing graph only after an entire iteration has zero
cross-clock shared nodes. The loop has a 24-iteration work bound and fails
explicitly if no legal clock routing is produced. This removes unconstrained helper calls
from the source/centroid/distribution path. It is a connectivity/resource policy,
not a clock-skew or timing guarantee. The producer identifies this policy as
`recovery-negotiated-clock-tree-v2`; historical sealed route strategies remain
readable by the validator.

`EmuFlowClockPathProbe --clock-owner-self-test` checks distinct-user counters.
`--clock-tree-self-test` exercises blocked resources, dedicated-graph boundaries,
cycles, unreachable nearer leaves, occupied sources and bounded sink tails
without loading a device. `--clock-graph DEVICE SOURCE_NODE SINK_NODE` additionally
checks a concrete device path and its programmable edge directions.
`--clock-source-prefix DEVICE BUFGCE_SITE...` prints dedicated successors along
each source's unique prefix, exposing mandatory shared entry resources.
`--audit-clock-buffer-allocation INPUT OUTPUT` replays the adapter's same BUFGCE
site allocator in the same cell order without creating or routing a design. It
reports source tile-region occupancy and up to sixteen unique-successor prefix
nodes per clock gate. Shared prefix resources are attributed only for clock
roots with non-gate consumers. This is a dedicated-graph allocation diagnostic;
source-region counts alone are not hardware capacity or clock-route certificates.

The academic literature reaches the same conclusion through different
algorithms:

- [UTPlaceF](https://www.cerc.utexas.edu/utda/publications/C199.pdf) combines
  accurate physical placement, congestion-aware packing/depopulation, and
  congestion-aware detailed placement.  It reports improved routed wirelength
  over the ISPD 2016 FPGA placement contest leaders.
- [RippleFPGA](https://ieeexplore.ieee.org/abstract/document/8122004) jointly
  solves packing and placement and adds FPGA-routing-architecture-aware
  optimization because ASIC-style spreading alone is insufficient.
- [Two-Stage Physical Synthesis for FPGAs](https://www.eecg.toronto.edu/~brown/papers/cicc05-singh.pdf)
  uses physical information and logic replication in two stages rather than
  treating synthesis and placement as permanently separate.
- [SPIRE](https://web.eecs.umich.edu/~imarkov/pubs/conf/iccad10-spire.pdf)
  combines placement-dependent timing, cloning, register placement, and
  retiming.  Retiming is relevant later, but is too invasive for the first
  EmuFlow implementation because it changes sequential path identity.

## Implementation plan for EmuFlow

The first implementation should preserve the Phase 1/2 result and avoid
retiming.  It should add a bounded physical-optimization loop inside Phase 7:

1. **Publish exact route feedback.**  After a short trial route, record the
   overused node, tile, SLR/clock region, intent class, occupying nets,
   source/sink cells, fanout, and placement bounding box.  Aggregate the
   congestion contribution per logical net and per placed slice.  Global
   intent counts are diagnostic but cannot select a safe transformation.
2. **Placement-aware fanout repair.**  Select only single-driver LUT/register
   nets with a demonstrated congestion contribution.  Cluster their physical
   sinks by SLR, clock region, and proximity; create one driver copy per
   cluster; place each copy near its loads; and cap both new resources and
   physical site-pin fanout.  Preserve clock, reset, asynchronous, macro,
   carry, and protected-net exclusions.
3. **Equivalent-driver rewiring.**  When logically equivalent copies already
   exist, redistribute loads by physical proximity and observed routing
   overlap before adding new cells.  This is cheaper and mirrors Vivado's
   equivalent-driver rewire pass.
4. **Placement-aware repacking and pin repair.**  Reconsider LUT/FF grouping,
   slice position, and legal pin choice in congested regions.  Allow selective
   depopulation of only the hot slices.  Re-run exact control-set, carry,
   macro, site/BEL, and mapped-pin validation after every change.
5. **Incremental feedback.**  Re-run a bounded trial route, accept a
   transformation only when its exact congestion vector improves without
   violating resource, placement, or connectivity contracts, and then run the
   full route.  Preserve the best checkpoint so a later attempt cannot destroy
   a better candidate.

The acceptance vector should be lexicographic: placement and connectivity
legality, unrouted connections, overused nodes/excess users, worst local
hotspot, routed timing, then routed wirelength.  Analytical overflow and HPWL
remain predictors rather than terminal gates.

## Required correctness contracts

Every structural optimization must publish a transformation ledger that maps
each added driver/net and every moved sink back to its original logical net.
Independent validation must prove:

- one functional source for every sink and no lost or duplicated sink;
- logical equivalence of the replicated LUT/register driver;
- clock/reset/control-set, carry, RAM/DSP, and fixed-boundary exclusions;
- exact resource-accounting updates;
- complete TimingPathDB projection through the transformation ledger;
- exact site/BEL legality after repacking; and
- route completion with zero overused and zero unrouted resources.

The existing Vivado scripts can be used as an oracle and A/B reference when a
license and a compatible DCP are available.  They are not currently part of
the default OpenPARF/RWRoute pipeline, so their presence does not fill the open
backend gap.
