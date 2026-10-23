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

See [phase contracts](FLOW_PLAN.md) for the whole-flow architecture,
[algorithm implementations](ALGORITHMS.md) for providers, and
[physical acceptance](XILINX_PHYSICAL_FLOW.md) for backend requirements.

## Shared core (W0)

The core owns representation-scoped typed IDs, checked artifact I/O, common
CRC-32/SHA-256, SQLite statement/transaction lifetimes, and atomic publication.
The linked operations use a versioned schema registry, `ArtifactRef`,
`StageRequest` and `StageResult`. Complete consolidation still requires shared
store/view adapters for the remaining public phase artifacts and external
engines. An integer identity is meaningful only with its owning artifact;
equal integers in different stores are not interchangeable.
These operation implementations and their supporting contracts are compiled
into one `libemuflow_core` target. The former `emuflow_native_stages` target is
only a CMake compatibility alias; the executable and native library tests link
the core directly. This does not yet cover every standalone adapter or complete
the native production data plane.

`phase3 refine-patron-from-store EMUIR TIMING CLUSTERS REQUEST OUTPUT` builds
the PATRON v14 model and invokes the existing solver in one shared-core process.
Compact indexed production with canonical packed clusters selects this producer.
The Python adapter carries board-sized configuration, source generations and bulk
primary cluster columns; it does not create pressure-net/path/name populations,
derive a second dimension/model representation or stream per-record solver input.
The original packed clustering and initial ownership are still Python-owned;
this migration does not claim complete native Phase 3 construction/publication.

The shared model is move-only, and both its typed compatibility reader and direct
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

Small/inline and explicitly selected legacy tools, retained diagnostic traces,
physical-feedback algorithms and explicit legacy input copies keep the reference
model path. A selected native producer never retries through that path. Its
`native_input_transport` is `native-store-model-v1`; input bytes count the bulk
cluster-column snapshot plus bounded request, and input records count those two
private artifacts rather than pressure vertices/hops. They are phase-owned scratch,
not checkpoints. Full NVDLA runtime/RSS/disk and terminal Phase 7/7C qualification
remain required before declaring the migration complete.

`phase7 export-bookshelf MAPPED ATOMS NETS SUMMARY SPOOL` is also a shared
`StageRequest`/`ArtifactRef` operation. The production adapter prefers the unified
entry, and the standalone compatibility CLI links the same core. The core owns
typed SQLite reads, immutable input paths, ordered atom/pin joins, endpoint
spooling, degree/driver checks, atomic text publication and scratch lifetime.
Two ordered cursors preserve numeric net order and stable driver-first endpoint
order without a per-net fanout vector. Counts and primitive pin signatures are
the only summary returned to Python. Existing mapped v2 and private lexical
atom relations retain their zero application/user PRAGMAs; their stage-owned
metadata and ordered-record checks remain required. This integration does not
complete the other physical exporters or full W0/W7 qualification.

`phase5 build-schedule ROUTE_STORE BOARD_REQUEST GENERATION_REQUEST OUTPUT`
owns the complete large indexed exact producer. It builds the integer model
from v3 RouteStore, computes a typed in-process solution, derives the dependency
and sink-readiness certificates and domain/phase metrics, then publishes the
existing v4 ScheduleStore through the shared writer. Python sends only bounded
board configuration, static header fields and domain templates. It never imports
the complete native result or builds route/predecessor arrays, completion offsets,
hop-sequence arrays or certificate populations for this producer. No numeric
input/result files are written or reread on this path.

The common model and solver preserve tree traversal/order, predecessor ranks,
capacity budgets, fixed lane/ratio groups and affine-delay cycle rounding.
`ComputeStaticExactSchedule` returns typed records directly to its C++ caller;
the standalone binary-input/output command is a compatibility wrapper around
the same computation. The legacy `schedule-from-store` and `publish-schedule`
operations remain available for explicit API/comparison uses. Their publisher
shares the final record writer with the complete producer.

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
native scheduler. Explicit standalone comparison tools, older storage versions,
dictionary compatibility inputs and small reference schedules retain the
reference path. A native failure never retries that path. Requests retain the
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
local sequence matches. This does not yet migrate legacy v1 readers, capture
identity joins, mapped physical stores or the other remaining phase views.

### Native operation API

`emuflow_core/native_pipeline.hpp` defines the typed in-process operation
interface; CLI parsing is a separate adapter. The library executes the native
implementation directly, with no Python invocation or graph-sized FFI transfer.
PATRON's default production resolver uses this interface; an explicit standalone
tool remains an independent comparison choice. A present unified binary that
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
retains advanced modes. Current defaults resolve from its unchanged API signature;
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
| Phase 3 | Shared-core primary clustering; native v14 store-based pressure construction/search and semantic/capture production; native exact-binary writer | Migrate candidate eligibility and remaining assignment publication/reference modes; qualify complete stage costs |
| Phase 4 | Native routing; typed RouteStore/results | Remove remaining graph-level adapter work and integrate complete native stage lifecycle |
| Phase 5 | One native exact model/solver/certificate/store producer; independent staged cursor validation | Native independent validation, timing adapters and non-exact interfaces |
| Phase 6 | Indexed split/transport/binding owner; streamed RTL/XDC | Complete native producers and adapter queries while preserving electrical contracts |
| Phase 7 | Indexed lowering; typed physical handoff and native Bookshelf pin export | Complete remaining backend/export/validation ownership and full physical qualification |
| Global STA | Typed segments/results and bounded identity/stitching views | Native check/input construction, result import and ordered complete-path stitching |

Native computation does not eliminate algorithmic search cost. Compare search,
imported-solution and frozen-output replay as separate execution modes. Porting
an adapter must not change the algorithm budget or weaken independent legality.

## Primary cluster production

Compact production clustering is a shared-core operation behind
`phase3 build-clusters`. It consumes the authoritative compact EmuIR and a
same-generation packed eligibility/group request. C++ owns union-find,
canonical lexical membership, integer CSR columns and resource sums. An illegal
high-fanout net is unioned as its endpoints arrive, without retaining a net
membership set. Resource payload caching has a fixed bound independent of cell
count. Group unions and fixed ownership preserve the reference behavior.

Python borrows read-only memory-mapped columns, including the UTF-8 name pool;
it does not rebuild the primary arrays or rerun the union/resource loops.
Borrowed columns retain their mapping and phase-owned scratch until the last
consumer releases them. That owner releases views and closes the mapping before
removing its directory, including exception cycles; no independent weak-reference
scratch finalizer may unlink a still-mapped file. The final cluster/assignment
schemas remain unchanged,
and candidate eligibility plus remaining final semantic/index publication still
need native ownership. The public reference constructor remains independently
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
