# Architecture and phase contracts

EmuFlow separates logical compilation, board-level communication, per-FPGA
physical implementation, and global timing analysis. Providers exchange
versioned artifacts rather than sharing tool-internal state.

## Data flow

```text
RTL + constraints + mapping profile
  -> mapped EmuIR and resource summary
  -> original TimingPathDB and optimization weights
  -> clusters and FPGA assignment
  -> board-level routes
  -> TDM ratio/lane/slot assignment
  -> per-FPGA netlists and transport RTL
  -> physical placement/routing and delay bindings
  -> whole-design global timing
```

## Canonical inputs and outputs

| Boundary | Canonical information | Consumer responsibility |
| --- | --- | --- |
| Frontend -> timing/partitioning | EmuIR connectivity, primitive parameters, resources, clocks, identities | Use the mapped circuit; do not reinterpret it as a contest communication graph |
| Timing -> partitioning/routing/TDM | Original-path identities, clock constraints, timing weights | Preserve the relationship between optimization representatives and original members |
| Partitioning -> system routing | Assignment, cut endpoints, BoardDB, route constraints | Choose legal source-to-sink communication paths |
| System routing -> TDM | Ordered routes, directions, link demand | Allocate ratios, lanes, and slots without changing route identity |
| TDM -> splitting/transport | Schedule-entry identities and event semantics | Bind logical boundaries and generate matching transport RTL |
| Splitting -> physical implementation | Per-FPGA netlists, constraints, device/architecture inputs | Account for every cell and enforce physical resource/ownership legality |
| Physical implementation -> global STA | Logic/interface delays, original-path bindings, concrete schedule, link timing | Check event readiness and launch-to-capture timing for the declared population |

Phase 5 has one production provider, the native Static Exact dependency
scheduler. It consumes the typed RouteStore and the Phase 3 semantic binding,
writes one typed ScheduleStore, and publishes its compact selector and report
only after independent native validation. `core::RunPhase5` owns the complete
stage: model/solver execution, independent validation, timing projection, store
and selector seals, phase report construction and report-last publication.
Its `phase5 run` entry accepts the original RouteStore and bounded board,
generation and source-binding requests. The Python API prepares only those
requests and checks the bounded returned report; it does not call the individual
producer, validator or timing subprocesses or assemble a second generation.
`phase5 compile` reads the current route selector, assignment v4 index and
normalized BoardDB directly. It verifies the route file/metadata seals and source
semantic binding, preserves typed relation/sequence checks, and constructs board
and generation requests inside C++. Fixed/affine delays, integer direction-bank
allocation, capacity identities and deterministic ordering retain the same policy.
The native compiler uses this entry without a Python TDM adapter; its following
phase receives a versioned reference to the generation-bound phase report.
The validator reconstructs readiness,
wire/ratio, arrival and commit evidence directly from typed owners; it does not
call the solver or replay route/hop records in Python. Full compilation rejects retired TDM
providers and registered-boundary mode before frontend work. Minimum-frame
search varies the frame bound using that same engine; it is not another TDM
provider. Hardware ratio/bank constraints and Chimew's lane/slot consumption
remain part of the supported contracts.

### Phase 5 indexed timing

Current typed Static Exact production uses cross-layer timing v3. RouteStore owns
the paths, members and cut transitions; ScheduleStore owns concrete events. The
contract contains their source identities, projection policy, complete path-binding
hash and counts, rather than another `path_bindings` array. Phase 7C validates the
projection against those owners and continues to read physical logic and boundary
evidence from their existing authoritative stores. The
[v3 schema](../schemas/cross-layer-timing-v3.schema.json) is distinct from inline
fixture/reference contracts; indexed production does not retry the Python builder.

`phase5 timing-contract ROUTE_STORE [SCHEDULE_STORE [BOARD_REQUEST]] REQUEST SUMMARY`
links into the shared core. The bounded request selects binding, timing analysis,
or both. Normal Phase 5 publication and validation select both in one invocation
when timing paths exist, avoiding a second route/path reconstruction. Binding
checks exact route-hop coverage, member uniqueness, source/sink identity, required
and fixed times, and explicit multicast transitions. Its digest is SHA-256 of the
compact sorted-key, ASCII-escaped JSON array of the former logical path bindings:
records are ordered by path ID, while member/cut order stays in source order.
Every original member and cut field participates; no sampling replaces coverage.

Analysis preserves absolute fixed-slot TX propagation, declared per-hop ratio delay,
worst-sink selection and the existing Phase 5 diagnostic population. Exact
quantiles and deterministic left-to-right negative-slack accumulation use a
transient numeric ordering relation. It holds only source-scoped integer IDs,
canonical ranks and floating-point values; names and member records remain in the
RouteStore. Working pages are bounded, and closing the native reader retires the
SQLite TEMP relation. It is neither a new handoff owner nor a checkpoint. Signed
zero and double precision survive sorting. No report claims global physical timing
from these pre-placement statistics.

Phase 6 exposes baseline transport and optional Chimew. The Chimew adapter
shares the typed pin-plan, electrical-binding and transport contracts; shared
validators and fixed wiring constraints remain authoritative. Retired
placement-aware planner providers cannot enter either split producer. The
baseline/Chimew comparison preserves complete physical/global timing gates.
Native control supports that same comparison sequence. Its private handoff carries the bounded, immutable package
pin catalog with its original architectural source generation and SHA; no
circuit locations are encoded in that catalog. It restores baseline report
aliases from the single comparison owner and rejects duplicate owners.
Its prepass and candidate share the selected Xilinx backend, tools, seed and
workers. Physical endpoint facts are queried from the existing typed owners;
the old VTR/virtual-package lookahead and implicit `auto` dispatch are retired.

One owner supplies each large population; reports summarize it rather than
embedding duplicate netlists, maps, or timing-path trees.

Pre- and post-route OpenSTA TSV results use the same native TimingPathDB
importer. The core parses records directly into the current indexed owner,
resolves reached endpoints against compact EmuIR, checks final ordered records
independently, and reduces normalization and original-path QoR without a Python
path population. The bounded host publishes the selector through the common
publication API. Immediate consumers reuse the invocation's generation-bound
validation proof; an explicit standalone audit reopens the canonical owner.
The current OpenSTA API and CLI use that same indexed result route. Inline
result production and the private output-mode/inline-return switches are retired.
Timing-weight generation consumes the typed path owner directly: a native SQL
reduction applies the unchanged maximum path-criticality policy, and a separate
ordered source-membership reducer independently checks every final weight.
Neither operation transfers path or net-weight populations through Python.
Derived weights retain their existing indexed schema and power-law parameters.

The native one-shot controller runs the RTL frontend, pre-placement timing,
partitioning, board routing, Static Exact TDM and baseline Phase 6 through native
stage owners. Timing configuration carries a bounded primitive library, clock
and benchmark I/O contract, and generation-bound source references. The native
timing owner exports the circuit, qualifies and supervises OpenSTA, imports every
original path, independently validates paths and weights, and atomically publishes
one complete directory. It never calls the Python path extraction coordinator.
Explicit prepared boundaries reuse their sealed indexed inputs; a missing native
policy for new timing extraction is an error.

Chimew, physical implementation and Phase 7C still have explicit stage adapters.
Final report assembly is native and no longer has a Python stage adapter.
Configuration preparation and detailed stage ownership are distinct: bounded
Python configuration is not a circuit data plane, and isolated execution alone
does not complete migration. The compiler continues to report
`native_data_plane_complete=false` until those remaining owners and terminal
qualification are complete. See [native compile control](USER_GUIDE.md#native-compile-control)
and the [migration contract](PHASE3_NATIVE_MIGRATION.md).

The remaining production boundaries are defined by execution responsibility,
not language line counts or the presence of typed storage:

| Boundary | Native responsibility | Remaining Python responsibility |
| --- | --- | --- |
| Phase 1–6 baseline | Complete frontend, pre-placement timing, partition, route, schedule and split stage owners | Bounded configuration, tool discovery, reports and explicit prepared-input control |
| Phase 6 Chimew | Grouping/refinement/RUDY kernels and indexed baseline split | Lookahead and pin-planning orchestration, independent grouping/bank qualification and adapter generation |
| Mapped physical inputs | Indexed Bookshelf pin/net writers and placement lowering | Mapped-index SQL projection, atomic/macro fact generation, geometry and window policy |
| Placement and routing handoff | Typed certificate/handoff ownership checks and RWRoute exporter | Site/BEL/primitive checks, handoff record production and independent route connectivity/PIP validation |
| Physical timing | Complete local-path and logic-segment query generation, shared physical object resolution, Vivado boundary/descendant association, indexed evidence import and OpenSTA staging | Bounded backend configuration and tool supervision |
| Global timing | Full-assignment physical coverage, event binding, measurement import, original-path reduction and final flow-report sealing | Runtime generation, engine supervision and the signoff stage |

Indexed atoms and ordinal safe names already avoid complete in-memory lists or
name dictionaries. Local-path evidence already has a typed owner. These are not
outstanding format migrations. Both physical timing query producers now publish
through shared native stage owners; their replaced Python production loops are
retired. Native global timing reduction does not imply that the complete signoff
controller has migrated. The remaining closure sequence is physical input/handoff
and independent route validation, then complete physical/signoff control and
retirement of their old production adapters. Chimew remains a maintained optional path with its own
ownership and qualification requirement.

## Native route publication

The Phase 4 native stage reads the assignment index, constructs the current
routing model, invokes the shared routing kernel, independently validates the
result and publishes the current RouteStore v3 with its bounded selector/report.
Its ordered sealer computes the existing logical identity and relation seals
without constructing a graph JSON document. The explicit standalone seal operation
uses that same core for independent audits; it is not another production route.

## Physical evidence coverage

Phase 7C checks every scheduled TX/RX endpoint and every assignment semantic
segment against the original typed physical owners. Configuration-stable constants
retain their structural zero-delay evidence. The native owner uses ordered joins
and independently audits its temporary relations, including actual ScheduleStore
record seals. The permanent coverage certificate contains counts, logical seals
and source references, rather than another endpoint, logic or board-hop database.
Its temporary relations close before OpenSTA starts. A terminal audit or explicit
diagnostic replay reconstructs them from the authoritative typed owners.

## Logical model

EmuIR retains directed endpoints, hierarchy, cell types and parameters,
resource vectors, clock/reset classification, and cut eligibility. Logical
schema identity is separate from its physical storage representation.

Phase 6 copies the bounded `resource_reservations_by_fpga` seal directly from
the Phase 3 assignment index into its split manifest. Its independent validator
compares that seal with the assignment index, rejecting missing or changed
reservations before the physical backend consumes the split. This control
metadata check does not load the instance ownership graph.

BoardDB describes FPGA nodes, resource budgets, and inter-FPGA links. An
academic BoardDB can omit package pins. Concrete deployment additionally
needs authoritative device resources, physical connections, electrical rules,
reference clocks, reset handling, and a board shell.

TimingPathDB identifies source logical paths and endpoints. Physical binding
must keep same-FPGA and cross-FPGA paths disjoint and preserve each original
member exactly once in the declared global population.

## Boundary semantics and timing

Sequential-only cuts retain unsupported combinational connectivity as atomic
partition units. Generalized Static Exact releases structurally supported
combinational boundaries under its explicit synchronous semantic contract.
See [Static Exact](STATIC_EXACT_COMBINATIONAL_CUT.md).

Phase 3 enforces structural capacity, fixed/group, SCC/cascade, topology, and
hop constraints. It may optimize predicted delay or TDM pressure, but does
not certify future slot readiness or physical deadlines.

Phase 4 chooses system routes. Phase 5 assigns concrete TDM resources/events.
Phase 6 materializes transport and checks macro-cycle semantics. Phase 7
provides physical delays and the global timing result. The
[contest/Phase 6 contract](CONTEST_PHASE6_INTEGRATION_CONTRACT.md) defines the
ownership of route and schedule fields.

## Physical providers

Only the following two physical routes are maintained. VTR/open and its
standalone execution, artifact adapters and build dependencies are retired.

- The RapidWright route uses device facts and RWRoute, with native OpenPARF
  responsible for placement and legalization.
- The optional Vivado route uses the proprietary device implementation tools.

A bridge translates identities and validates the provider result; it must not
silently choose a different placement or architecture. Resource, macro,
cascade, clock-region, and SLR requirements must be admitted explicitly.
Unsupported requirements are errors, not fallback opportunities.

## Scope

A physical flow must retain source timing identities and explicit clock periods
even when optimization is timing-oblivious. OpenSTA is the default global
numeric timing engine; its model consumes physical segment/interface and
transport-event bindings rather than precomputed Python slack.

Academic physical models and board-link models are not measured silicon or
board signoff. Bitstream generation and hardware operation require a compatible
concrete platform. See [timing scope](global-opensta-qualification.md) and
[validation requirements](VALIDATION.md).

## Shared native core (W0)

W0 provides the common infrastructure used by the phase migrations. Its public
boundary is `ArtifactRef → Store/View → Writer → Summary`; the internal API is
C++17. Match headers and library revisions when linking. Versioned artifact and
request protocols are the inter-process compatibility boundary; STL objects,
SQLite handles and circuit-sized Python containers do not cross it.

| Contract | Shared owner | Required behavior |
| --- | --- | --- |
| Build closure | `libemuflow_core`, `src/core/sources.txt` | CMake, native fixtures and thin runtime deployment consume one checked source manifest; migrated kernels have one executable and implementation owner. |
| Schema and source binding | `SchemaRegistry`, `ArtifactRef`, `SqliteStore` | Explicit versions, registered logical envelopes, immutable generations and mixed-source rejection; bounded metadata checks rather than integrity scans or recounts. |
| Identity and order | `ArtifactScope`, `ArtifactId`, typed views | Cell/net/path/endpoint/FPGA/link identities retain their artifact namespace. Route source sequence and canonical net order are distinct coordinates. |
| Storage and publication | SQLite connection/statement leases, `SqliteArtifactWriter`, `AtomicBinaryWriter` | Last borrowers retain connections and private staging. Close connections before retirement; reject active transactions/statements, input aliases and changed sources before atomic publication. |
| Selector commit and recovery | `commit_store_selector` | Back up a bounded previous selector before replacement; preserve the original source bindings through independent post-write validation and directory sync. Close rejected returned views before retiring their SQL, and retain recovery files if closing or rollback fails. |
| Common data structures | `ResourceView`, integer CSR helpers, source string/payload pools | Deterministic record order, bounded resource decoding and explicit extraction of kernel-local integers; no second persistent graph catalog. |
| Failure and measurements | `StageError`, `RunStage`, `StageResult` | Shared error codes and bounded reports; wall/CPU/I/O intervals and process lifetime RSS retain their stated scopes. |
| External engine lifecycle | `RunExternalTool`, `ToolRequest`, `ToolResult` | Literal arguments/environment, bounded diagnostics, limits, deadlines, cancellation and reaping; engines remain separate processes. |

The topology hop legalizer uses the same source generations, errors, metrics
and atomic directory publisher. `phase3 refine-hops-from-primary` directly reads
original source/primary/ownership artifacts and calls the existing deterministic
FM and tiny-case exact fallback through an internal C++ model. Its independent
source endpoint/resource audit does not trust projected nets or kernel counters.
`phase3 audit-hops-from-primary` checks selected ownership without building a
second search model or executing another optimizer. The numeric `refine-hops`
protocol remains an explicit reference/diagnostic input, not a production model
exchange for current native owners. The complete Phase 3 stage/controller now
uses those owners directly; current-design equivalence and performance
qualification remain required W7 evidence.

`EmuirView` reads indexed v1 and compact v2 directly. Explicit v1 reads derive
counts because that historical format has no count manifest; compact production
v2 construction reads bounded metadata. Both expose the same cell-state and
connectivity semantics. Interned pin IDs exist only in v2; validated v1 pins
retain their original port/bit fields without inventing an integer string pool.
No read silently rewrites or upgrades its input.

The maintained owner boundary is below. Each public SQL owner uses the registered
envelope, bound input generations and common closed-file publisher; phase-specific
record construction and independent semantic validation remain with the named
producer/consumer. This census does not imply that those algorithms have moved
out of their Python adapters. Final native Phase 3 semantic nodes feed assignment
publication directly through the shared bounded decoder. Production requests
reference original selected ownership, coordinate and endpoint-count artifacts;
they contain no cluster/hop columns, cut names or predecessor lists. Original
ownership/node/capture/count generations remain bound through final pair commit,
and the independent typed-index/exact-binary checks remain required. The shared
`AssignmentView` validates v4 cut relations using ordered cursors and streams
their canonical digest; it does not call the producer or replay Python records.
The host retains bounded identity checks and the separate exact-binary gate.

| Artifact family | Producer owner | Maintained consumers |
| --- | --- | --- |
| Compact EmuIR v2; explicit indexed v1 reads | Yosys/EmuIR import and private-generation finalizers | Native EmuIR view, partitioning, timing, splitting and physical lowering |
| Primary cluster columns v2, candidate IDs and final cluster catalog | Shared native cluster/candidate/catalog operations | PATRON, assignment and semantic publication |
| Assignment v4; explicit v3 reads | Native assignment producer and indexed publisher | Routing, scheduling, split and transport |
| Timing paths and net weights | OpenSTA import/weight adapters | Partitioning, routing and global timing |
| RouteStore v3 and ScheduleStore v4 | Native route/seal and exact scheduling operations | Schedule generation, split, transport and global event binding |
| Split v2 and transport v1 | Phase 6 split/transport adapters | Physical lowering and boundary/event identity |
| Pin plans, position hints, electrical binding, Chimew bank input/report | Retained Phase 6 adapters and their shared selector commit | Transport/runtime generation and independent electrical/Chimew validators |
| Placement EmuIR and mapped index | Lowering and Xilinx mapping adapters | OpenPARF, Vivado, physical ownership checks and timing export |
| Atomic source, name map, site geometry, certificate and physical handoff | OpenPARF adapters/bridge and shared ownership helpers | OpenPARF host, native RWRoute export and Java implementation |
| Vivado source functions and lineage | Indexed exporter and shared lineage import | Vivado implementation, physical timing and source coverage validation |
| Boundary identity/timing, logic-segment identity/timing, local-path timing | Physical measurement/query adapters | Global STA and terminal physical/timing checks |
| QoR report | Typed report writer | Report consumers and terminal evidence validation |

Private router results, atom indexes and Vivado local-query indexes are scoped
scratch, consumed within their owning operation rather than additional published
graph owners. EmuIR v1 writing remains an explicit offline handoff-qualification
oracle; ordinary production writes compact v2. Primary cluster columns have one
current native protocol, v2; the unconsumed v1 registry entry is retired.

`AssignmentView` supports public assignment v3/v4 and checks primary coverage as
its ordered copy cursor is consumed. Assignment v1/v2 and their migration writer
are retired; historical artifacts require their matching Git revision.
`SplitView` supports split v2, with scoped partition/cell/net/pin coordinates,
replica names, top endpoints and synthetic
shadow identities. References to upstream source sequences require an explicitly
paired source artifact; equal integers never prove a cross-store identity.
`RouteView` and the native ScheduleStore producer use the same ownership and
publication facilities. ScheduleStore has one installed storage contract, v4;
older storage readers, Python snapshot publication and native version aliases
are retired. Cluster catalog, Assignment and ScheduleStore DDL each have one canonical SQL owner
under `src/python/emuflow/data/schema`; native headers are generated from those files,
and bounded test snapshots use the same SQL. Logical and child-relation version
numbers remain independent of the storage envelope. Candidate construction, primary clustering and PATRON
joins borrow the existing `EmuirView` store, sharing its source connection/cache
and lifetime. The schema registry also names mapped/physical handoffs
and boundary, logic-segment and local-path timing stores. Their phase-specific
readers and independent electrical/timing validators retain semantic authority.

Final native-primary cluster publication runs as `phase3 publish-cluster-catalog`
in the shared core. It reads source-bound member/name/resource columns directly
and streams the existing compressed SQLite sections through bounded zlib buffers.
The original native candidate artifact supplies eligibility; an ordered source
cursor checks bitmap/name agreement. Sparse fixed/group configuration stays in a
checked request. The host does not expand cluster records or candidate names,
and native failure never retries the reference serializer. Assignment packing
uses canonical ordinal IDs and member counts instead of decoding each cluster.

Primary membership ordinals and EmuIR ownership source sequences are distinct
coordinates. The catalog independently checks their pairing with a single
ordered source scan; its cell IDs retain the primary artifact namespace.
Lease-bound per-record ownership checks compare the frozen namespace in memory;
physical generation checks remain at input, validation and publication boundaries,
not one filesystem call per cell. Final catalog SQL keeps its logical/storage
schema but uses `clusters.<sha256>.sqlite` generations behind `clusters.json`.
Readers also accept the prior fixed basename for identified historical inputs.
The common selector commit preserves the old pair on failed publication,
directory synchronization or partial backup, and retains recovery files if
rollback itself fails. Durable commits sync the selector's parent directory.
Logic-segment identity uses that same commit owner instead of a separate selector
backup/restore implementation. Its query TSV remains a separately published
companion: failed identity commits restore the prior query, and callers still
require successful stage completion for the complete set. Old payloads
remain invocation scratch until process-aware terminal retirement.

Current compact EmuIR, final assignment v4, RouteStore v3 and ScheduleStore v4
producers hand their closed SQLite files to the common publisher. Indexed Yosys
imports bind their named source before row construction; pipe/in-memory inputs
do not invent a file identity. Routing retains its private native result through
final adoption. Schedule requests borrow the original validated RouteStore and
selector generations rather than recapturing the files after computation.
Assignment publication preserves its existing independent validation and exact
binary rollback; the final SQL commit binds the newly installed binary as well
as the original EmuIR and primary columns.

EmuIR private-generation finalizers run after record insertion and before reverse
adjacency construction, commit and common adoption. The writer retains transaction
and connection ownership. Transport fanout buffering uses that existing connection
while synthesis streams in; it never copies or reopens a published EmuIR. It keeps
its deterministic buffer tree, endpoint coverage, orphan and row-accounting checks
without an implicit database integrity scan. Routed OpenSTA performs timing-only
resource, delay-net and warning normalization through the same boundary, using SQL
sets instead of an expanded delay-net ID list. Original mapped/timing selector and
endpoint payload generations stay bound through final replacement. Transport RTL
and runtime sources are bound before invoking synthesis.

TimingPathDB and net weights publish immutable, invocation-owned SQLite payloads
named by their file digest, then commit the small selector. Consumers follow the
payload descriptor rather than assume a fixed `.paths.sqlite` or `.weights.sqlite`
filename. Failed selector publication restores the previous selector and removes
only a newly created uncommitted payload; an incomplete backup never replaces a
valid prior selector. Prior payloads remain ephemeral within their owning run
until process-aware terminal retirement, not a persistent checkpoint cache.
Vivado source-function stores and the final QoR SQL owner also use common
adoption; their independent semantic validators remain required. The unused
optimization-proof producer has been retired. Separate Verilog, report and
selector files are not an atomic multi-file
transaction, and consumers still require successful stage completion.

OpenPARF site geometry is built once into private SQL staging and adopted through
the same boundary. Later per-FPGA paths may be hardlink aliases of that immutable
owner: a private alias is replaced atomically, preserving an old target if link
creation or replacement fails. Expected ctime changes from this known link
operation are accounted for without dropping inode, size and content checks.
Only an explicit cross-filesystem link failure triggers normal materialization;
other I/O failures stop the operation. Hardlinked geometry is never disguised as
private single-owner SQL staging or copied solely to satisfy that requirement.

Phase 6 registers one transport store for endpoint, lane, anchor and virtual
binding views, plus position hints, pin plans and the retained Chimew input,
electrical-binding and bank/channel report stores. Split publication adopts the
same common writer boundary and accepts the supported assignment v3/v4 owners.
Their producers hand closed same-directory SQLite staging to the common
`publish-store REQUEST OUTPUT` command. The checked bounded request names the
registered store schema, staged generation and original source generations.
`SqliteStore` validates the envelope and releases its reader before the common
publisher syncs and renames the existing file; it never copies or rewrites the
database. The result uses the normal stage errors and metrics. An optional
report must name a fresh invocation-owned file. Active source, staging or
destination journal/WAL state, aliases, unknown versions and changed generations
reject publication. There is no alternate Python publisher on failure.

Schedule, position/pin-plan and Chimew input/binding/report readers preserve both the selector
and sidecar generations established at construction. Producers borrow those
bindings instead of capturing a later snapshot. A transport producer hashes its
published database once: all selectors, summary and returned lazy views reuse
one invocation-scoped file seal, checking its path, schema, bytes, digest and
generation. Independent consumers still verify their input file; no global
hash cache or persistent catalog is introduced. Phase 6 row construction and
electrical/RTL/XDC checks retain their existing semantic owners and still require
their own data-plane migration and qualification.
Pin-plan, position-hint, electrical-binding and retained Chimew bank input/report
selectors use the common commit helper. Their existing independent view validators
remain active. Typed bank-input relocation uses the same helper and the original
source bindings; it has no separate selector restoration implementation. A source
change during selector replacement or validation rejects the whole operation.

Phase 7 mapped indexes, placement IR, OpenPARF atomic sources/name maps,
placement certificates, physical handoffs and boundary identity/timing,
logic-segment identity/timing and local-path timing producers adopt the same
closed-file publisher. Placement IR binds the source split and transport EmuIR;
atomic sources bind their mapped owner before consuming its records. Physical
and timing readers preserve selector/store generations before independent input
validation, and derived writers borrow those original bindings through the
selector commit. The transport views and zero-delay local identity projection
forward their existing owners instead of inventing new snapshots. RapidWright
and Vivado timing consumers pass typed identity scopes; Vivado measurement
imports also bind the input measurement file. These checks neither reconstruct
the graph nor repeat a file hash.

The mapped-index writer binds compact EmuIR before opening it. OpenPARF export
and placement validation retain original document references and borrow the
mapped/name-map/atomic-source store scopes instead of refreshing their
identities after consuming records. The handoff builder keeps its existing
source guards and forwards the registered mapped, source and certificate
references into the shared publisher. This includes active SQL sidecar
rejection; opaque documents continue to use their phase-specific semantic
validators. The name-map and placement-certificate selectors commit after their
SQL generation. A rejected selector commit removes only a newly adopted,
uncommitted generation, preserving the previous selector and referenced store.
This is single-file atomic publication; an explicitly requested companion JSON
export is not a multi-file transaction.

Chimew semantic validation releases owned typed report readers on exceptions as
well as success, before discarded generation files are retired. The borrowed
bank-input lease remains with its caller. Transaction contexts alone do not
close SQLite connections; callers and qualification fixtures must explicitly
close database and physical-evidence owners before removing their directories.
Strict generation inventory validation still rejects unexpected files.

Explicit timing-selector relocation copies into private same-directory staging,
then uses the common publisher and checks the original input scope before
committing its selector. A failed source check removes an uncommitted new
sidecar and preserves the prior selector/store. An output alias of a live input
is rejected before acquiring its exclusive selector lease. Existing logical
schemas, ordered records, independent legality/timing checks and the query
sidecar rollback contract remain unchanged. This infrastructure adoption does
not qualify the remaining phase algorithms or replace real-design acceptance.

Typed Chimew input relocation also forwards its original selector/store bindings
to the common publisher when adopting a copied closed sidecar, and checks those
bindings before selector commit. It preserves its rollback and independent reader
validation without reconstructing the logical group population.

Indexed Chimew input republication streams its bank/group cursors and borrows
the validated logical identity instead of materializing the complete input.
Writing back from the same active input view is rejected before acquiring its
exclusive writer lease. A different input view retains its source leases until
the derived store has committed.

Split publication binds EmuIR, assignment, schedule and any Static Exact binary
source. The existing binary sidecar contract retains its same-filesystem
read-only hardlink or explicit cross-filesystem copy. An existing identical
hardlink is reused. Creating and renaming an owned hardlink may advance ctime;
the producer checks that inode, bytes and mtime remain unchanged, then binds the
generation after that known alias operation. Other source changes are rejected.
Full generation checks, including ctime, cover construction between those
individual alias operations; the exception does not span the split build.
Binary and split-database names remain separate file commits; consumers require
the successful phase result and its independent validation before using the set.

The indexed OpenPARF bridge calls `phase7 validate-physical-cells` before
publishing its handoff. The shared `SqliteStore` readers compare both directions
of mapped/physical ownership, constant membership, pin owners and complete port
ordinals, then check every mapped integer output bit for duplicate drivers.
Only a bounded checked request and result cross the process boundary; no Python
cell catalog, pin reconstruction or second all-driver database is used for these
checks. The caller still checks geometry and FF controls independently per site,
and validates MUX/CARRY topology and dedicated cascades. Native errors stop
publication; they never select the Python checker as a fallback. Explicit logical
JSON qualification inputs retain their existing mapping checks.

For a typed atomic source bound to an indexed mapped owner, the bridge also
calls `phase7 validate-atomic-ownership`. Registered source and certificate
stores are read through `SqliteStore`. Ordered mapped cells, source macro and
constant records, and certificate assignments are merged directly; singleton
source IDs use the original mapped rank, including constants. No source or
certificate ownership mirror is published or staged. Source seals, versions,
counts, unmatched records and per-cell types remain mandatory. Borrowed source
and certificate readers retain their original file generations, and the handoff
publisher binds those same files through final replacement. Explicit logical
qualification inputs keep their existing checks; a native failure stops the
typed production path without fallback.

Static Exact analysis and the four Chimew kernels have no standalone production
wrappers or separate build targets. Chimew grouping, refinement and bank/channel
assignment expose only typed inputs; the old text parsers, writers and registry
entries are retired. Chimew kernels write to caller-owned streams. Their file output passes
through `PublishTextArtifact` and `AtomicBinaryWriter`: producer failure, bad
stream state or changed inputs cannot replace an already published result.
The stage API and CLI share this policy. Stdout is an uncommitted stream until
the consumer verifies complete records and successful process termination.

Exact analysis, final semantic nodes, capture records, canonical semantic hashes
and section summaries pass their bound `ArtifactRef` inputs into the shared
binary writer. PATRON uses the same writer for typed and diagnostic text output.
The writer verifies source generations when opened and again after file sync,
immediately before rename. Source change, disappearance or destination aliasing
rejects publication and preserves a prior destination; this check reads metadata,
not the graph or another serialized copy. Primary-store PATRON publication also
passes source bindings when publishing its order/result/summary files. These are
separate file commits, not an atomic multi-file transaction. A successful stage
result remains required before consumers accept the complete output set.
Native clustering, candidate IDs, partition summaries, Bookshelf/RWRoute
exports and Vivado lineage use the same source-bound commit. The shared SQLite
writer checks inputs again after sync; routing publishes through that owner
rather than creating the final database directly. Routing and ScheduleStore
construction retain the stage dispatcher's input generations instead of binding
a later snapshot of those paths.
Capture production has only `build-captures` plus the independent
`validate-captures` checker; the older seq-index publisher is retired.

W0 closes only after versioned round trips, deterministic ordered records and
negative cases pass: unknown versions, truncation, duplicate keys, orphan or
mismatched references, mixed generations, active SQLite sidecars, interrupted
publication and borrowed cursor lifetime. The same frozen NVDLA input must
establish equivalent records and measure the pure integration overhead against
the prior implementation; the permitted regression is at most 5%. Keep hashes
and measurements outside production handoffs. Fixture/API gates alone cannot
establish the large-design performance gate.

Thin runtime deployment borrows only the named maintained synthesis, timing,
partition and board-helper dependencies. It does not copy every executable from
an older installation. The unified entry and shared core are rebuilt from the
exact deployed source; retired and unknown programs cannot re-enter through
that dependency prefix. Borrowed tools retain their separate base identity.

W0/W7 closure also requires retirement of superseded execution paths. Once the
replacement has passed the required equivalence and performance gates, remove
the old production entry, configuration switches, deploy/build targets and
unconsumed protocol readers/writers together. Historical rollback uses the
matching Git revision. A schema version is retained only for a current producer
or an identified artifact consumer, not merely because it was once published.
Keep independent correctness oracles in validation code without preserving a
second production flow. Migrate remaining consumers before deleting their
entry; a test of an obsolete wrapper is not a reason to keep that wrapper.
Native production resolution requires the unified entry and does not recover
an incomplete selected installation by borrowing another tool installation.

W0 completion does not mean the remaining phase algorithms, Python data planes,
advanced compile controls or physical/global timing qualification have migrated.
Those retain the W1–W7 ownership and acceptance requirements. An infrastructure
closure must not be reported as completion of the whole native production flow.
