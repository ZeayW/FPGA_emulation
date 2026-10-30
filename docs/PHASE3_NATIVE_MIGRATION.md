# Native data-plane migration

## Architecture and ownership

`libemuflow_core` owns the maintained production flow. `emuflow-native compile
REQUEST_JSON` resolves configuration, admits sources, executes the dependent
stages, supervises engines and publishes the complete flow report. The public
`emuflow run` command encodes bounded options for that controller. There is one
upstream algorithm chain, two physical backends and optional Chimew:

- Yosys synthesis and Xilinx UltraScale+ mapping, then native EmuIR import;
- pre-placement OpenSTA and indexed timing weights;
- TritonPart initialization followed by PATRON v14, with Static Exact
  combinational cuts;
- timing-aware global-candidate board routing;
- deterministic dependency earliest-slot TDM;
- baseline split/transport, or Chimew baseline/candidate comparison;
- OpenPARF/RWRoute or Vivado physical implementation;
- physical evidence and TDM event binding followed by global OpenSTA.

Yosys, OpenSTA, OpenROAD/TritonPart, OpenPARF, RapidWright/RWRoute and Vivado remain
external engines. The core owns their requests, source identities, lifecycle,
result import and validation. OpenPARF keeps its required Python/PyTorch solver
host. A size-limited independent Python behavioral oracle may qualify small
Phase 6 inputs; large designs retain native constructive/electrical validation.
A native stage failure stops execution without selecting another implementation.

[FLOW_PLAN.md](FLOW_PLAN.md) defines the storage and whole-flow contracts;
[ALGORITHMS.md](ALGORITHMS.md) defines maintained providers; and
[XILINX_PHYSICAL_FLOW.md](XILINX_PHYSICAL_FLOW.md) defines physical acceptance.

Implementation, retirement and terminal qualification are separate requirements.
`native_data_plane_complete` remains false until the retained entry paths,
retirement checks and current-source NVDLA physical/global timing acceptance have
all passed. Component tests and successful admission do not establish completion.

## Shared core (W0)

The executable and native library tests link one `libemuflow_core`. Its checked
source inventory is `src/core/sources.txt`. Native operations share registered
schemas, `ArtifactRef`, `StageRequest`, `StageResult`, common errors and metrics,
SQLite RAII, CRC-32/SHA-256 and atomic publication. CLI parsing is a boundary
adapter to the same library operation rather than a second implementation.

An integer ID retains its artifact namespace. Cell/net/path/endpoint identities,
primary membership ordinals, route source sequences and canonical net order are
distinct coordinates. Equal integers from different owners are not equivalent.
Canonical ordering and explicit tie breaks apply to final records and hashes.

`ArtifactRef` binds a registered schema, canonical path and local file generation
(device, inode, bytes, modification and change timestamps). Operations check
retained generations before and after execution, including publication. They do
not silently bind a stale reference to a replacement file. Supplying both bound
artifacts and paths for the same input is ambiguous and is rejected.

Generation metadata is not a portable or cryptographic semantic identity.
Independent source joins, counts, producer seals and logical hashes remain
required. File hashes establish artifact provenance and integrity; canonical
ordered-record hashes establish representation-independent logical identity.
Different SQLite page layouts must not appear as a circuit change.

Supported readers have explicit registered versions. Current ScheduleStore
production accepts storage v4; old snapshot writers and version aliases are
retired. EmuIR and assignment readers retain only the versions required by
identified consumers. Unknown versions and mixed generations fail closed.

### Storage, leases and publication

`SqliteStore`, typed views and statement/transaction owners share connection and
artifact lifetimes. A final SQLite publication is a closed single file. Active
WAL/journal sidecars, unfinished statements/transactions, aliases and changed
sources reject adoption. Normal handoff does not run an implicit integrity scan.

Producers use the common `SqliteArtifactWriter`, `AtomicBinaryWriter`, directory
publisher or selector commit boundary. Existing private staging is adopted after
closing its writer; it is not copied into another intermediate database. Final
selectors retain the original source bindings through validation and directory
synchronization. Failed replacement restores the previous selector and preserves
recovery material if restoration itself fails. Consumers require successful
whole-stage publication when several companion files form a result set.

Canonical SQL lives under `src/python/emuflow/data/schema`; native headers and
reader/test definitions use that same owner. A logical fact has one authoritative
owner. JSON carries bounded configuration, summaries and references. Explicit
typed binary records remain appropriate for ordered assignment semantics.

Lazy readers keep their connection lease until a cursor finishes or closes.
Rejected returned views close before their staged files are retired. Python
SQLite contexts explicitly close their handles as well as commit or roll back;
garbage collection and unlink semantics do not define connection lifetime.
Input URIs preserve read-only mode and path characters such as `?`, `#` and `%`.

Private staging, mapped source columns and cached query state retire after their
last borrower. Required downstream owners are published outside stage scratch.
A live process retains its source/build/install and artifacts until it terminates.
The controller removes consumed control states; it does not create a persistent
checkpoint DAG or reuse a partially published result after failure.

### Errors, engine lifecycle and measurements

`emuflow-native --report RESULT_JSON PHASE OPERATION ...` optionally publishes a
bounded [operation result](../schemas/native-stage-result-v1.schema.json).
Registered input/output references, status, errors and metrics share one format.
Report paths cannot alias inputs, outputs or companion stores. Payload stdout
retains its stage protocol, including complete CRC/EOF validation for pipes.

`RunExternalTool` owns literal argv/environment, working directories, resource
limits, deadlines, a separate process group, cancellation escalation and `wait4`
reaping. It never constructs an engine command through a shell string. Engine
launch scripts retain their own explicit executable identity. Fixed-size tails
retain diagnostics without a complete Python log string. Native Yosys output
flows directly into its importer. Consumer rejection cancels and reaps a live
producer; a producer failure retains its diagnosis when incomplete output also
causes a parser error. Remaining group members are cleaned up when the leader
exits. This is process-group ownership, not containment of deliberately daemonized
processes.

Operation wall/CPU/I/O intervals and engine metrics retain their stated scopes.
Overlapping parent/child or parallel intervals must not be summed as exclusive
costs. `wait4` maximum-process RSS and process-lifetime high water are distinct
from sampled simultaneous process-tree peaks. Unavailable counters remain null.
No component measurement alone establishes whole-stage or NVDLA performance.

W0 acceptance requires versioned round trips, stable ordered records and rejection
of unknown versions, corruption/truncation, duplicate keys, orphan references,
mixed generations, interrupted publication and invalid cursor lifetimes. The
same frozen NVDLA input must qualify pure integration overhead against the prior
implementation; the permitted regression is at most 5%. Fixture/API gates do not
prove that large-design gate. Infrastructure acceptance does not replace later
stage or complete-flow qualification.

## Native production entry (W7)

The native sequence is preflight → frontend → timing → partition → routing
policy → route → TDM → split → physical → signoff → finalize. Configuration and
explicit prepared-input admission are native. The Python compiler, stage adapter,
baseline/candidate physical orchestration and duplicate finalize entry are
retired. The optional Python API supplies bounded options and original references
and reads final summaries. Its memory must not grow with circuit populations.

The controller claims an empty output directory, freezes effective configuration
and source/tool identities, and checks stage order, run identity, generations and
published paths before advancing. Unsupported configurations fail before the
relevant engine starts. Failure/cancellation never advances, retries another
provider or joins results from different runs. Final report/result publication is
atomic and retains the normal electrical, timing and cross-stage validators.

| Boundary | Native owner and required behavior |
| --- | --- |
| Admission and Phase 1 | Preflight configuration, source checks, Yosys supervision, streamed import, normalization, clocks, resource census and source-bound publication |
| Phase 2 | Timing configuration, OpenSTA input generation/execution, every original path import, timing weights and independent validation |
| Phase 3 | Structural candidates, primary clusters, TritonPart, PATRON v14, independent capacity/decision/hop audits, final semantic records and assignment publication |
| Phase 4 | Source-backed routing model, candidate selection, route tree/capacity/timing audits, typed publication and independent published-owner replay |
| Phase 5 | Dependency schedule, readiness/capture certificate, typed store, independent validation, timing projection and generation commit |
| Phase 6 | Split, shadow/transport/runtime generation, fixed wiring and electrical binding, RTL/XDC, costs, independent audits and final manifest/report |
| Chimew | Baseline physical/global timing, placed lookahead, grouping/refinement and bank/channel qualification, typed pin pipeline, candidate split/physical/global timing and final comparison |
| Physical inputs | Mapped indexes, macro/atomic facts, qualified geometry, Bookshelf, safe-name ordinals, fixed-I/O and Vivado runtime inputs |
| Placement/routing | OpenPARF placement control and import, certificate/handoff, site/BEL/macro audits, RWRoute or Vivado execution, connectivity/PIP checks and closure evidence |
| Physical/global timing | Native original-path and segment queries, routed OpenSTA, event/check binding, measurement import, complete member reduction, QoR and final source-qualified report |

### Phase 3 contracts

Clustering consumes compact resource/type/endpoint projections. Packed ownership
and resource columns feed deterministic source-bound hMetis/Tcl exports and
native kernels. Capacity reservations, fixed/group constraints, SCC/cascade,
reachability and hard-hop legality remain authoritative. An initializer must
satisfy strict resource/balance limits before repair. Partitioning must not add an
early schedule-readiness, frame-feasibility or physical-segment deadline gate.

PATRON consumes compact EmuIR, indexed timing, primary columns and initial
ownership directly. Its search algorithm and policy identifiers remain separate
from implementation identity. Full model snapshots, textual membership/resource
protocols and Python pressure-net/path/name populations are retired. Native
source-cell and connectivity reducers independently check projection and accepted
ownership rather than trusting optimizer counters.

Final semantic records retain canonical cut order, dependency levels, source and
capture segments, launch flags and integer predecessor coordinates. Sparse stored
endpoint ordinals and dense logical capture positions are distinct; independent
readers reconstruct the same order. The search's lexical coordinates and final
canonical cluster ordinals remain bound by the identity-order seal.

`assignment.sqlite` owns instance/partition lookup and source-qualified cuts.
`assignment.exact.bin` owns ordered dependency/segment/capture payloads. Native
publication hashes final sections while emitting them into one staged file and
seals counts, extents and source identity before replacement. A complete Python
semantic tree, section spool copy or second full payload is not a production
handoff. Downstream views borrow the final typed owners.

### Frontend and timing contracts

The native frontend owns raw RTL and explicitly supplied Yosys input admission,
streamed hierarchy import, constant/output binding, primitive normalization,
source audits and final EmuIR publication. The old standalone Python importer,
synthesis and normalization producers and their public CLI entries are removed.
Finite mapping policy and primitive-library facts stay available to bounded hosts.
Optional characterization/statistics tools live outside the production package.

Pre- and post-route OpenSTA TSV import use the native indexed TimingPathDB owner.
Endpoint resolution, normalization, every original path, net-weight reduction and
independent source-membership validation execute without Python path populations.
Engine and EmuFlow preparation/import costs are recorded separately.

### Routing, scheduling and split contracts

Published route replay rejoins the original assignment and BoardDB, validates the
selector/record/logical seals and reconstructs demand identities, dependencies,
sinks, directed trees, half-duplex locks, capacity/bank use, delay and latency.
Original timing inputs determine path membership/compression, route signatures,
normalization, route/TDM proxy metrics and complete coverage. Replay does not
rerun routing or accept the optimizer's report as an independent proof.

The current Phase 5 API requires typed RouteStore/ScheduleStore owners. Its
independent native validator retains absolute TX events, ready/arrival, relay,
capture/commit and predecessor semantics. Inline schedule validation, Python
certificate reconstruction and complete compatibility materialization are
retired. Small independent scheduling and routing oracles live in tests. Frame
length remains an explicit run constraint; the unused frame-search execution,
report compatibility and historical schemas are retired.

Phase 6 consumes typed schedule cursors and original source ownership. Every
remote sink has exactly one scheduled shadow driver, each endpoint/copy is covered
and lane/slot/clock identities agree. Fixed board wiring, P/N pairs, direction,
shared bank inventory and RTL/XDC consistency remain required. P/N is one physical
channel rather than two independent channels.

Chimew keeps the same frozen upstream inputs for its baseline and candidate.
Both arms execute complete physical implementation and global OpenSTA with the
same backend/tool/seed/worker policy. Placed lookahead, independent grouping and
bank/channel certificates precede candidate publication. Comparison uses both
complete terminal results; admission or a partial candidate does not qualify it.

### Physical and timing contracts

OpenPARF preparation directly emits qualified atoms, macro windows, geometry,
Bookshelf, typed name/site stores, configuration and manifest. Native placement
import resolves solver coordinates through those same owners. Site/BEL,
carry/mux/cascade, constants, primitive/port identity and whole-device capacity
remain independently checked. Typed physical handoff replaces production
`placement.json` and `packed-sites.json`.

RWRoute consumes the typed handoff and source-owned exports, then publishes
routing and measurement evidence. Vivado consumes native primitive/runtime and
fixed-I/O inputs and returns source lineage, physical and timing evidence.
Both backends require complete original/transport cell accounting, fixed-I/O
coverage, connectivity, legal site/BEL/PIP ownership, zero unrouted nets and DRC
violations, and the final per-FPGA resource budget. Estimated Phase 3 resources do
not replace final physical counts. The whole-device capacity denominator remains
unchanged by placement windows or density.

Local path and logic-segment queries retain original assignment, route, schedule
and mapped/physical identities. Their indexed owners feed native event/check
binding, OpenSTA model generation, measurement import and global reduction.
Compressed representatives restore every original member. Same-FPGA and
cross-FPGA coverage is complete and disjoint; target/runtime and TX/commit checks
are present, and each original path contributes to TNS exactly once. Floating
precision and deterministic accumulation are part of the contract.

Reports summarize typed evidence rather than embed endpoints or path tables.
Standalone physical report validation consumes the original bounded summary
artifact. A bounded namespace/census mapping supplied by an explicit API caller
must not trigger traversal of its other evidence fields.

## Retirement and qualification

A replacement and its superseded production implementation are one delivery
unit. Remove old entries, configuration switches, deploy/build dependencies,
unused schemas and full compatibility producers after their required gates pass.
Git retains historical implementations. Keep independent validators/oracles in
tests, or a deliberately size-limited behavioral qualification module, without
retaining another production flow. A retired route must not return through an
installation fallback, source-path forwarding layer or silent runtime retry.

Qualification preserves algorithm/provider/policy, effective constraints, source,
BoardDB/part, engine versions, seeds and worker settings. Native/representation
changes and physical-policy changes receive separate comparisons. Preserve all
original rejection assertions when moving their fixture code.

Required evidence includes record-by-record equivalence, deterministic logical
hashes, corruption/truncation/mixed-source/publication failure cases, complete
phase and total wall/CPU/RSS/I/O observations, and both backend terminal results.
Measure equivalent inputs and tool settings before claiming a performance gain.
Enclosing/native-engine intervals retain separate scopes.

Final acceptance uses NVDLA through the complete raw RTL Phase 1–7/7C flow,
including the retained Chimew comparison, complete physical accounting, resource
budgets, fixed I/O, complete original-path global OpenSTA WNS/TNS and independent
terminal validation. Prepared-stage replay, package/admission checks and fixtures
are intermediate evidence. A result from a different native source/build requires
explicit equivalence evidence and cannot silently qualify later code changes.
Active source/build/install directories remain immutable throughout their runs.

Publication includes the exact reviewed source, installed-runtime retirement
checks and passing regression evidence. The final completion decision must check
all maintained entries and terminal gates against the actual frozen source;
implementation progress alone does not justify a completed W7 marker.
