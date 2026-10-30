# Xilinx physical flow

This document describes the public contract of EmuFlow's Xilinx physical
backend, including the implementation stages and their acceptance criteria.

## Backend contract

The physical backends publish the same high-level result contract while using
different device models and implementation engines:

| Backend | Device model and implementation |
| --- | --- |
| RapidWright | Supported Xilinx device resources and site/BEL connectivity, native OpenPARF placement, validated site/BEL binding, and RWRoute detailed routing |
| Vivado | Optional vendor synthesis and implementation integration for supported inputs |

The maintained Xilinx implementation routes are OpenPARF/RWRoute and Vivado.
The unused AMF and DREAMPlaceFPGA feasibility adapters, candidate runners and
AMF certificate policy and unused multi-candidate comparator are removed.
Retired placer certificates are not
accepted by Xilinx placement validation. Common packing, capability reporting,
site/BEL ownership, cascade and typed physical-handoff validation remain part
of the retained routes. VTR/open execution, its standalone architecture/packing/
routing/timing adapters and engine build/install dependencies are retired.
Chimew consumes only the maintained Xilinx physical owners.

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

Physical inputs have an explicit last consumer in the ordinary flow. A closed
transport EmuIR is retired once placement-IR lowering succeeds. The merged
placement EmuIR, and Vivado's emitted Verilog when selected, are retired once
that partition's query construction, physical implementation and timing checks
have completed. Producer reports retain their counts and retirement state;
retirement does not rehash or reload the graph. Failed consumers retain their
inputs for diagnosis until the failed attempt is safely retired. This applies
to both controllers and separately executed FPGA workers, without a cache flag.
Boundary identities and typed timing owners remain available for aggregation
and complete global STA. Mapped indexes, physical handoffs and site/lineage
owners remain available to the retained Chimew lookahead consumer. Per-partition
retirement does not constitute complete physical/global timing acceptance.

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

The separate CARRY8/LUT6_2-only exporter, runtime adapter, certificate and JSON
bridge are retired. Mixed-resource atomic placement owns carry macro and directed
chain validation. Shared native carry kernels and current legality tests remain.

## Typed placement handoff

`materialize_xilinx_openparf_atomic_contract(mapped, architecture, certificate,
physical_handoff, ...)` has one required SQLite output. The former pair of JSON
output arguments and optional typed-mode selector are retired. Production requires
the typed placement certificate and streams each validated site group directly to
the handoff builder; no complete physical-cluster tree is returned by the validator.
Packing and placement remain lazy views for independent circuit/site/BEL checks.

The publisher binds the declared source files and certificate/atomic-source
sidecars before consuming their records. It rejects aliased outputs, including hard
links, and checks bound generations immediately before atomic replacement. A source
change or failed independent validation preserves the previous published handoff.
Bridge source checks and header assembly reuse the same operation-local digests,
while publication checks bound generations. Independent native-device validation
retains its own source checks.
Small JSON validation fixtures are snapshots of this same typed producer under
`tests/openparf_bridge_fixture.py`; this bounded helper is not installed and does
not implement a second bridge algorithm.

## Native RWRoute export

The native physical controller exports from a mapped index and one typed
physical handoff. The Python qualification client
`export_rwroute_input(mapped_index, physical_handoff, output)` delegates to the
same native stage; complete-flow execution does not call the Python client.
The former standalone Python CLI, separate `--packed`/`--placement` JSON export
inputs and Python-object API are retired.
The v2 bounded native request carries source identities, I/O and resource limits;
top and part come directly from the authoritative stores. It never opens Python
packing/placement views or copies their graph populations. Standalone export hashes
the physical handoff once; managed callers reuse the sealed source digests.
The independent Python TSV oracle lives in `tests/rwroute_reference.py` for explicit
qualification and is not installed or selected by production failures.

For mapped index v2 and a single typed physical handoff, the production bridge
requires `emuflow-native phase7 export-rwroute MAPPED PHYSICAL REQUEST TSV SUMMARY`.
The shared-core operation reads both immutable stores directly. The checked
request contains their file generations, source digests and resource
limits and fixed differential I/O configuration; it contains no placed-cell,
safe-name, gate or net population. A selected native failure stops the export.
The noninstalled test oracle remains available for explicit differential checks;
legacy JSON inputs are limited to qualification fixtures.

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

Clock-region/site-type buckets expose their complete nonoverlapping physical
capacity. Analytical density reserves whitespace without deleting a fraction of
qualified sites. Site/BEL uniqueness and whole-device resource limits remain hard
checks; local utilization is diagnostic within physical capacity. RWRoute must
still establish routability. The site-domain contract records
`full-qualified-site-domain`; the routability policy is
`openparf-rudy-pin-stable-feasible-full-sites-v8`.

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

`phase7 export-vivado MAPPED REQUEST RTL FUNCTIONS SUMMARY` produces primitive
RTL and the existing source-function index through the shared native operation
API. Source generations, cell ordinals and complete pin coverage are checked;
the producer holds only the current primitive and its pins. Net and port bit
identities, scalar parameter values, source provenance attributes and function
signatures preserve their existing contracts. Integers wider than 64 bits are
not narrowed, and unspecified x/z INIT values retain the existing omission
policy in RTL while remaining part of the source-function identity. RTL is
hashed as it is emitted. A bounded summary returns to Python, which publishes
the derived output group with source checks and failure rollback before the
final report. Recovery links do not copy payloads or form retained checkpoints.
The old Python cell writer/signature producer lives only in small independent
tests. Missing native export capability fails before full-flow synthesis;
failure never selects a Python exporter. Source query construction and other
physical adapters still have separate native-migration obligations.

Boundary, logic-segment and local-path routed measurements are imported through
the shared native core into the current typed timing owners. Import validates
source/object identity, measurement classification, order and full coverage
before atomic selector publication. The local query identity uses the same typed
zero-delay owner as the OpenPARF/RWRoute route; the private JSON-row SQLite
identity is retired. See the [global timing contract](global-opensta-qualification.md)
for partial-diagnostic and terminal original-member coverage rules.

The optional `vivado` backend consumes the current compact Phase 6 split and
transport contracts. It streams native Xilinx primitives through the canonical
mapped SQLite index into RTL, then runs Vivado synthesis, optimization,
placement, physical optimization, and routing in out-of-context (OOC) mode.
Fixed data pads are implemented, while external clock sources remain abstract;
absolute clock insertion/skew and board signoff are not qualified. This scope
is retained in each physical result and its summary projection. The common
physical-result schema accepts the native resource dimensions, resource-budget
evidence, and explicit OOC analysis scope. The whole-EmuIR execution adapter,
independent pre-placement Vivado TimingPathDB producer, and sampled-export
controls are removed. Current Vivado export and physical execution reject JSON
EmuIR inputs; there is one indexed implementation chain. Shared primitive
conversion remains for explicit Xilinx reference exports, and the board timing
consumer uses the current streaming log and indexed boundary owners.
Board-top generation reads only indexed design metadata and ports, releasing
the view before implementation; it does not load a circuit merely to connect
the already mapped partition.
CMake replaces its managed Vivado script directory so deleted scripts cannot
remain executable in an updated install. This integration is undergoing
real-device qualification; unit tests alone do not establish routed or global
STA success.

The standalone `sta emit-vivado-cut-map`, `import-vivado-tsv`,
`emit-vivado-net-map` and `import-vivado-path-database` commands, their vendor
path-provider policy and cut-path script are removed with the old timing
producer. Current OpenSTA net maps, path import/projection and original-path
validation remain; Phase 7 vendor boundary, local and logic measurements use
their dedicated indexed owners. Historical vendor TimingPathDB artifacts must
be examined with the matching Git version.

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

The current census protocol explicitly records native primitive level and,
for physical leaf/internal objects, site, BEL, tile column and tile row. Macro
wrappers remain lineage observations but do not occupy a second BEL. Tile
coordinates use [the shared RapidWright/Vivado column/row definition](https://www.rapidwright.io/javadoc/com/xilinx/rapidwright/device/Tile.html),
not the numbers embedded in site names or OpenPARF's site-stride x encoding.
The native importer validates unique site/BEL occupancy and complete placement
for routed physical objects, checks source origins against the mapped owner,
and atomically publishes logical `emuflow.vivado-cell-lineage/v2`. The retained
SQLite owner contains both observed provenance and placement facts. Objects
reference integer site IDs; each site's native coordinates are stored once,
rather than repeated for every packed cell. There is no separate full placement
JSON. Tcl caches geometry only for sites reached in the frozen census. Its placed-object count must agree with the
independent physical mapped-cell census. Consumed raw pre/post-route TSV
snapshots are removed after import; their byte hashes remain in the owner.
These are placement/provenance checks, not a device-geometry or functional
proof. Source tags that disappear remain explicitly unretained; no zero-delay
or fabricated-location fallback is introduced. The Chimew lookahead queries
this same owner by source identity; missing active source/boundary placement
remains an error.

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

## Indexed Xilinx Chimew lookahead

Chimew's optional prepass uses the same selected backend and physical options
as the candidate. Vivado locations come from mapped source origins and the
normalized lineage/site tables. OpenPARF/RWRoute locations come from the typed
physical handoff and `site-map.sqlite3`, whose native tile coordinates and
architecture seal are explicit. Named-site lookups use the site-name index;
OpenPARF dense/site-stride coordinates are not substituted for native geometry.
The device site-map publisher hashes each immutable generation once and reuses
that seal for later hard links. Routine consumers check the published file
generation. If only ctime changed, they verify the original byte seal once per
current shared inode generation; restored mtime cannot hide changed content.
Original instances are matched by ID, never by equal ordinals in two artifacts.
Forwarded endpoints use placed boundary registers, with complete register
coverage. No instance-order alias map or full placement JSON is produced.

Package pin locations come from the ArchitectureDB's source-qualified native
package catalog. The exporter uses the device package's actual site/tile
[API](https://www.rapidwright.io/javadoc/com/xilinx/rapidwright/device/PackagePin.html);
fixed P/N identities are independently checked against the board inventory.
The device-sized pin catalog is distinct from circuit-sized endpoint data.
Missing or ambiguous geometry fails before frontend work.

Virtual region encodings and RUDY remain lookahead heuristics. Actual fixed
wires, whole-bank static direction and electrical constraints remain mandatory.
This interface does not itself establish real-device Chimew QoR, board signoff
or complete NVDLA Phase 1–7/7C qualification.

## Indexed handoff population validation

`emuflow-native phase7 validate-physical-cells MAPPED PHYSICAL REQUEST SUMMARY`
checks the current mapped-index v2 and typed physical-handoff v1 owners directly.
Its checked request binds both input generations and the expected mapped identity
and top. Source/assignment coverage is checked in both directions, constants are
accounted for separately, each pin retains its source owner and complete port
ordinal range, and output-driver uniqueness covers the entire mapped population.
The result is a compact summary published through the shared source-bound writer.
A failed check cannot replace the physical handoff or a prior summary.

This population check complements the bridge's per-site FF control, capacity,
site/BEL, region and native BRAM checks and its MUX/CARRY/cascade topology checks.
It does not replace routing, fixed-I/O, resource-budget or final timing acceptance.
Cascade discovery queries only its requested primitive families. Topology driver
lookups read the required bits from the mapped pin owner after the complete native
uniqueness check, rather than rebuilding the complete driver population.

`emuflow-native physical build-routed-endpoints` binds the mapped index and one
typed physical handoff to the qualified route net stream. It joins typed pins
and physical assignments in native, net-local indexed relations, without Python
sites/endpoint maps or a whole-route seen-bit set. The existing v2 timing
manifest and endpoint JSONL remain the interchange: the native writer computes
their hashes while emitting them. The host publishes both closed files as one
source-bound group and restores both previous outputs on publication failure.
The old Python producer remains an independent test oracle only.

Independent route validation still runs first. Its `source_generations` seals
the route manifest and net/PIP payload generations while qualifying them;
endpoint binding guards the manifest/net generations and reuses the completed
PIP qualification without reading or retaining that payload. Physical handoff views
also retain their original generation. The timing host returns borrowed,
sequential metadata-array views instead of rebuilding the clock/I/O populations;
graph and OpenSTA consumers retain that produced generation. This preserves
exact/shared-site/intra-site delays, static/internal-clock exclusions and fixed
I/O/clock-gate bindings. It does not replace independent physical route checking.
The former separate routed-timing build/validation CLI is retired. The complete
native RapidWright partition binds route endpoints and publishes every requested
timing evidence owner within the accepted partition transaction. Component-level
producer and independent-reader harnesses live only in the test tree.

Routed boundary, logic-segment and same-FPGA path measurements use
`emuflow-native physical build-routed-segments`. Its checked request binds the
current mapped index, source placement IR, routed timing manifest/endpoint stream
and typed identity owners. The operation constructs one forward CSR graph and
emits the three existing typed timing owners directly. It groups queries by
start and resets only the reached cone between groups. No graph, path array or
intermediate measurement TSV crosses the Python boundary. The host publishes
closed owners through the common source-bound selector publisher after all
requested projections succeed. The physical report retains the common engine
wall/CPU/RSS metrics and bounded graph/query counters. Native capability is required before synthesis;
missing capability or a failed native measurement cannot select a Python route.
The former graph/builders are independent small-fixture test oracles.

This preserves the lightweight provider's delay/qualification limits, sequential
barriers, exact endpoint coverage, fixed-I/O terms, cycle and unreachable-path
rejection. It does not extend device characterization or replace final global
OpenSTA qualification. `source_constraints` records a bounded undriven-bit count
and canonical source reference instead of copying the source population into
every physical report. Independent route qualification, mapped Verilog export
and path-result import still retain Python data-plane work; these producers do
not complete native migration or real-design qualification.

`emuflow-native physical stage-routed-opensta` consumes the current mapped-index
v2 and routed-timing v2 endpoint stream and writes the final compact EmuIR directly.
It joins typed pins to routed sinks, assigns deterministic delay cell/type/net
identities and streams delay-cell Liberty. Temporary indexed work relations stay
inside the native writer; no complete mapped-cell snapshot, Python endpoint map,
or Python unique-delay model is created. The SQL DDL for compact EmuIR has one
installed source shared by Python and the native header generator.

The compact owner includes `routed_opensta_feature_schema` equal to
`emuflow.routed-opensta-types/v1` and a `routed_delay_types(seq,attributes_seq)`
index. It references the existing interned attribute payload rather than copying
delay values or names into another store. Old compact readers may ignore this
feature; routed-model queries require their original owner generation. Model
coverage reports bounded base-type lists, generated-type counts and the typed
owner reference (`emuflow.native-routed-model-coverage/v1`), instead of expanding
all generated type names into reports. The final OpenSTA run's
`used_cell_type_summary` preserves that schema, the total `count`,
`base_cell_types`, and `generated_cell_types` owner reference; it does not
reconstruct a complete name set for a summary hash. Ordinary bounded models
retain their existing count/hash summary. The host renders only the bounded base
model; OpenSTA reads the native delay-cell Liberty as a second library.

`emuflow-native physical export-opensta-inputs EMUIR REQUEST VERILOG NET_MAP SUMMARY`
emits timing-only Verilog and the original-name net map from the compact owner.
Either text output can be `-` when an explicit exporter needs only the other;
normal indexed OpenSTA execution produces both in one call. The checked request
binds the original file generation and selects either stored cell types or the
existing Xilinx clock-polarity specialization. Endpoint cursors merge by integer
owner, and only one instance's pins are retained. Constants, sparse bus pin
scalarization, inout connections, escaped-provider ambiguity checks and stable
ordinal/net naming retain the reference semantics. Both text outputs are
adopted together with rollback and without payload copies. The Python indexed
Verilog builder is retired; inline EmuIR rendering remains the independent small
fixture reference. A missing native export capability fails before synthesis
when compilation requests internal OpenSTA timing.
The OpenSTA report's bounded `input_export` section records the implementation,
emitted record counts, maximum per-instance pin population and native stage
wall/CPU/RSS/I/O metrics separately from the STA engine.

Before publication, an independent shared-core check requires one delay cell per
sealed endpoint, a valid type/payload binding and exactly one A input and Y output
on distinct nets for each delay cell. It uses the normal reverse-adjacency index.
The host adopts both closed output files as a source-bound group and restores the
previous graph and library on failure. Interrupted native files remain in their
invocation's private scope. Borrowed model reads and the OpenSTA consumer reject
changed generations or a model paired with another graph. Missing capability
fails before synthesis; native failures cannot select a Python staging route.

The `arch run-xilinx-opensta` producer requires `--mapped-index` and current
`--routed-timing`; the former `--mapped-json` producer input is retired. Historical
result validation remains a separate reader. The old graph builder is a bounded
test oracle only, with no production import or fallback. Primitive setup-only
limits, negedge FF semantics, conservative LUT/CARRY coefficients and unqualified
hard-block surrogates remain unchanged. This does not establish hold, clock-skew,
physical or whole-design timing qualification.
