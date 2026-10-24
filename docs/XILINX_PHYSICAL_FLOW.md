# Xilinx physical flow

This document describes the public contract of EmuFlow's Xilinx physical
backend, including the implementation stages and their acceptance criteria.

## Backend contract

The physical backends publish the same high-level result contract while using
different device models and implementation engines:

| Backend | Device model and implementation |
| --- | --- |
| RapidWright | Supported Xilinx device resources and site/BEL connectivity, native OpenPARF placement, validated site/BEL binding, and RWRoute detailed routing |
| VTR | VTR architecture XML, VPR packing and routing-resource graph, and either OpenPARF or VPR placement |
| Vivado | Optional vendor synthesis and implementation integration for supported inputs |

RapidWright device data describes a device, not the multi-FPGA board around it.
Board topology, fixed package-pin connections, link capacity, and inter-FPGA
latency come from the separate [hardware platform model](HARDWARE_PLATFORM.md).

## Placement preparation

Phase 7 maps the split netlist and generated transport logic to the selected
device model. Ordinary high-fanout nets in the transport design are converted
to deterministic balanced LUT1 buffer trees before native OpenPARF placement.
Clock and reset networks keep their dedicated handling. The added LUTs remain
subject to the same per-FPGA resource limits as all other cells.

The mapped SQLite exporter retains primary-key and pin constraints during
insertion, then bulk-builds the auxiliary pin indexes before atomic publication.
No consumer can observe a partially indexed netlist. This avoids per-pin random
updates to the net-order index during large physical input generation.

For mapped index v2, `emuflow_xilinx_bookshelf_exporter` reads the typed cell,
pin, and validated atom relations directly. C++ checks atom ownership and types,
contiguous port bits, pin directions, and multiple drivers, then streams nets in
numeric net order with drivers first. Constants remain in primitive pin
signatures but do not become net endpoints; single-endpoint nets are omitted.
Only primitive pin signatures and aggregate counts return to Python, which
renders the small library and atomically publishes the net file. The exporter
opens its frozen input relations with explicit immutable read-only URIs and
uses the SQLite 3.22+ development library at build time. Materialized JSON callers
retain the reference exporter for explicit compatibility and differential tests.
This removes one graph-sized Python traversal; mapped lowering, physical macro
preparation, and timing-query preparation are separate stages.

Atomic-source construction, Bookshelf export, and placement validation reuse
the physical macro derivation owned by the same live immutable mapped reader.
Consumers receive detached records, so modifying one returned contract cannot
change later validation. Closing the reader releases this derived state. A new
reader, a different mapped file/top, or a mutable JSON input derives its own
facts; no persistent analysis artifact or cross-run cache is introduced.

The native SLL multiplier keeps OpenPARF's `[0,1000)` acceptance policy and
uses the fixed `t_psi=0.002` Adam step from LEAPS Equation (23). This is the
incremental component of the former `1.002` exponential growth rate, rather
than that rate itself. The current SLL-count delta receives the paper's
`rho=0.9` EMA weight before entering the Adam moments. Applying the absolute
delta again as an exponential factor makes the update depend exponentially on
design size. Oversize positive updates are rejected and negative updates clamp
at zero without disabling SLL optimization or relaxing placement checks.

OpenPARF applies a bounded square-root weight to large nets in its analytical
placement objective. This guides placement without changing connectivity.
The preferred logic-filler row limit may be exceeded when necessary to keep
each LUT/FF filler within half a density bin. Native startup validates the
actual filler count, size, bin size, and span, and the convergence certificate
records those values. Preserving total filler area alone is insufficient:
coarse samples alias the density field and can leave overflow flat while the
placement multipliers diverge. For square logic fillers, the half-bin bound
requires about four times the sampling rows of the former full-bin bound.
Exact legalization and an independent site/BEL validator remain responsible
for physical legality.

Global-placement overflow is an analytical quality signal, not a site/BEL
legality rule. The normal pass target is `0.10`. When the optimizer reaches a
stable candidate between `0.10` and `0.25`, the representative diagnostic path
restores the lowest-HPWL bounded checkpoint and runs DirectLG. Its certificate
records mean and maximum legalization displacement, post-legalization HPWL,
and exact legality. A diagnostic handoff does not authorize routing by itself;
the placement and initial congestion evidence must be reviewed first.
The diagnostic patience window is measured from the last improvement to the
best bounded HPWL. A smaller analytical overflow alone does not reset it,
because the density multiplier can otherwise keep reducing overflow while
wirelength and SLL cost diverge.

The conservative UltraScale+ model qualifies eight independent 6LUT BELs and
sixteen FF BELs per slice. DirectLG retains sixteen LUT z positions to encode
the 5LUT/6LUT pairs, with paired LUT use disabled. That encoding is not physical
density capacity: each analytical LUT consumes `1/8` of a slice, each FF `1/16`.
The density headroom check and filler count use the same qualified capacity.
The `resource_unit_capacity` manifest field records these density units; the
Bookshelf site grammar retains sixteen LUT slots for native BEL numbering.
This leaves the configured target density and final site/BEL checks intact.

Bookshelf's column grammar stretches each site to the next entry. The exporter
therefore inserts zero-resource entries after a logic site when physical gaps
or reserved rows follow it. LUT/FF sites occupy one native grid row; hard-block
spans keep their qualified heights. These unavailable entries are not physical
sites and never appear in the atom-to-site database. The driver compares the
already constructed native density tensors with the exported LUT/FF site-area
totals before optimization. Missing or mismatched area contracts fail closed;
regenerate native inputs when continuing from an older configuration.

The OpenPARF coordinate importer joins solver rows to the authenticated atomic
name-map sidecar through an explicitly URI-enabled SQLite connection. The
sidecar is attached read-only and immutable; the join independently checks atom
identity and exact coverage without expanding the complete name map in Python.

The default placement region covers all SLRs of the device. The platform's
per-FPGA utilization limit is a whole-device capacity contract and does not
implicitly create a pblock. Using the whole device lets the placer trade
wirelength for lower local density and better routability.

## Detailed routing and validation

The validated placement is passed to RWRoute. Routing is complete only when
all required connections are routed, no capacity overlap remains between
different nets, and the independent route certificate can resolve every
covered logical net to its physical nodes and PIPs. Iteration diagnostics
include congested connections, overused-node counts, excess users, maximum
occupancy, and SLR distribution.

Clock-region capacity, clock-tree legality, ordinary-net legality, and route
coverage are separate checks. Passing a coarse clock-region capacity audit is
necessary when that audit applies, but it does not by itself certify complete
clock routing or clock timing.

## Fixed differential I/O

Phase 7 consumes the sealed Phase 6 binding, differential adapter, and XDC
files. Each used virtual-board lane must resolve to the same top-level port,
P/N package pins, bank, direction, and schedule entry recorded by Phase 6.
The RapidWright adapter materializes the corresponding differential I/O
boundary and includes it in route coverage and boundary timing.

A mismatch between the wiring inventory, binding, generated RTL, or XDC is a
hard failure. The resulting route validates the declared research platform;
electrical board signoff still requires the real board's power, clock, reset,
connector, termination, and signal-integrity constraints.

## Timing publication

Per-FPGA physical timing records logic and interface delays from the routed
implementation. EmuFlow then binds those delays to the selected TDM events and
BoardDB link-delay model for global analysis. OpenSTA is the default global STA
engine. Its WNS/TNS values are meaningful only for the declared TimingPathDB
population and the sealed physical/platform inputs described in the
[global timing qualification](global-opensta-qualification.md).

Passing partition resource checks, placement legality, or routing alone does
not establish whole-design timing closure. Phase 7 publishes a successful
whole-design result only after every selected FPGA record and the global timing
contract pass their independent validators.

## Vivado indexed provider

The optional `vivado` backend consumes the current compact Phase 6 split and
transport contracts. It streams native Xilinx primitives through the canonical
mapped SQLite index into RTL, then runs Vivado synthesis, optimization,
placement, physical optimization, and routing in out-of-context (OOC) mode.
Fixed data pads are implemented, while external clock sources remain abstract;
absolute clock insertion/skew and board signoff are not qualified. This scope
is retained in each physical result and its summary projection. The common
physical-result schema accepts the native resource dimensions, resource-budget
evidence, and explicit OOC analysis scope. The retired whole-EmuIR adapter
remains only for explicit legacy inputs. This integration is undergoing
real-device qualification; unit tests alone do not establish routed or global
STA success.

The exporter declares actual SQL net ordinals, including one-based or sparse
keys. Disconnected input identities start beyond the maximum source ordinal,
so they cannot alias a real net. Generated RTL disables implicit net creation.
Before mapping, a bounded SQLite audit rejects every used net without a driver,
including source-declared `undriven` nets, and saves a compact diagnostic sample.
An upstream semantic repair or an explicitly specified OOC input model is
required for these sources; the provider never invents constant bindings.

Native timing bindings include FF clock-enable and reset/set controls, both
LUT6_2 outputs, and the qualified RAMB18 physical write-enable aliases.

The provider uses the Phase 6 sealed differential adapter and XDC through a
physical wrapper. Post-route checks require exact P/N package pins,
IOSTANDARD, IBUFDS/OBUFDS ownership (including Vivado's native DIFFINBUF/IBUFCTRL
decomposition), zero unrouted nets, and zero blocking DRC violations.
Mapped primitives carry source tags, without global KEEP/DONT_TOUCH, LOC/BEL,
or implicit pblocks. Synthesis and physical optimization may remove, merge,
replicate, or change cells. Indexed inventories record observed source lineage
and unretained source tags; this census does not certify functional equivalence.
The explicit `vivado-optimized-source-lineage-v1` policy separates this source
census from actual routed physical components and inserted infrastructure.
Timing queries resolve the original source's typed descendants first, without
broadening a retained source to other equivalent cells. A removed object may
alias another source only when the primitive, parameters, pin interfaces and
original input connections are identical. Queries across descendant collections
take the worst physical endpoint delay. Removed source objects require checked
elimination proofs before an original path can be marked optimized away.
Unproved missing associations and unsupported transformations fail timing
coverage; they never trigger blanket endpoint locks, silently drop original
paths, or qualify incomplete global WNS/TNS. Fixed I/O
and the complete original TimingPathDB remain independently checked.
Collections with multiple physical endpoints are labelled
`optimized-endpoint-collection-upper-bound`, rather than endpoint-exact.
Sequential transformations whose cycle correspondence is not established
are rejected by the timing adapter, without disabling the vendor optimization.
The indexed elimination-proof owner records closed source regions with no path
to a retained source or partition output, and inductively constant register
states with specified initial values. A proof that depends on selecting a
don't-care initial value remains a conditional candidate until the vendor's
choice is confirmed; it cannot eliminate a timing record on its own.
Unknown combinational behavior, changing reset values,
and unsupported primitives do not establish a constant proof. Timing records
must reference this canonical source proof rather than invent a zero delay.
Source names are sealed with the proof so a timing record cannot borrow an
unrelated cell's elimination. A routed proof is bound to one FPGA and mapped
input; a synthesis-stage diagnostic cannot qualify a routed timing disposition.

Local timing storage version 2 retains every original local member, including
`measurement: optimized-away`, `delay_ns: null`, and an `optimization_source`
reference when its requested endpoint has a checked elimination proof. It does
not invent a physical endpoint trace. Version 1 measured-only generations remain
readable. The query resolver, Tcl export, and importer preserve the disposition;
the physical summary references one compact proof database per FPGA. Unknown
initialization candidates remain unresolved until the vendor choice is confirmed.
The boundary-timing and cross-layer physical-binding JSON schemas also encode
null-only elimination records and reject fabricated numerical delays.
Logic-segment and boundary timing storage version 2 provide the same nullable
disposition contract and retain measured-only version 1 readers. A removed shadow
interface must reference its canonical Phase6 boundary register; a different
cell's proof is rejected. An unassociated DUT net or port is still unresolved.
Cross-FPGA binding consumes every original logic segment and scheduled boundary,
then excludes an original member from numerical STA only when a required segment
or interface has a checked elimination proof. Missing segments, events, interfaces,
and conditional initialization candidates still make qualification incomplete.
The explicit Python comparison composer rejects optimization dispositions; this
population contract currently requires the global OpenSTA engine.
Whole-device resource budgets also apply to the final physical cell population.
Resource accounting excludes MACRO wrappers while retaining LEAF and INTERNAL
implementation components. LUT capacity is counted by occupied site/LUT letter
(O5/O6 share one LUT); DSP and URAM components are counted by unique sites.

Same-FPGA timing queries cover the complete declared original-path population;
cross-FPGA logic and interface queries retain the Phase 6 identities. Fixed
logical transport ports resolve to the declared physical P pad. Query extents
are checked against actual timing endpoints; a sequential Q launch may start
at the same primitive's clock to include clock-to-Q, while an unrelated prefix
or any suffix beyond the requested endpoint is rejected. Fresh timing-export
sessions apply equal datapath-only requirements across clock pairs so a cone
query ranks physical delay rather than nominal clock slack. This measurement
constraint is never written to the routed checkpoint or used for implementation
WNS/TNS or global event deadlines. The
indexed provider does not use the legacy sampled Vivado TimingPathDB as terminal
evidence. Per-FPGA vendor WNS/TNS describe vendor endpoint constraints; global
original-path WNS/TNS still require the separate complete OpenSTA composition.

Use an installed Vivado with the exact part database and a valid synthesis and
implementation license. `EMUFLOW_VIVADO_THREADS` controls the recorded thread
limit (default 2). All generated files, checkpoints, and temporary tool files
belong to the selected output directory and experiment scratch lifecycle.
`scripts/validation/qualify_vivado_indexed.py` exercises a small three-clock,
four-differential-pair implementation on XCVU19P. Its default `Quick` placement
directive is specific to this smoke test; the production provider remains
`Default`. Its result explicitly covers
synthetic physical/provider qualification only.

An explicit checkpoint I/O diagnostic is available as
`scripts/vivado/inspect_checkpoint_io.tcl INPUT_DCP OUTPUT_DIRECTORY EXPECTED_PART`.
It opens and writes a temporary checkpoint and checks its part; this is an I/O
check and does not qualify placement, routing, or numerical timing.

`scripts/vivado/inspect_constant_frontiers.tcl INPUT_DCP QUERY_TSV OUTPUT_TSV`
exports selected LUT INIT values and every input driver with its source tag.
The query has `source_origin` and `native_cell_hex` columns. The bounded
`require_equal_frontier_functions` checker exhausts the union of input variables
for one source/native LUT pair, allowing pin permutations and reduced LUTs only
when their complete functions agree. Matching a pin name or observing a GND pin
alone is insufficient. These diagnostic facts do not automatically promote an
unknown-initialization candidate to a routed elimination proof.

`compare_selected_constant_frontiers` joins these observations to the mapped
source and its primitive-signature index. It checks every selected object and
input pin, identifies live variables by source output provenance, and rejects
unsupported sequential transformations. Selection is capped at 4096 objects;
the diagnostic reconstructs only one LUT pair at a time. Conditional candidate
values remain hypotheses in this comparison and still need complete fanout
coverage before they can become elimination proofs.

`prepare_constant_frontier_query` follows state-to-state fanouts through
conditional candidates to construct a bounded closed component, proves its
joint state values inductively, and returns the complete native LUT inspection
query. It closes the source databases before the native checkpoint is opened;
the plan has `status: inspection-required` and is not an elimination proof.
The component is limited to 4096 states, 4096 retained LUT objects, 65536 fanout
uses and frontier references, and a bounded closure traversal.

`confirm_selected_constant_sources` rebuilds that plan and checks complete
fanout coverage for the closed component. Every use must stay inside the
independently inductive selected state set or reach a checked retained LUT, and
every native replica must be observed. Each source record lists all reachable
external LUT frontiers, including exits through other selected states;
components without an observed frontier remain unresolved. Likewise, a direct
output port or unsupported fanout remains unresolved. The certificate binds the FPGA, mapped source,
conditional source-model artifact, synthesized checkpoint, source inventory,
and native observations. Its explicit policy chooses a legal joint
initialization for unspecified states and confirms the selected substitutions
at their retained frontiers. It is not equivalence for every initial state.
A routed consumer must still check its synthesized checkpoint identity and
import the certificate into the partition proof owner before global timing may
use these selected states as elimination proofs.

The proof owner accepts `CheckedConstantSources` from
`check_constant_sources`, rather than a saved JSON `status: pass`. It binds the
checked facts to its own FPGA, mapped index, and synthesized checkpoint, verifies
the selected values against its current inductive model, and seals the compact
confirmation document inside the partition proof database. Only those selected
sources can gain `initialization: vendor-confirmed`; other conditional states
remain unproved. Proof readers verify the document seal, complete frontier
references, selected values, policy, and counts. A synthesis-only preflight must
explicitly select the synthesized lineage stage and is never accepted as routed
proof by a routed consumer. The native producer still needs to connect this
checked input at its post-implementation import boundary.

Endpoint association uses the indexed source-origin lookup for retained cells.
Only absent origins enter the indexed identical-input signature lookup. Both
branches use the stage/origin index explicitly, so a pin query cannot become a
scan of the full physical inventory. The lineage snapshot is immutable during
query consumption; no whole-circuit Python map or SQLite integrity audit is
needed for resolution.

An explicit `diagnostic_unresolved_path` on `resolve_timing_query` scans the
whole requested query population and aggregates unresolved required sources by
source pin and role into a bounded-memory SQLite diagnostic. It reports
`incomplete` when any required use is unresolved. Optional unused cone anchors
remain deferred. Diagnostic TSV rows use an invalid native hint, so ignoring
the incomplete status cannot silently produce partial timing measurements.
Normal execution retains fail-fast behavior.

Constant-confirmation version 2 proves the selected states inductively using
only their selected values and specified, already-proven constant states. Other
conditional source states remain unknown; their proposed INIT choices cannot be
borrowed silently. For the selected synthesis frontiers, primitive type, INIT,
and each pin binding must also agree with the source projection. Boolean
function equality alone can conceal a different input association and is only
a diagnostic in that case. These checks validate selected synthesized facts;
Vivado optimization remains unconstrained. Version 1 confirmation documents are
preliminary diagnostics and cannot qualify a vendor-confirmed elimination in
the current proof reader.
