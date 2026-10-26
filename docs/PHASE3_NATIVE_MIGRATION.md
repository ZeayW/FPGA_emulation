# Native data-plane migration

## Architecture and ownership

The production target is one `libemuflow_core` and an `emuflow-native compile`
entry that owns execution of the complete flow. External Yosys, OpenSTA,
OpenROAD/TritonPart, OpenPARF, RapidWright/RWRoute and optional Vivado remain
separate engines. OpenPARF may retain its required thin Python/PyTorch host.
A native command that only invokes the Python compiler is not this target.

The current native executable links real Phase 3, 4, 5 and 6 kernels through a
shared typed operation API, including PATRON search. An opt-in native controller
now owns the ordinary one-shot phase sequence with native RTL frontend/TDM execution and
isolated shared Python adapters for remaining phases. Advanced orchestration and
remaining native data-plane ownership are unfinished. W0 supplies the shared
infrastructure contract; complete-stage ownership and native compilation are W7
delivery obligations. Existing and new
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
EmuIR v1/v2, assignment v3/v4 and split v2 have explicit versioned readers.
The initial topology hop legalizer also links into this core and uses the
shared stage/error/metrics/source-binding and atomic output contracts. Its old
standalone build, deployment and test entry are retired. The existing algorithm
and independent final hop audit remain; summary output retains counters without
a Python move trace. Current primary ownership also supports the direct source-to-kernel hop model described below; complete stage ownership remains W7 work.

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
complete all remaining physical adapters or W7 data-plane migration.

`phase7 export-vivado MAPPED REQUEST RTL FUNCTIONS SUMMARY` now owns the
primitive RTL and source-function signature producers in the same shared core.
An ordered mapped cell/pin merge preserves source ordinals, bit order and
parameter identity, without per-cell Python reconstruction or FFI. The bounded
host publishes closed derived outputs with rollback and retains the normal
source/resource audits. The prior Python writer/signature oracle is test-only.
This closes that data-plane boundary; source query builders, the RapidWright
routed timing graph and other adapters still require native ownership, and
complete real-design qualification remains required for W7.

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
plus process success. Infrastructure acceptance is separate from the remaining
adapter integration and W7's native data plane; a common interface is not
whole-flow qualification.

`phase3 initialize-tritonpart EMUIR PRIMARY REQUEST OUTPUT_DIR` owns the complete
production initializer, including typed net-weight input, integer hypergraph and
resource bounds, streamed OpenROAD inputs, supervised engine execution, seed
selection and minimum-occupancy repair. Optional weight stores and imported
solutions are explicit source references. Strict resource/balance validation runs
before repair; neither initialization nor repair relaxes the constraints.
An independent source-cell reduction checks each accepted candidate, and a
separate source-connectivity traversal audits exported hyperedges and resources.
Imported solutions do not generate unused solver inputs; unweighted inputs exist
only for an explicitly enabled baseline.

The small summary and existing `patron-initial-columns-v1` ownership file publish
as one new directory. The host borrows the read-only column and passes the same
original artifact to PATRON; it does not load net weights, reconstruct hyperedges,
parse owner rows or re-emit the initial ownership. Original source generations
remain bound across both operations. Algorithm policy and objective ordering are
unchanged; the implementation identity distinguishes native execution from the
explicit diagnostic reference. Complete Phase 3 configuration, selection and
stage ownership still require migration and real-design qualification.

`phase3 refine-patron-from-primary EMUIR TIMING PRIMARY INITIAL REQUEST OUTPUT`
builds the PATRON v14 model and invokes the existing solver in one shared-core
process. Normal compact native clustering selects this direct owner. The shared
`PrimaryClusterView` reads source-scoped ownership and resource columns without
copying them into another membership/resource artifact or transposed C++ matrix.
The Python adapter carries board-sized configuration, fixed constraints and an
initial ownership reference. It does not create pressure-net/path/name populations,
derive a second model or stream per-record solver input. The snapshot command,
cluster-column protocol and v1 board request are retired. Missing primary
ownership fails without generating a replacement snapshot. Selected ownership
feeds `phase3 finalize-assignment-semantics`: the native owner projects cut sinks,
builds analyzer inputs/captures/final nodes and independently checks source
projection, capture coverage and topology recurrences. Only bounded policy and
source references enter; no instance assignment, cut list or semantic tree returns
through Python. The instance-assignment identity is computed from ordered source
records inside the same operation. Its four published files are nodes, captures,
endpoint counts and a bounded summary. Their shared directory retires after its
last mapped borrower; the Phase 3 command explicitly closes them after publication.

The shared model is move-only, and both its typed pressure-model reader and direct
store producer call the same search implementation. The producer checks primary
cell/membership coverage, pin identities, sparse endpoint ordinals, ordered timing
paths/net references, fixed ownership, resource/balance limits and bank symmetry.
Capacity and route policies retain their existing meanings; no schedule-readiness
or Static Exact feasibility guard is introduced. Input CRCs and retained file
generations reject truncation, corruption and mixed sources before solving.
The output keeps the existing typed assignment format. The search retains its
lexical model coordinates; publication emits canonical cluster ordinals, including
identities longer than six digits. A sealed identity-order sidecar binds those
coordinates. The result parser borrows the read-only column without reordering
or rebuilding the solver ownership.
Only counts and implementation identity return in the bounded
[model summary](../schemas/patron-native-model-v1.schema.json).
The summary also separates model-build and search operations using the common
wall/CPU/I/O metrics. Their RSS values are process-lifetime high-water snapshots,
not incremental stage memory; the enclosing sampled process-tree peak remains
the whole-phase memory measurement.
Normal handoff still runs the independent assignment validator and candidate
selection before final semantic/index publication.

For original native initializer/PATRON owners, `phase3 audit-decision` now owns
that decision boundary. It borrows both original ownership files and the PATRON
coordinate file, validates both candidates, and selects using the existing v14
path-hop/ratio/objective ordering. The candidate comparison never scans Python
ownership columns or accepts a separately reconstructed host objective. Rank
quantization preserves Python ties-to-even and integer values beyond int64.
One source-cell cursor independently reduces resources for both assignments;
primary resource totals, reserved capacity, strict balance, active/minimum device
counts and fixed/group source identities must agree before selection. This does
not introduce a schedule-feasibility gate. A reused bounded audit remains bound
to its original ownership, source generations and effective policy.

Owned resource queries consume those same original artifacts through the v2
request instead of copying a cluster-part column back through Python. Assignment
construction reuses their bounded counts and exposes any part-coordinate
translation as a borrowed read-only view, without another cluster-sized array.
Explicit reference owners retain the existing record request. Complete Phase 3 option assembly/controller ownership and real NVDLA stage qualification remain unfinished.

Compact production constraints now normalize through
`phase3 normalize-configuration` in the shared core. The original constraint
file is a generation-bound input reference; the host does not read or reconstruct
its JSON. Only board-sized capacities/topology and scalar overrides enter the
small request. Explicit API dictionaries are streamed into a private input file.
The native producer expands case-sensitive Unicode instance patterns from an
ordered source cursor, resolves explicit identities by indexed lookup and scans
source resources only for minimum-capacity selection. It retains no complete
source identity catalog. Homogeneous greedy selection and heterogeneous subset
ranking preserve required fixed targets, directed reachability, endpoint boundary
headroom, direct capacity/latency and platform-order ties; inactive devices
remain legal transit nodes. Input order, normalized logical fields, dimension
overrides and resource reservations keep their existing meanings.
The request explicitly records the established character-range convention;
changing the configuration host cannot silently change pattern expansion.

The normalized result remains bounded by the existing publication metadata
contract. Raw input size is separate: a large redundant input can still produce
small effective constraints. Native code checks original generations and closes
the result before the host reads that bounded configuration. Shared resource
decoding preserves wide integer amounts and raw field names, including escaped
NUL, without JSON scalar coercion. Phase 3 option assembly and complete native stage/controller ownership are still unfinished; this
producer does not establish real NVDLA runtime/RSS gains or W7 completion.

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
same independent checker with a bounded JSON proof. The former `resolve-captures`
command, persistent seq index and its schema registry entry are retired. Tests
compare the current owner with an independent source-coordinate oracle; there
is no second production capture publisher or Python fallback.
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
and explicit typed input copies keep the reference model path. A selected native producer never retries through that path. Its
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
complete the other physical exporters or full W7 qualification.

The OpenPARF bridge has one required typed output and consumes typed placement
certificates. Its validator requires a record sink, instead of optionally returning
a complete physical-cluster tree. The physical publisher binds source generations,
rejects output/input aliases and rechecks sources at atomic publication. Separate
packing/placement JSON output is retired; bounded test snapshots use the same
producer. This does not make the remaining Python bridge data plane native.

RWRoute's production API takes the mapped SQLite index and one typed physical
handoff directly. JSON/object export is retired; the noninstalled
`tests/rwroute_reference.py` remains an explicit differential oracle. Native request
v2 derives top/part from the stores instead of Python metadata views.

`phase7 export-rwroute MAPPED PHYSICAL REQUEST TSV SUMMARY` now owns the complete
production TSV export from mapped index v2 and the typed physical handoff in the
shared core. Its small request binds source generations and carries board I/O
and resource configuration only. Placed cells, RAM properties, LUT6_2 route
ordinals, closed clock-gate qualification and net/pin rows stay native. The
Python cursor oracle is noninstalled and reserved for explicit independent
differentials; it is not a production input path or failed-native fallback. See the authoritative
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
standalone-only installation is rejected for this boundary. Production supports
only the Static Exact dependency provider. Small scheduling oracles live in tests;
retired providers and intermediate result import are not production fallbacks.
Requests retain the source generations captured by their producer; stale references
are rejected before computation and rechecked before atomic publication. Python
still commits the compact manifest and report. Reports identify the store and
producer implementations separately from the schedule algorithm. Storage wall time
includes native calculation and is labelled with that measurement scope.

`phase5 validate-schedule ROUTE_STORE BOARD_REQUEST SCHEDULE_STORE REQUEST SUMMARY`
is the independent shared-core validator used before report commit. It reads the
same versioned board grammar as the producer, but never calls its model builder,
solver, solution or certificate builder. It reconstructs the dependency order,
source and relay readiness, affine link latency, fixed wire/ratio groups, bank
direction, sink/commit deadlines, completions and phase metrics from source routes
and stored events. Ordered typed cursors check route/child coverage and canonical
section, compact and pretty logical seals without per-route SQL queries or a
second validation store. Only bounded header/configuration and the final summary
cross Python. Original route and schedule generations remain bound through atomic
summary publication. A native rejection never retries a Python validator.

The Python packed-DAG indexed validator and its ordered-record replay helper are
retired. Indexed validation requires the current typed RouteStore; dictionary
schedule fixtures can still use the small independent correctness oracle. Fixture
comparisons cover full fields and hashes, not just counts or a solver success flag.
Native domain summaries use bounded counts. The shared timing projector now
reconstructs pre-physical timing and cross-layer binding directly from staged typed
owners. It emits a v3 descriptor with the complete logical path projection seal,
not another member/cut tree. The Python path materializer remains an explicit
reference/stream-consumer API rather than the current Phase 5 summary path. Opening
the freshly sealed producer-owned store does not repeat its full file hash. Compact publication and validation apply without a
managed-storage flag. TSV/transport JSON/testbench/feedback exports and reference
simulation are not implicit native-production outputs.

Normal Phase 5 publication and validation share one native timing/binding pass.
Native timing statistics retain the reference arithmetic and sorted accumulation
order; numeric TEMP sorting uses source-scoped IDs instead of duplicate path names.
See the [indexed timing contract](FLOW_PLAN.md#phase-5-indexed-timing). This closes
the current Phase 5 summary and cross-layer publication data plane, alongside the
independent indexed schedule validator. Python still controls report/generation
publication; native orchestration integration, other phase data planes and real
design qualification remain required. This does not complete W7 or establish
whole-stage performance gains.
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
does not call the Python compiler. TDM consumes the current route selector, assignment index and normalized BoardDB
directly in C++; it has no Python phase adapter. The ordinary RTL frontend also
executes directly in C++, using a checked configuration prepared by preflight.
Each remaining transitional
Python adapter invokes one shared phase body and exits, releasing its working population before the next
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
planes and several direct engine adapters remain. Retained Chimew comparison
runs through native control with either Xilinx backend. Its source-bound device
catalog is carried as bounded control data; baseline physical/runtime reports
are serialized once within the comparison. Frame search and serial BSP remain
unsupported by native control. Component coverage does not establish real-design
physical/global timing qualification.
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
| Phase 1–2 | Complete native ordinary RTL frontend under native control, including engine/import/clock/resource/report publication; native OpenSTA input export, indexed path-result import, timing-weight generation and independent validators | Migrate supplied/prepared/other indexed-source adapters and complete timing-stage lifecycle; qualify the whole current-design frontend |
| Phase 3 | Native source-backed constraint normalization, candidate eligibility/primary clustering, complete TritonPart initialization/native v14 search; original-owner decision and independent audit; selected semantics and final-directory publication | Complete option assembly and stage lifecycle/controller integration; qualify complete stage costs |
| Phase 4 | Native routing; typed RouteStore/results | Remove remaining graph-level adapter work and integrate complete native stage lifecycle |
| Phase 5 | Complete shared-core stage owns model/solver/certificate/store, independent validation, timing projection, report and generation commit | Native compiler configuration/execution are native; optional Python API configuration remains bounded. Complete real-design qualification |
| Phase 6 | Indexed split/transport/binding owner; streamed RTL/XDC | Complete native producers and adapter queries while preserving electrical contracts |
| Phase 7 | Indexed lowering; typed physical handoff and native Bookshelf pin export | Complete remaining backend/export/validation ownership and full physical qualification |
| Global STA | Native typed physical-to-event binding, event validation/model export, measurement import, full-original-path reduction and typed QoR publication | Migrate remaining physical producers and orchestration; qualify complete real-design physical/global timing |

Native computation does not eliminate algorithmic search cost. Compare search,
imported-solution and frozen-output replay as separate execution modes. Porting
an adapter must not change the algorithm budget or weaken independent legality.

## Source-owned hard-hop generation and audit

`phase3 refine-hops-from-primary EMUIR PRIMARY REQUEST OUTPUT_DIR [OWNER [ORDER]] [WEIGHTS_SELECTOR WEIGHTS_STORE]`
accepts original TritonPart or PATRON ownership and board-sized policy. It builds
the existing directed net/resource model in C++, calls the unchanged FM/exact
fallback kernel, and publishes current ownership columns plus a bounded summary.
Python does not load timing weights, construct a net population, serialize a
cluster/net text model or parse per-cluster output. A zero-move result retains
the original caller ownership without rebuilding assignment semantics.

Independent ordered source endpoint and resource reductions check the original
and selected assignments, fixed ownership, active/minimum device use, capacity,
balance, reachability and hard hops. Violation examples retain canonical net and
FPGA order with a bounded 64-record heap. Inactive devices remain valid topology
relays. `audit-hops-from-primary` applies those checks to the selected owner
without another search model or optimization pass. Normal Phase 3 final PATRON
and single-target audits use this owner when original artifacts are available.
Generation/CRC/weight seals reject corruption before a complete new directory
publishes. Failure never retries through the numeric reference path.

This closes source-dependent hop construction, repair handoff and independent
audit for current native owners. Whole Phase 3 request assembly, stage execution
and direct compiler integration remain unfinished, as does complete NVDLA
runtime/RSS and physical/global timing qualification.

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
scratch finalizer may unlink a still-mapped file. Final catalog publication now
reads those columns in the shared core and writes the existing compressed SQLite
sections directly. It also consumes the original candidate artifact, checking its
bitmap/name/source pairing without decoding the name population in Python.
The common SQL publisher adopts the closed generation; the selector follows its
sealed payload descriptor rather than a fixed filename. Source replacement,
corrupt member/name relations and failed selector commits reject publication.
The final logical cluster/assignment schemas remain unchanged. Assignment packing
uses ordinal IDs/member counts without replaying cluster records. Remaining
policy/semantic/index orchestration still needs native ownership; this boundary
alone is not complete Phase 3 migration. The public reference constructor remains
available for independent differential validation; native failure has no fallback.

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

Ordinary native production uses `phase3 publish-partition`: one source-bound
transaction owns the exact binary, assignment index and selector, cluster catalog
and selector, normalized constraints and final report. It composes the existing
shared-core producers, independently validates physical sections/cut relations
and source/cluster coverage, closes output connections, then publishes the entire
new directory with one rename. The request carries bounded headers and original
artifact references; no graph is reconstructed in Python between these steps.
Existing final directories and mixed report/source identities are rejected.
The report records `native-partition-publication-v1`. Runtime measurements retain
their enclosing phase scope. This closes final publication, not the remaining
Python configuration, candidate selection/hop auditing or complete Phase 3 owner.

`phase3 publish-exact-binary` owns section generation, bounded manifest/descriptor
assembly, preamble sealing and independent validation. It appends/seals its
invocation-owned private file in place, avoiding another section-file copy, and
publishes the checked file and descriptor together. `validate-exact-binary`
independently reads the complete file: it checks preamble/manifest/source
bindings, section extents/digests, ordered net identities, capture/segment/source
coverage, binding and metrics. Nested populations are proven before allocating
integer coverage buffers. Identities stream one record at a time; only integer
state scales with segment/capture counts. The superseded Python graph validator
and its nested-source scan are test oracles only. Existing explicit v1/v2 read
semantics and the 8192-bit v2 capture bound are preserved.

The following component operations remain shared library/explicit fixture entry
points; ordinary production does not orchestrate them through separate Python
publishers.

`emuflow-native phase3 publish-assignment EMUIR PRIMARY REQUEST [NATIVE_NODES [COUNTS [OWNER [ORDER]]]] OUTPUT`
publishes current assignment v4 through the shared `SqliteArtifactWriter`.
The C++ owner joins primary ownership with the paired EmuIR cell cursor and writes
primary copies directly; Python never expands or transfers instance records for
this operation. Production v3 requests bind original native ownership, coordinate,
semantic-node and count files. They carry only FPGA catalogs, dimensions, source
generations and bounded metadata; cluster and endpoint-count columns are not
re-emitted through Python. The C++ reader consumes ordered
cut identities, ownership and predecessors directly from that node artifact,
using the shared bounded semantic-record decoder. It does not receive another
host-generated cut/predecessor record protocol. The compact cut owner lends its
existing count file. Explicit reference producers without native selected-owner
artifacts retain the v2 packed-column protocol; a missing or changed selected
native artifact cannot select that reference protocol as recovery.

`phase3 publish-assignment-descriptor EMUIR PRIMARY INDEX REQUEST OUTPUT` writes
the existing packed JSON selector from the final authoritative assignment index.
One ordered source/copy merge checks complete source identity and unsplit primary
clusters. Partition counts and cut/semantic seals must match the bounded header.
Index-derived part columns stream through zlib/base64 to the common atomic writer;
Python neither iterates cluster ownership nor holds the compressed payload.
The selector remains byte-compatible with the reference exporter.

The explicit v1 request remains for current nonsemantic and Python-reference
semantic producers. Missing or changed native ownership fails; it never selects
v1 as recovery. Both native and reference writers use the same installed SQL
schema. CMake, compiler-only fixture builds and deployment builds generate the
native header from those authoritative SQL files.

The producer checks source generations, counts, ownership, cut order and
predecessor coverage. Original node and capture generations remain bound through
independent validation and final index/binary pair publication, including ctime
changes. The separate typed-index validator still checks the output,
canonical cut digest and exact contract. For assignment v4,
`phase3 validate-cuts ASSIGNMENT SUMMARY` invokes `AssignmentView::ValidateCuts()`
through the shared stage API. It reads the published SQL relation independently
of the producer, merges cuts with the sink table's native net/sink order and the
predecessor table's index/ordinal order, and streams canonical fields into the
logical hash. It retains no per-cut dictionary, child list or global name map.
A separate indexed join verifies predecessor-name existence; the merge checks
parent coordinates, ownership, scalar types, completeness, order and orphans.
It does not sort the complete sink relation through a parent join. Only a bounded
count/digest summary returns to Python, which retains the design/instance and
exact-binary validation gates. The superseded Python digest-only replay is
removed; record iteration remains for consumers that actually need cut records.
Selected native validation failures never retry that iterator. Only then does
the host publish the index/binary pair and its descriptor. Failed requests or validation preserve the
previous pair, including failures before the previous binary can be moved aside.
A selected native failure does not retry through the reference writer.

The phase report records `assignment_index_writer_implementation_id` and native
writer metrics, including native/host cut counts and request bytes. This closes
the Python instance-row and final native-cut request boundaries; policy
conversion, initial cut construction, reference inputs and other remaining
publication work retain their explicit migration obligations. It does not qualify
complete Phase 3 costs or complete native compilation without the required real-design measurements.

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
Invocation-owned event facts and metadata now share one registered primary;
the derived index stores ordering and representative spans without arc copies.
`global-sta index-events` independently validates and compresses complete bundles;
`global-sta export-events` streams the OpenSTA model through common source-bound
publication. Production no longer retains whole check/identity arrays in Python.
Representative engine measurements now have one native scalar owner. Native
import/reduction restores every original identity, preserves complete TX/commit
checks and computes original-path WNS/TNS with faithful partial sums. Native
QoR publication writes the existing typed final columns directly; the reader
validates complete ordered records through the shared core. Saved-result
consumers join the same event namespace rather than load expanded TSV or rebuild
Python scalar/path arrays. Current typed physical facts now enter
`global-sta bind-events` directly. The
binder reuses the Phase 5 scheduled-path visitor, merges physical segment cursors
and queries FPGA-owned endpoint delays without rebuilding Python path/hop or
endpoint populations. Its final event primary is still independently indexed
and validated. Mixed typed/inline owners fail rather than fall back. Current boundary and logic-segment
identity/timing and local-path timing readers now delegate
complete record/schema/metadata/logical-seal validation to the shared core.
Their Python full-store validators are retired. Maximum-delay summaries reuse
validated facts instead of triggering another whole-table query. Current Vivado boundary/logic/local measurement import producers also write
and seal the final typed records in the shared core. Their Python measurement
spools and row reconstruction are retired; the legacy identity importer remains
only as an independent small fixture oracle in tests; local query identity is shared with
the existing typed owner rather than stored as private JSON rows. Source query
construction and other phase adapters remain. Routed-endpoint production now
joins typed mapped pins to physical assignments in the shared core, reading only
the net/end-point route stream after independent route qualification. Net-local
relations and a disk-backed seen-bit index replace Python sites, endpoints and
whole-route ID sets. The existing endpoint JSONL and timing manifest are emitted
and hashed directly; no second representation or schema is introduced. Their
source-bound grouped publication restores previous outputs on a late failure.
Route validation seals generations of the manifest, net and PIP payloads;
consumers reuse that proof without rescanning PIPs. Physical views likewise keep
their original generation. Metadata arrays are borrowed sequential views over
the immutable manifest, and graph/OpenSTA consumers keep the produced generation
instead of rebinding a changed file. The old Python producer is test-only.
RapidWright segment measurement now consumes the mapped index and routed endpoint
stream directly in the shared core. One integer CSR graph serves the boundary,
logic-segment and local-path owners; the graph has no reverse-edge copy. Queries
are grouped by start and reuse reachable-cone work arrays rather than initialize
whole-graph dictionaries for each original path. The existing launch offsets,
sequential barriers, primitive delay bounds, fixed-I/O terms, source-declared
floating inputs, reachability/cycle rejection and deterministic tie ordering are
preserved. Output records are sealed directly in the existing typed owners.
The old Python graph/builders are independent fixture oracles under `tests/`,
with no production fallback. Graph counters and the undriven-bit count are
bounded summaries; diagnostic copies of the undriven population are retired.
Routed OpenSTA staging now writes the final compact EmuIR and large Liberty
payload in the shared core, directly from mapped pins and the endpoint stream.
It keeps endpoint matching/type numbering in temporary indexed relations and
retains one small type-to-interned-payload index in the final graph owner. Python
receives a bounded base model and artifact references; it does not rebuild an
endpoint or generated-type population. Coverage and model queries retain that
owner's generation. The common compact SQL schema is installed once and embedded
by the native header generator. Independent final-record validation preserves
endpoint coverage and A/Y/type binding before the grouped graph/library commit.

The old full graph builder is test-only. Indexed timing Verilog and net-map export
now use one shared-core stage over compact EmuIR, including source-bound negative
clock specialization, instance-local pin merging, constants and escaped-name
ambiguity rejection. The Python indexed emitter is retired; standalone text
exporters select only the requested output. Pre- and post-route OpenSTA use the
same producer and grouped publication. Independent route qualification and
remaining phase orchestration still require migration.
These remaining boundaries, the complete native entry/lifecycle and current-source
whole-design qualification keep W6/W7 incomplete.
See [global timing semantics](global-opensta-qualification.md).

`physical import-sta-paths EMUIR TSV REQUEST STORE SUMMARY` parses OpenSTA path
records into the current indexed TimingPathDB in the shared core. The source is
compact EmuIR v2; the output retains the existing path-store schema. Uniqueness
and net membership use SQLite keys and ordered joins. Reached endpoint identities
use source queries and a bounded cache rather than a graph-wide object map; top
port scalar lookup does not expand every port bit per query. The importer does
not retain path IDs, all reached nets or a negative-slack list in Python.
Normalization, WNS and original-path TNS retain their existing definitions, with
the shared exact summation used by global timing. TNS counts each original
TimingPathDB record once, rather than deduplicating register endpoints.

Before publication, a separate final-row audit rebinds endpoint certificates to
the source, merges ordered memberships, checks uniqueness, numerical domains,
normalization and coverage, and computes its own reductions. It receives no
parser endpoint cache or counters. `physical validate-sta-paths` exposes this
audit for a published owner. The normal immediate consumer instead reuses the
already completed audit through an invocation-scoped proof bound to the original
EmuIR, selector, payload generations and exact manifest; it does not reopen or
hash the population a second time. The shared publisher seals the store once,
then commits the bounded selector. Rejection preserves the previous publication.
The former Python streaming producer is test-only; native failure never selects
it. This closes path-result import and validation, not complete Phase 1–2 or W7.

Timing-weight production and validation now use the same core. The operation
`physical build-sta-weights EMUIR PATH_SELECTOR PATH_STORE REQUEST STORE SUMMARY`
binds original input generations and reduces path memberships directly into the
current indexed net-weight owner. Maximum clamped path criticality, membership
counts, the power-law weight, and summary extrema preserve the existing policy.
Multiplication and addition round separately, preserving the reference arithmetic
instead of allowing FMA contraction to change fractional-exponent records.

A separate native auditor visits memberships in net order, independently reduces
one live net, and merges it with final stored weights. It checks exact coverage,
counts, scalar domains, policy parameters, source identity, summary and payload
counts without a Python path or weight dictionary. The producer runs this audit
before publication. `physical validate-sta-weights` exposes an explicit standalone
audit. The host performs shared source-bound publication and returns a bounded
summary. Ordinary compiled timing passes the producer's original generation-bound
validation proof to the weight consumer. A changed owner or manifest rejects
instead of rebinding the file or repeating validation. The former unqualified
`input_already_validated` boolean is retired for this producer; explicit standalone
input validation retains the strong source/payload audit.

OpenSTA API/CLI/compile extraction now share one current indexed result route and
require compact EmuIR v2 before engine launch. The full inline path producer and
unused inline weight producer are retired; the independent former path/weight
producers remain only under `tests/`. Directed through-net diagnostics query the
selected membership relation instead of rebuilding the entire path-net population.
This closes the normal path-result and timing-weight data planes. Yosys import,
remaining timing configuration/engine orchestration and complete real-design
qualification still keep Phase 1–2 and W7 open.

### Streamed Yosys hierarchy ownership

`phase1 stage-yosys-hierarchy REQUEST STORE SUMMARY` consumes the Yosys document
on stdin through the shared native core. It stages bounded module records in
SQLite, expands repeated hierarchy in deterministic name order, preserves
blackbox/whitebox leaves and open ports, and resolves aliased ports and late child
constant outputs against the final signal equivalence relation. Only top-port
records and bounded summary/configuration cross the Python host boundary; there
are no Python cell callbacks, local-bit dictionaries or complete alias maps.

The current importer continues in this same private staging database after
same-filesystem adoption. It does not copy the cell/name relations into another
owner. Source module tables and alias scratch are removed before this handoff.
The byte pump uses bounded chunks and kernel backpressure; no complete Yosys JSON
file is introduced. Native writer scratch is inside the host's cancellation-owned
root, so a failed producer reaps the consumer before retiring partial writes.

The shared external-tool request v2 explicitly forwards stdin for stream
consumers. Existing v1 requests still supply closed input via `/dev/null`; there
is no inferred input mode or retry through a different launcher. The result
protocol and bounded diagnostic tail remain unchanged.

The parser bounds individual captured records to 32 MiB and JSON/hierarchy depth
to 512; it does not bound the total design population. Invalid/truncated records,
duplicate defined identities, recursive modules and incompatible port/constant
bindings reject before a completed staging owner is returned. The former Python
hierarchy producer is test-only and supplies an independent differential oracle.
With a bound primitive library, request v2 runs Xilinx normalization inside this
same private native owner. The kernel preserves CARRY4 pairing and single-half
lowering, CI/CYINIT handling, carry shared-exit XOR reconstruction, physical
DI/S LUT6_2 sources, INV lowering, negative-edge FF aliases and MUXF7 constant
leaves. Helper identity collisions reject instead of overwriting source cells.
Library policy/resources and original generation are checked; a separate final
primitive census validates types/directions and reduces resource totals from the
finished rows. File and logical summaries keep the current schemas and policies.

Consumer exposure is reduced to counts per carry-output signal, with a compact
CI[0] exception index for cascade exclusion. It does not store every consumer's
repeated identity. Negative-edge FF metadata updates its existing rows directly,
without creating a second full FF relation. Carry and helper rewrites use
SQLite-owned records rather than Python name/consumer/replacement dictionaries.
The production hierarchical frontend never retries the Python normalizer after
native failure. The existing Python normalizer still serves explicit flat and
mapped-index consumers; those consumers must migrate before that implementation
can retire. Independent differential tests use it as an oracle.

`phase1 import-yosys REQUEST EMUIR SUMMARY [PRIMITIVE_LIBRARY]` now runs the
complete hierarchical source-to-compact producer in the shared core. Source
module/cell/name records stay in SQLite TEMP storage under the same private
connection. The main database contains only the canonical compact owner, so
retiring raw staging does not leave its pages in the published file. Cell
payload interning is bounded; pin owners are integer IDs, and endpoint ordering
uses source cell/port/bit order without a Python fanout list. Canonical names,
aliases, constant connections, resource vectors, wide integer parameters and
floating payloads retain their current logical definitions.

Before commit, a separate source cursor merges all original endpoints with the
finished owner, checks cell payloads and aliases, and independently recomputes
net classification. Only the final counts, normalization summary and native
metrics return to the host. The host requires the producer's original output
generations, adopts the closed file by same-filesystem rename, and invokes the
shared publisher once. A changed private store or summary rejects without
rebinding validation to the modified file. An explicit finalizer remains private
and labels the original validation as preceding that callback; it cannot mutate
a prior publication. The ordinary hierarchical path has no Python row-writer or
custom Python transform fallback. Stage-only output remains a component
qualification interface, not the production handoff.

This closes hierarchical source-to-compact generation, not the complete frontend:
remaining flat/mapped-index consumers, complete timing-stage ownership and
real-design qualification remain open. The native-controlled RTL stage below
owns its clock analysis/reporting and phase lifecycle.

### Complete native RTL frontend

`core::RunFrontend` and `phase1 run REQUEST [EXPECTED_OUTPUT]` own the maintained
Xilinx RTL frontend as one stage. Preflight produces a bounded, CRC-checked
request containing the existing Yosys script/argv, normalized board configuration,
clock policy, primitive library and original source generations. It constructs no
circuit records. Native control verifies that original request binding, starts
this stage directly and rejects an output outside its claimed flow directory.

The shared `RunExternalToolStream` forks the engine before starting its in-process
consumer thread. A bounded pipe feeds the existing hierarchy/normalization/compact
producer directly; the supervisor continues draining bounded stderr and servicing
deadlines/cancellation. Consumer failure cancels and reaps the engine group.
Detached stdout writers reject without blocking cleanup. A syntactically complete
stream is insufficient: the engine must exit successfully before publication.

The stage uses ordered native resource/clock queries, including all clock sinks
and fabric drivers, and retains the existing capacity and fabric-clock policy.
Detailed clock evidence is streamed into the derived phase report; it never enters
Python or subsequent control state. The existing source-to-compact independent
endpoint/net-classification audit remains required. After readers close, the
shared publisher synchronizes the complete private stage and exposes its new
absent destination through one directory rename. Failed engines, malformed data,
changed sources, strict clock/capacity failures and existing destinations leave
no partially published Phase 1. The stage does not replace a previous generation.

The controller carries a generation-bound `frontend-summary.json` reference and
canonical EmuIR/board paths; the following adapter reads only that bounded summary.
It retires the small configuration after successful state validation. Result v2
records `native-rtl-frontend-v1` for this stage and retains
`native_data_plane_complete:false`. Supplied JSON/prepared inputs remain explicit
adapter modes; they are not automatic retries after a native failure. The default
Python controller remains a separate transitional consumer of the shared importer.

The physical logic-segment reader accepts both supported assignment v3/v4
contracts through their sealed binary cursors. Current v4 is never classified as
legacy or reconstructed through the compatibility reader. Unknown versions still
reject at the shared assignment boundary.

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
establish complete Phase 1–7 qualification or completion of W7. W0 infrastructure
acceptance has its own scoped equivalence and integration-overhead gates; it does
not require all remaining phase data planes to have native implementations.

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
