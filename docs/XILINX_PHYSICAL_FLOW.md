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

The native SLL multiplier keeps OpenPARF's `[0,1000)` acceptance policy. When
the exponential step exceeds floating-point range, a log-domain comparison
implements the same positive-update rejection or negative-update clamp without
materializing the overflowing power. Normal finite updates keep the original
arithmetic; this does not disable SLL optimization or relax placement checks.

OpenPARF applies a bounded square-root weight to large nets in its analytical
placement objective. This guides placement without changing connectivity.
Exact legalization and an independent site/BEL validator remain responsible
for physical legality.

The conservative UltraScale+ model qualifies eight independent 6LUT BELs and
sixteen FF BELs per slice. DirectLG retains sixteen LUT z positions to encode
the 5LUT/6LUT pairs, with paired LUT use disabled. That encoding is not physical
density capacity: each analytical LUT consumes `1/8` of a slice, each FF `1/16`.
The density headroom check and filler count use the same qualified capacity.
The `resource_unit_capacity` manifest field records these density units; the
Bookshelf site grammar retains sixteen LUT slots for native BEL numbering.
This leaves the configured target density and final site/BEL checks intact.

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
