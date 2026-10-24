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
are guidance for fresh placement, not legal site assignments. Every branch keeps its original fanout;
no resource is added and all endpoint fields are preserved. Missing coordinates
skip the whole group. Each move is recorded with the original and new net
ordinals, input identities, and source/output hashes.

The transform publishes a separate candidate transport EmuIR atomically. It
must subsequently pass normal lowering, remapping, placement, and routing;
editing a frozen route-input TSV or rebinding an old certificate is not
permitted. The distance improvement is a placement predictor and cannot be
reported as proven routing improvement. This API is not enabled automatically
in the production flow until a complete physical run qualifies it.

## Relevant academic results

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
