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

The maintained Xilinx implementation routes are OpenPARF/RWRoute and Vivado.
The unused AMF and DREAMPlaceFPGA feasibility adapters, candidate runners and
AMF certificate policy and unused multi-candidate comparator are removed.
Retired placer certificates are not
accepted by Xilinx placement validation. Common packing, capability reporting,
site/BEL ownership, cascade and typed physical-handoff validation remain part
of the retained routes. The VTR/open backend is still needed by the current
Chimew lookahead integration; this retirement does not remove that dependency.

The RapidWright backend has one placement chain: native OpenPARF atomic
placement, independent site/BEL and macro checks, typed physical handoff, then
RWRoute. There is no `--rapidwright-placer` or
`--physical-rapidwright-placer` selector. The old packed-cluster guidance and
greedy legalizer flow, its JSON pack/place producers, native packed-cluster
smoke runner, and single-SLR planner are retired. Their `arch pack-xilinx`,
`place-xilinx`, `guide-xilinx-openparf`, `plan-xilinx-single-slr` and
`validate-xilinx-single-slr-plan` commands are removed. Independent packing,
site/BEL/cascade validators remain available. Small bounded packing and site
oracles live only in tests and are not installed; they do not launch a flow.
The obsolete JSON schema for the original exact-legalizer provider is removed;
current handoff schemas and semantic validators retain their existing contracts.

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

For mapped index v2, `emuflow-native phase7 export-bookshelf` reads the typed
cell, pin, and validated atom relations directly through `libemuflow_core`.
C++ checks atom ownership and types,
contiguous port bits, pin directions, and multiple drivers, then streams nets in
numeric net order with drivers first. Constants remain in primitive pin
signatures but do not become net endpoints; single-endpoint nets are omitted.
Only primitive pin signatures and aggregate counts return to Python, which
renders the small library and atomically publishes the net file. The exporter
uses two ordered cursors over one endpoint index instead of retaining one net's
fanout. It binds input generations, rejects aliased destinations, stages text
through the shared atomic writer, publishes the summary last, and retires its
owned scratch relation on success or failure. It opens its frozen input
relations with explicit immutable read-only URIs and
uses the SQLite 3.22+ development library at build time. Materialized JSON callers
retain the reference exporter for explicit compatibility and differential tests.
This removes one graph-sized Python traversal; mapped lowering, physical macro
preparation, and timing-query preparation are separate stages.

## Native RWRoute export

For mapped index v2 and a single typed physical handoff, the production bridge
requires `emuflow-native phase7 export-rwroute MAPPED PHYSICAL REQUEST TSV SUMMARY`.
The shared-core operation reads both immutable stores directly. The checked
request contains their file generations, source digests, top/part, resource
limits and fixed differential I/O configuration; it contains no placed-cell,
safe-name, gate or net population. A selected native failure stops the export.
The explicit Python reference owner remains available for differential checks
and legacy JSON inputs; it is never an automatic error fallback.

C++ emits the complete existing RapidWright TSV contract: resource/source META,
CELL, selected RAM PARAM, IOB, CLOCK_GATE, NET/PIN and EXCLUDED rows. Lexical cell
order, numeric net order and driver-first pin order are unchanged. LUT6_2 still
occupies the same two physical LUTs; unsupported routable pins reject. RAM binary
strings and arbitrarily large integer parameters retain their exact decimal
meaning. Large INIT payloads are not exported. DSP physical-count expansion and
CARRY8 shared-exit conflict checks retain their existing meanings.

The `native-rwroute-export-v2` implementation builds a source-scoped cell catalog
during CELL emission: route ordinal, interned site ID and primitive/gate flags
occupy 16 bytes per source cell. Sparse nonnegative mapped sequences use an
additional ordered integer ID column; identities never cross artifact boundaries.
The temporary site-name interner is released before clock and net processing.
Clock qualification reduces distinct enable nets once, then distinct root/output
nets once. A reverse-dependency queue computes the existing closed-gate-chain
policy, including cycles; private SQLite scratch contains selected net IDs rather
than endpoint copies. There is no full physical-cell window computation or
candidate-by-candidate fanout scan.

The pin cursor checks source sequence/name agreement and consumes the integer
catalog directly. Only one live net's endpoints retain port names; cell/site/type
strings are not copied per pin. Emitted nets restore canonical cell/port/bit and
driver-first order. CARRY8 exit conflicts use per-cell flags rather than a second
SQL relation. The summary records catalog bytes, visited pin rows, sparse clock
work and maximum live net endpoints. These are implementation metrics; the
logical TSV, clock policy and summary envelope remain unchanged. No gate or
safe-name map returns to Python. Top-port direction/width/bit bindings, duplicate physical I/O bits,
source cell/type ownership, schema versions, request CRC and source generations
are checked before publication. Physical handoff readers use the common escaped
immutable URI, including paths containing `?`, `#`, `%` or Unicode.

The common stage API owns artifact binding, structured errors/metrics and
aliased-destination rejection. TSV publication uses the common atomic writer;
the bounded `emuflow.rwroute-export-summary/v1` is published last. Java/RWRoute
still owns actual routing,
and the independent site/BEL/connectivity, resource, DRC and timing gates remain
required. Native export parity alone does not qualify physical implementation
or complete the native data-plane migration.

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
The proof owner is not yet connected to terminal path dispositions. Until that
consumer is implemented and independently qualified, missing original timing
endpoints remain a coverage failure; the proof store alone cannot qualify
global WNS/TNS.
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
