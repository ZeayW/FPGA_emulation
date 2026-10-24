# Native data-plane migration

## Architecture and ownership

The production target is one `libemuflow_core` and an `emuflow-native compile`
entry that owns execution of the complete flow. External Yosys, OpenSTA,
OpenROAD/TritonPart, OpenPARF, RapidWright/RWRoute and optional Vivado remain
separate engines. OpenPARF may retain its required thin Python/PyTorch host.
A native command that only invokes the Python compiler is not this target.

The current native executable links real Phase 3, 4, 5 and 6 kernels through a
shared typed operation API, including PATRON search. An opt-in native controller
now owns the ordinary one-shot phase sequence through isolated shared Python
adapters. Advanced orchestration and remaining native data-plane ownership are
unfinished. W0 shared-core
consolidation and W7 native compilation are the priority architectural delivery;
stage adapters must converge on those interfaces rather than add independent
parsers, identity maps, error formats and publication implementations.
Completion also requires retiring superseded execution chains, unused schemas,
standalone entry points and installation fallbacks. A disabled compatibility
switch alone is not a reason to retain a complete implementation. Keep only
readers with a demonstrated current consumer, and record their exit condition;
historical replay uses its matching Git version. Independent correctness
oracles may remain in tests without retaining a second production flow.

See [phase contracts](FLOW_PLAN.md) for the whole-flow architecture,
[algorithm implementations](ALGORITHMS.md) for providers, and
[physical acceptance](XILINX_PHYSICAL_FLOW.md) for backend requirements.

## Shared core (W0)

The core owns representation-scoped typed IDs, checked artifact I/O, common
CRC-32/SHA-256, SQLite statement/transaction lifetimes, and atomic publication.
The linked operations use a versioned schema registry, `ArtifactRef`,
`StageRequest` and `StageResult`. `SqliteStore`, `EmuirView`, `AssignmentView`,
`SplitView` and `RouteView` share artifact binding and connection ownership;
`SqliteArtifactWriter` owns staged SQLite publication, including ScheduleStore.
EmuIR v1/v2, assignment v1–v4 and split v2 have explicit versioned readers.
The [W0 contract](FLOW_PLAN.md#shared-native-core-w0) specifies shared interfaces,
compatibility and acceptance separately from the later phase algorithm migrations.
An integer identity is meaningful only with its owning artifact;
equal integers in different stores are not interchangeable.

`phase7 import-vivado-lineage MAPPED FUNCTIONS REQUEST STORE SUMMARY INVENTORY...`
is a shared-core operation exposed through the same typed API. Mapped input,
versioned source-function signatures, the small request and every inventory are
bound `ArtifactRef`s. The importer uses the common SQLite RAII, registry, error,
metric and atomic-publication contracts. It streams census records into the
canonical source/placement owner without Python per-cell parsing. Only the
selected stage is retained. `check-runtime SCHEMA` verifies an explicitly
registered version; Vivado compile/standalone preflight requires the lineage
schema before synthesis or vendor implementation. Write-store connections explicitly enable URI
handling before attaching immutable source stores. This operation does not
complete the remaining Vivado RTL exporter or W7 data-plane migration.

Python SQLite adapter scopes close their connection explicitly as well as
commit or roll back their transaction. The SQLite connection context manager
alone does not close the handle. Prepared timing readers, native ScheduleStore
summary readers and physical certificate/site adapters release their handles
before publication or phase retirement, including on validation errors. Tests
retain connection objects beyond those scopes to verify actual close behavior;
resource release cannot depend on garbage collection or filesystem unlink rules.
The same rule covers SQLite connection factories behind lazy physical name maps,
atom lookups, boundary identity/timing, logic-segment identity/timing and local
path timing. Exhausted or explicitly closed iterators release their connection;
live iterators retain it until their consumer closes or finishes them. A reader
lease remains explicitly owned by its view and must close before that view's
publication is retired.
Python adapters construct immutable input URIs through `sqlite_artifacts` and
explicitly enable URI handling on connections that attach published stores.
This preserves read-only inputs independently of SQLite's build-time URI
default and protects path characters such as `?`, `#` and `%` from URI parsing.
These operation implementations and their supporting contracts are compiled
into one `libemuflow_core` target. The former `emuflow_native_stages` target is
only a CMake compatibility alias; the executable and native library tests link
the core directly. This does not yet cover every standalone adapter or complete
the native production data plane.

The duplicate Static Exact analyzer and four Chimew executables/build options
are retired. Tests and current adapters call the same unified entry and shared
kernel implementation. Chimew's superseded text readers/writers and schema
registry names are removed. Its grouping, refinement, bank and RUDY kernels
accept a caller-owned output stream; the shared stage layer publishes file
outputs only after production completes and source generations still match.
Atomicity failure gates exercise early parse rejection, late producer failure,
changed sources, failed output streams and prior-result preservation. Pipe
outputs retain their streaming protocol and require complete record validation
plus process success. This infrastructure change does not complete W0's frozen
NVDLA overhead gate, the remaining adapter integration or W7's native data plane.

`phase3 refine-patron-from-primary EMUIR TIMING PRIMARY INITIAL REQUEST OUTPUT`
builds the PATRON v14 model and invokes the existing solver in one shared-core
process. Normal compact native clustering selects this direct owner. The shared
`PrimaryClusterView` reads source-scoped ownership and resource columns without
copying them into another membership/resource artifact or transposed C++ matrix.
The Python adapter carries board-sized configuration and packed initial ownership
plus fixed constraints. It does not create pressure-net/path/name populations,
derive a second model or stream per-record solver input. The snapshot command,
cluster-column protocol and v1 board request are retired. Missing primary
ownership fails without generating a replacement snapshot. Final semantic/index
publication still has Python work.

The shared model is move-only, and both its typed pressure-model reader and direct
store producer call the same search implementation. The producer checks primary
cell/membership coverage, pin identities, sparse endpoint ordinals, ordered timing
paths/net references, fixed ownership, resource/balance limits and bank symmetry.
Capacity and route policies retain their existing meanings; no schedule-readiness
or Static Exact feasibility guard is introduced. Input CRCs and retained file
generations reject truncation, corruption and mixed sources before solving.
The output keeps the existing typed assignment format. A sealed integer order
sidecar binds its lexical native coordinates to canonical cluster ordinals,
including identities longer than six digits; Python maps it without a name pool.
Only counts and implementation identity return in the bounded
[model summary](../schemas/patron-native-model-v1.schema.json).
The summary also separates model-build and search operations using the common
wall/CPU/I/O metrics. Their RSS values are process-lifetime high-water snapshots,
not incremental stage memory; the enclosing sampled process-tree peak remains
the whole-phase memory measurement.
Normal handoff still runs the independent assignment validator and candidate
selection before final semantic/index publication.

`phase3 reduce-loads EMUIR PRIMARY REQUEST OUTPUT` independently reduces the
primary resource columns under the final packed assignment. Construction of
compact partition summaries, balance validation and final online validation use
this common operation rather than expand every cluster's names, membership and
resources. The request binds the source and primary generations and streams one
integer part per canonical cluster; it does not carry solver resource totals.
Only per-partition cluster/cell/resource counts return to Python, where the
existing independent capacity, reservation and share equations remain unchanged.
The final validator checks instance/cluster ownership agreement, fixed/group
constraints and source-bound membership, and reuses its independently reduced
loads for its balance equations. Unknown owners, missing coverage, source
changes, malformed requests and checksums reject before atomic publication.
Native consumer failure never retries through expanded reference records.
Instance ownership borrows the existing member offsets and, when catalogs
match, the packed part column; it does not rebuild cluster IDs or member counts.
The public assignment and balance-report schemas are unchanged. Final semantic
record construction and index publication still require further native ownership.

`phase3 build-captures EMUIR REQUEST TOPOLOGY PARTS OUTPUT` owns compact capture
production and independent source validation in one shared-core operation.
Inputs keep their current EmuIR v2, exact request v4, topology v3 and part-catalog
v1 envelopes; the final capture stream remains byte-compatible v1. Python sends
file references and the bounded part catalog, then reads the checked output. It
does not replay capture coordinates, decode the capture population, retain a
coverage bitset or issue a SQLite lookup for each capture.

The producer writes one ordered cursor directly rather than maintain the legacy
per-capture seq index through repeated select/update statements. Its private
normalization spool closes and is removed before validation. The independent
checker does not consume that spool: it rebinds topology coordinates to source
endpoints, validates dense ordinals despite stored ordinal gaps, checks owners,
architectural-state resources and exact pin/bit identities, and merges its own
canonical relation with the immutable capture stream. Missing, extra, reordered
or changed records reject. Coverage is relative to the declared topology
coordinates; graph traversal and downstream equivalence/timing retain their
separate qualification gates. A complete required source cursor also checks sink
coverage without a separate recount. Its temporary relation is retired on exit.

Shared `OrderedRecordTable` decoding is also used by native semantic hashing and
section emission. `StagedArtifact` owns exclusive same-directory staging and
private sidecars for both PATRON and capture operations. A capture result replaces
an older generation only after independent checking and source-generation checks.
`phase3 validate-captures EMUIR REQUEST TOPOLOGY PARTS CAPTURES OUTPUT` exposes the
same independent checker with a bounded JSON proof. Explicit legacy
`resolve-captures` comparisons retain their seq index; production never falls
back to that path or to Python when the unified native operation fails.
The Phase 3 profiler records a nested `static_exact.native_capture_build_and_validation`
interval and native capture count. The interval belongs to its enclosing semantic
operation and must not be added to that enclosing time. Final semantic-node
metadata construction, other Python semantic traversals and assignment/index
publication are still separate migration work. Native node-reader rejection
explicitly releases its mapped views before retiring staging, so NFS cleanup
cannot mask the original semantic validation error. The reader also releases
partially constructed mappings on malformed input.
Native node and capture readers reuse the shared column/record mapping owner.
It retains registered views and any borrowed record iterator or scalar decoder;
dropping the outer view cannot retire a live borrower. Producers transfer their
private scratch to that same owner. There is no independent temporary-directory
weak-reference finalizer: normal release and exception cycles both release views,
unmap, then retire scratch in that order. Explicit close invalidates borrowed
cursors. Readers of external published inputs unmap without deleting those inputs.

Small/inline and explicitly selected external tools, retained diagnostic traces,
physical-feedback algorithms and explicit typed input copies keep the reference
model path. A selected native producer never retries through that path. Its
`native_input_transport` is `native-primary-store-model-v2` for direct primary
ownership. Input bytes count only the emitted packed initial/fixed ownership
and bounded request; the primary store is referenced. Input records count those
two emitted private artifacts rather than pressure vertices/hops. They are phase-owned scratch,
not checkpoints. Full NVDLA runtime/RSS/disk and terminal Phase 7/7C qualification
remain required before declaring the migration complete.

`phase7 export-bookshelf MAPPED ATOMS NETS SUMMARY SPOOL` is also a shared
`StageRequest`/`ArtifactRef` operation. The production adapter requires the unified
entry; the redundant standalone Bookshelf CLI has been removed. The core owns
typed SQLite reads, immutable input paths, ordered atom/pin joins, endpoint
spooling, degree/driver checks, atomic text publication and scratch lifetime.
Two ordered cursors preserve numeric net order and stable driver-first endpoint
order without a per-net fanout vector. Counts and primitive pin signatures are
the only summary returned to Python. Existing mapped v2 and private lexical
atom relations retain their zero application/user PRAGMAs; their stage-owned
metadata and ordered-record checks remain required. This integration does not
complete the other physical exporters or full W0/W7 qualification.

`phase7 export-rwroute MAPPED PHYSICAL REQUEST TSV SUMMARY` now owns the complete
production TSV export from mapped index v2 and the typed physical handoff in the
shared core. Its small request binds source generations and carries board I/O
and resource configuration only. Placed cells, RAM properties, LUT6_2 route
ordinals, closed clock-gate qualification and net/pin rows stay native. The
explicit Python cursor reference is reserved for compatibility and independent
differentials; it is not a failed-native fallback. See the authoritative
[physical export contract](XILINX_PHYSICAL_FLOW.md#native-rwroute-export).
This closes that exporter boundary, not all physical adapters or W7.

`phase5 build-schedule ROUTE_STORE BOARD_REQUEST GENERATION_REQUEST OUTPUT`
owns the complete current indexed exact producer. It builds the integer model
from v3 RouteStore, computes a typed in-process solution, derives the dependency
and sink-readiness certificates and domain/phase metrics, then publishes the
existing v4 ScheduleStore through the shared writer. Python sends only bounded
board configuration, static header fields and domain templates. It never imports
the complete native result or builds route/predecessor arrays, completion offsets,
hop-sequence arrays or certificate populations for this producer. No numeric
input/result files are written or reread on this path.

The common model and solver preserve tree traversal/order, predecessor ranks,
capacity budgets, fixed lane/ratio groups and affine-delay cycle rounding.
`ComputeStaticExactSchedule` returns typed records directly to its C++ caller.
The numeric model/result readers and writers, Python result import owners,
standalone Exact scheduler and split scheduling/publication commands have been
removed. Current native production has one complete producer and no intermediate
wire-format replay path. Negative tests exercise RouteStore/configuration sources,
publication ownership and the independent staged-store validator directly.

Canonical and pretty logical hashes are accumulated from typed records during
publication. Original route edge order, Unicode identities, nullable ratios and
section seals are retained. Bounded floating-point header/domain values use the
same shortest round-trip decimal convention as Python, including its exponent
thresholds and signed zero. The shared `RouteView` reads identities and original
edge order from the authoritative RouteStore through ordered joins; there is no
additional graph identity catalog or per-route SQL lookup.
The complete producer keeps the publication identities and original integer
edge order from its model-building scan and moves them into the writer;
publication does not rescan RouteStore to reconstruct them.

This production path requires current v3 typed route input and the unified
native scheduler, regardless of the route count. Indexed production never
selects Python because a partition happens to have fewer cut routes, and a
standalone-only installation is rejected for this boundary. Older storage and
dictionary inputs and supported registered-boundary/ratio providers still use
the common Python schedule builder; it no longer selects native result import
by a circuit-size threshold. A native failure never
retries that path. Requests retain the
source file generations captured by their producer; stale references are rejected
before computation and rechecked before atomic publication. The Python layer
still commits the compact manifest and report. Phase 5 reports identify the
store and producer implementations separately from the schedule algorithm.
Storage wall time includes native calculation for the complete producer and is
labelled with its measurement scope.

Independent schedule validation runs against the staged typed store before
report commit. It does not rebuild the reference validator's complete route/hop
dictionaries. Native domain summaries use bounded counts over the in-process
solution. Timing reconstruction consumes the staged typed view, and opening
that freshly sealed producer-owned store does not repeat its full file hash.
Compact output and indexed validation apply without a managed-storage flag;
legacy TSV/transport JSON/testbench/feedback exports and reference simulation
are not implicit native-production outputs. Small reference and explicit
compatibility paths retain their export behavior; qualification tools may
request those functions explicitly.

The independent Python validator still has compact route-scale working data;
non-exact providers, explicit compatibility result import, timing adapters and
advanced phase orchestration are unfinished. This delivery does not
complete W7 or establish whole-stage performance gains.
Deployment probes load the unified entry and physical bridge before publishing
runtime provenance; launchers must repeat them in the actual execution
environment before expensive computation.
`emuflow-native check-runtime` exercises SQLite version and JSON features without
creating graph artifacts or an on-disk database. Its optional `--report` uses
the common operation-result contract. External EDA engines retain their own
qualification requirements.

SQLite owns relations requiring joins, uniqueness or bounded lookup. Large
ordered populations may use typed binary sections or ordered record streams;
they must not be expanded into SQLite solely for container uniformity.
Compatibility JSON is an explicit exporter, not a second authoritative owner.

Normal handoffs verify schema, source identity, required row counts and circuit
semantics. They do not run SQLite `quick_check` or `integrity_check`. Strong
hashing belongs to publication/import and explicit terminal verification,
not repeated routine reads of already validated immutable artifacts.

`emuflow_core/emuir_view.hpp` supplies the compact EmuIR v2 read-only view used
by the production Phase 3 analyzer. Its cell-state and connectivity visitors
consume ordered records and check sequence, ownership and declared coverage in
that same scan. Construction reads bounded metadata; it does not preload or
recount the graph. Sparse endpoint ordinals, top-level endpoints and empty nets
retain their existing meaning. Algorithms explicitly extract local integer
sequences for their own arrays; public cell/net IDs retain the source artifact
namespace. A lookup using an ID from another artifact is rejected even when its
local sequence matches. This does not yet migrate legacy v1 readers, noncompact
capture adapters, mapped physical stores or the other remaining phase views.

### Native operation API

`emuflow_core/native_pipeline.hpp` defines the typed in-process operation
interface; CLI parsing is a separate adapter. The library executes the native
implementation directly, with no Python invocation or graph-sized FFI transfer.
PATRON uses the unified entry, including when its path is supplied explicitly.
The redundant in-tree standalone CLI and legacy text input parser are retired.
`EMUFLOW_PATRON_INPUT_COPY` publishes the same CRC-checked typed binary stream
as pipe input, with atomic replacement and rollback on publication failure.
An explicit external comparison engine remains a separately supplied input. A present unified binary that
rejects a request is an error, not permission to retry an older implementation.
The new PATRON operation defaults to compact typed summary output; detailed
text traces require explicit `--trace-output`. Existing Python qualification
calls select that mode explicitly when retained trace evidence is requested.

`ArtifactRef` binds an explicit registered schema to a canonical path and a local
file generation (device, inode, size, modification and change timestamps).
Generation checks run before and after computation. File envelopes are checked
without full payload loading or rehashing. SQLite inputs must be finalized
single-file publications; active WAL/journal sidecars are rejected. Pipe inputs
retain the stage's CRC/EOF and semantic parser without consuming their prefix
twice or creating a scratch copy. Text compatibility grammars likewise remain
stage-owned rather than being guessed from file contents.
Native callers pass producer `ArtifactRef` objects directly; the library
checks their declared schema and retained generation before execution. It does
not silently rebind stale producer references to whatever now occupies a path.
CLI paths remain an explicit import boundary. Supplying both bound artifacts
and paths for the same request is rejected as ambiguous ownership.

Local generation metadata is not a cryptographic or portable semantic identity.
Producer seals, artifact namespace binding, cross-input provenance, row counts
and independent circuit validation remain required. The registry does not
replace those checks with a successful header read.

`emuflow-native --report RESULT_JSON PHASE OPERATION ...` optionally publishes
the bounded [operation result](../schemas/native-stage-result-v1.schema.json)
atomically. It includes registered input/output references, status, an error
code/message and common metrics. Payload stdout stays available for existing
stream protocols. Report paths may not alias inputs, outputs or the capture
index; invalid requests are rejected without overwriting those artifacts.
Errors use nonzero exits and never trigger a silent reference fallback.

Wall time describes the operation; CPU counters are process-interval deltas.
Parallel operations in one process may overlap, so their CPU/I/O deltas are not
exclusive stage costs and must not be summed. RSS is explicitly the current
process's lifetime high-water mark, not a sampled per-operation/process-tree
peak. Linux I/O counters are self-process physical read/write deltas; unavailable
counters are null. An operation result does not qualify a complete phase or
substitute for whole-flow provenance, validation or timing evidence. Exact
section emission still leaves final manifest publication with its owning outer
publisher; its native operation result is the section summary, not a claim that
the complete Phase 3 assignment has been published.

## Native production entry (W7)

`core::Compile` and `emuflow-native compile REQUEST_JSON` execute the fixed
preflight → frontend → timing → partition → route-policy → route → TDM → split →
physical → signoff → finalize sequence. The opt-in CLI bridge is
`multi-fpga compile --compile-implementation native-control-v1`. The native owner
does not call the Python compiler. Each transitional Python adapter invokes one
shared phase body and exits, releasing its working population before the next
phase starts. The existing Python controller uses the same phase bodies and
retains advanced modes. Current defaults resolve from the shared phase API
signature: PATRON v14 and generalized Static Exact. Cut mode does not infer an
algorithm version, and the redundant flow-refinement selector is retired;
both requested and effective configuration are recorded in the separate native
result. See [supported modes and contracts](USER_GUIDE.md#native-compile-control).

The controller binds the request, adapter and interpreter through `ArtifactRef`,
checks phase order, run identity and request generation, and claims an empty
output directory. It validates the terminal flow-report envelope before atomic
controller-result publication. Stage failure or cancellation never advances or
retries the sequence. Previous control state is removed after the next adapter
consumes it; no checkpoint DAG is created. Canonical phase artifacts retain their
normal validation and publication contracts. Controller validation is not a new
electrical/timing oracle or a replacement for terminal flow validation.
Phase temporary/cache roots are isolated and retired after successful execution
and state validation, rather than accumulated until the complete flow exits.
Control-state paths cannot refer into those roots through absolute paths or
output symlinks; required downstream stores must be published outside them.

This is a native orchestration milestone, not completed W7. Python phase data
planes and several direct engine adapters remain; cross-stage/frame search,
Chimew comparison and serial BSP are not supported by native control yet.
The controller reports `native_data_plane_complete:false`. Full native data-plane
ownership and real-design Phase 7/7C qualification remain required for completion.

`core::RunExternalTool` owns actual engine execution: explicit executable/argv,
working directory, inherited or replaced environment, optional wall/CPU/address
space/file-size limits, a separate process group, deadline/cancellation escalation
and `wait4` reaping. It does not invoke a shell. Empty argv elements and
environment values remain distinct from missing fields; executable, working
directory and environment names remain nonempty. Engine diagnostics use a fixed-size
tail rather than a full temporary log or Python string. Forwarded stdout remains
a direct engine-to-importer pipe for production Yosys; document bytes never pass
through the request/result protocol. A failed importer cancels the supervisor,
which reaps its engine before the adapter removes request scratch. Remaining
members of the engine process group are killed when the leader exits. This is
process-group ownership, not containment of deliberately daemonized processes.

The CLI boundary is `emuflow-native run-tool REQUEST RESULT_JSON`; its bounded,
versioned binary request carries only tool configuration. The atomic
[tool result](../schemas/external-tool-result-v1.schema.json) embeds the common
operation result and distinguishes launch errors, engine exit/signal, timeout
and cancellation. Diagnostic bytes use hex encoding so invalid UTF-8 cannot
corrupt the result. Engine CPU and maximum-process high-water RSS from `wait4`
remain separate from supervisor metrics; neither is a simultaneous process-tree
RSS measurement. Tool artifact semantics and whole-phase publication remain the
owning phase's responsibility.

The installed production Yosys stream and OpenSTA version/extraction adapters
select this runtime from the tool's unified sibling or configured native install.
The bounded provider-output helper uses the same owner. Explicit comparison tools
without a unified installation retain the Python launcher. Once a native runtime
is selected, failure never retries the engine through Python. Other direct
subprocess adapters and advanced compile orchestration are still unfinished.

The complete entry must resolve effective configuration, execute dependencies,
manage external tools and resource limits, collect stage metrics, and publish
terminal evidence through the shared core. Each stage consumes artifact
references and bounded configuration and returns references plus summaries.
Graph-, hop-, endpoint- and path-sized Python containers are outside the native
production contract. Python reference algorithms and explicit legacy exporters
remain separate from production implementation selection.

Delivery requires an actual complete compile, not only CLI parsing, a plan
printer, a kernel dispatcher or a subprocess wrapper around the Python CLI.
Defaults, provider/policy identifiers, seeds, failure behavior and external
tool identities must remain explicit. Unsupported combinations fail closed;
there is no silent implementation or provider fallback.

## Stage ownership boundaries

| Stage | Existing native/indexed foundation | Remaining delivery obligation |
| --- | --- | --- |
| Phase 1–2 | Native synthesis/STA engines; compact indexed inputs | Consolidate import, identities and timing adapters under the shared artifact API |
| Phase 3 | Shared-core candidate eligibility and primary clustering; native v14 store-based pressure construction/search and semantic/capture production; native exact-binary writer | Migrate remaining policy/assignment publication and reference modes; qualify complete stage costs |
| Phase 4 | Native routing; typed RouteStore/results | Remove remaining graph-level adapter work and integrate complete native stage lifecycle |
| Phase 5 | One native exact model/solver/certificate/store producer; independent staged cursor validation | Native independent validation, timing adapters and non-exact interfaces |
| Phase 6 | Indexed split/transport/binding owner; streamed RTL/XDC | Complete native producers and adapter queries while preserving electrical contracts |
| Phase 7 | Indexed lowering; typed physical handoff and native Bookshelf pin export | Complete remaining backend/export/validation ownership and full physical qualification |
| Global STA | Typed segments/results and bounded identity/stitching views | Native check/input construction, result import and ordered complete-path stitching |

Native computation does not eliminate algorithmic search cost. Compare search,
imported-solution and frozen-output replay as separate execution modes. Porting
an adapter must not change the algorithm budget or weaken independent legality.

## Structural candidate production

`phase3 build-candidate-ids EMUIR POLICY OUTPUT` owns candidate resource/pin
classification, adjacency construction and iterative SCC analysis inside the
shared core. The policy is bounded and binds the EmuIR generation; cells, nets
and pins use the shared artifact-scoped identity view. A common bounded resource
decoder is used by candidate analysis and primary clustering.

The integer graph has forward CSR only. Parallel logical arcs preserve SCC
membership, so the producer needs neither a temporary SQLite arc table nor a
sort/deduplication pass. Net identity on each arc lets cycle rejection project
back to candidates without another endpoint scan. Architectural state remains
outside that graph. Soft drivers, complete driver multiplicity, cyclic sinks,
hard/opaque resources, safe sequential input ports and top-level pins keep the
reference rules; readiness, slot feasibility and downstream timing remain absent.

A single lexical cursor streams eligible names into the sealed output, followed
by integer offsets. Python borrows that view without rebuilding graph arrays or
a name pool. Compact production clustering and the large native exact analyzer
select this operation. Explicit characterization/reference APIs keep their own
independent implementation; a selected native failure never falls back.
The name-facing policy export remains a conversion boundary to retire during
final publication work. Normal native primary-to-pressure handoff reuses the
primary artifact; full membership/resource snapshot protocols are retired.

## Primary cluster production

Compact production clustering is a shared-core operation behind
`phase3 build-clusters`. It consumes the authoritative compact EmuIR and a
same-generation packed eligibility/group request. C++ owns union-find,
canonical lexical membership, integer CSR columns and resource sums. An illegal
high-fanout net is unioned as its endpoints arrive, without retaining a net
membership set. Resource payload caching has a fixed bound independent of cell
count. Group unions and fixed ownership preserve the reference behavior.

The private v2 primary envelope binds the source generation and carries sealed
resource totals, with header and full-column CRCs. Shared native readers validate
source/count/membership/resource coverage and retire completed projection pages
before constructing the next model population. Python uses the header totals for
TritonPart dimension/totals queries without scanning resource columns.

Python borrows read-only memory-mapped columns, including the UTF-8 name pool;
it does not rebuild the primary arrays or rerun the union/resource loops.
Borrowed columns retain their mapping and phase-owned scratch until the last
consumer releases them. That owner releases views and closes the mapping before
removing its directory, including exception cycles; no independent weak-reference
scratch finalizer may unlink a still-mapped file. The final cluster/assignment
schemas remain unchanged; final policy/semantic/index publication still needs
native ownership. The public reference constructor remains independently
available for differential validation; native production has no failure fallback.

## Phase 3 contracts

Production clustering consumes compact EmuIR resource/type/endpoint projections,
not synthesis attributes or complete cell dictionaries. Packed cluster ownership
and resource columns feed deterministic hMetis/Tcl exports and native providers.
Strict resource bounds include reservations before search; separate balance
repair is not a replacement for a valid initializer. Provider-neutral capacity,
fixed/group, SCC/cascade, reachability and hop checks remain authoritative.
Do not introduce early Static Exact readiness, frame feasibility or physical
segment deadline hard constraints into partition selection.

After selection, native analysis reads compact EmuIR and selected ownership.
Native semantic records retain canonical cut order, dependency levels, source
and capture segments, launch flags and integer predecessor coordinates. Stored
endpoint ordinals may have gaps; capture coordinates use dense positions in
logical EmuIR iteration order. Independent readers reconstruct that same order.

Python maps lifecycle-scoped native semantic/capture artifacts and independently
checks source/topology coverage. Native hashing reproduces canonical semantics;
native binary publication writes final sections directly into one staged file,
hashing them as emitted. A bounded manifest seals exact counts, source identity,
section extents and digests before fsync and atomic replacement. Do not restore
section spool copying, a complete Python semantic tree or duplicate full hashing.

`assignment.sqlite` owns instance/partition lookup, while `assignment.exact.bin`
owns ordered dependency/segment/capture payloads. Downstream consumers use the
bounded assignment view and typed cursors. Generic JSON materialization is an
explicit compatibility operation. References bind a complete generation;
unknown versions, truncation, corruption and mixed generations fail closed.

## Assignment index publication

`emuflow-native phase3 publish-assignment EMUIR PRIMARY REQUEST OUTPUT` publishes
current assignment v4 through the shared `SqliteArtifactWriter`. Its source-bound
request carries integer cluster ownership, the FPGA catalog, metadata and ordered
cut records. The C++ owner joins primary ownership with the paired EmuIR cell
cursor and writes primary copies directly; Python never expands or transfers
instance records for this operation. Both native and reference writers use the
same installed SQL schema. CMake, compiler-only fixture builds and deployment
builds generate the native header from those authoritative SQL files.

The producer checks source generations, counts, ownership, cut order and
predecessor coverage. The separate typed-index validator still checks the output,
canonical cut digest and exact contract. Only then does the host publish the
index/binary pair and its descriptor. Failed requests or validation preserve the
previous pair, including failures before the previous binary can be moved aside.
A selected native failure does not retry through the reference writer.

The phase report records `assignment_index_writer_implementation_id` and native
writer metrics. This closes the Python instance-row publication boundary; policy
conversion, host cut-record emission and other remaining publication work retain
their explicit migration obligations. It does not qualify complete Phase 3 costs
or complete native compilation without the required real-design measurements.

## Physical and timing contracts

Mapped indexes, placement certificates and `physical-handoff.sqlite` carry
site/BEL placement, macros and connectivity into RapidWright. Production does
not reconstruct `placement.json` or `packed-sites.json` trees. Preserve cell
accounting, site/BEL/PIP uniqueness, macro/cascade legality, fixed IOB coverage,
clock/SLR constraints and final per-device resource budgets.

The transport owner binds endpoint, lane, slot, direction, differential pins,
RTL and XDC semantics. Schedule frame length does not expand the BoardDB's
permitted ratio domain. Consumers join sealed relations rather than retain
independent full binding or endpoint maps.

Global OpenSTA remains the numerical authority. Ordered event/check and
measurement relations must preserve every original timing-path member exactly
once, complete TX/commit checks and disjoint local/cross-FPGA populations. TNS
counts each original path once, even when checks use representative bundles.
See [global timing semantics](global-opensta-qualification.md).

## Validation and completion

Qualification records separate nested stage wall time, parent/child CPU,
sampled process-tree RSS and available I/O counters. Enclosing and child step
times must not be added together. Keep actual experiment measurements outside
public design documentation.

Representation changes require independent record equivalence, deterministic
ordering and corruption/truncation/mixed-generation/publication-interruption
rejection. Preserve provider, policy, effective constraints, seeds and tool
versions. Physical-policy changes receive separate qualification.

Final acceptance uses the requested NVDLA design and the registered complete-flow
contract: exact provenance, per-phase/total runtime and resource evidence,
complete physical accounting, zero unrouted nets and DRC violations, final
resource budgets, original-path coverage and global OpenSTA WNS/TNS. Prepared
input continuations and component tests are intermediate evidence; they do not
establish complete Phase 1–7 qualification or completion of W0/W7.

### Chimew lookahead ownership

The maintained optional Chimew producer queries indexed Xilinx physical owners
for the selected backend. VTR atom-order reconstruction, full instance-position
dictionaries, copied placement/architecture/routing JSON sources and synthetic
package-pin generation are retired from that producer. Its source document
contains owner references only. Timing guards reside in the crossing input;
the Phase 5 schedule is not cloned. Native paper kernels remain unchanged.

Python still builds hop/group inputs and timing weights, and the shared core
and full native production data plane are not complete. VTR/open execution
entries, standalone artifact adapters and engine/build dependencies are retired.
This scope reduction does not establish full NVDLA acceptance.
