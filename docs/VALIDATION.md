# Validation requirements

## Cross-branch integration gates

Run `python3 scripts/check_data_plane_contract.py .` and
`python3 -m unittest discover -s tests -p 'test_data_plane_contract.py'` on every
change, alongside affected component tests. CI checks registered entrypoints,
production imports of test/retired modules, registered native source ownership,
retired-path reintroduction and explicit exception metadata. It deliberately
does not attempt to infer algorithmic complexity from Python syntax. PR review
must inspect data population, SQL iteration, FFI payloads, bounded controls,
identity and lifecycle. New exceptions must be reviewed with a source revision,
scope, reason, owner and concrete retirement condition; absence is not approval.

1. Start new features from latest `main`. Shared governance lands on `main`
   first, then is explicitly merged into active development branches.
2. Inventory merge bases, unique commits, uncommitted work and live consumers.
   Use an isolated branch from the intended consumer's exact committed head.
   Do not switch, rewrite or edit a checkout used by another task or live job.
3. Integrate shared infrastructure before stage producers/validators, then
   backend adapters. Record prerequisites and unresolved overlap. A clean
   textual merge is not proof of semantic compatibility.
4. For a public contract change, update both physical consumers and exercise
   their normal and rejection paths. Retire the old producer/entry with its
   tested replacement; add an explicit tombstone to the structural registry.
5. Run component/contract gates daily. Cross-stage semantics, physical behavior
   or performance-critical changes additionally need corresponding NVDLA
   qualification, with exact source/config/tool identities. Keep integration
   status separate from real-design qualification and complete Phase 7/7C
   acceptance. Small fixtures never stand in for those gates; documentation
   changes alone do not trigger an expensive full flow.
6. Preserve experimental labels for unaccepted Vivado capabilities. Preserve
   active NVDLA/backend jobs and their immutable source/outputs. Remote tests
   deploy an exact pushed Git SHA through the existing connection policy.
7. Before retiring a branch, compare unique commits and verify all intended
   consumers have switched and no live task uses it. Preserve unique work on a
   reviewable branch; do not batch-delete branches or force-push active history.

Before each push, update README when its entry-point meaning changes, check
changed local Markdown links/anchors and `git diff --check`. Keep run-specific
integration inventories and qualification results outside public source docs.

Validation establishes a specific claim on a declared input population.
A structural check, functional fixture, optimizer objective, and physical
timing result answer different questions.

SQLite handoff tests must exercise URI parsing with the process-wide URI
default disabled, as well as filenames containing Unicode, `?`, `#` and `%`.
Inputs attached by a writable staging connection must remain read-only, and a
missing input must fail without creating another database. The native route
publication regression runs the real producer and SQL publisher in an isolated
process configured without the URI default; it also rejects Python route-tree
materialization during publication.

## Experiment lifecycle

Run the selected complete flow in one isolated directory through the current
compile interface. Phase intermediates exist only while the run is active.
After all owned processes have exited, retain compact source/config/tool and
logical identities, correctness/physical/timing seals and wall/RSS/disk metrics;
retire replay-only payloads. A failed run retains a compact failure record and
cannot publish passing acceptance. An explicitly requested debugging continuation
may read a completed input without making a persistent checkpoint cache.

The old `experiment-cache`, `experiment-stage`, `validation-farm`, `archive`,
`benchmark-experiment-compile` and contest `matrix-*-farm-spec` commands are
removed with their DAG/cache, stage replay, GC, archive and farm implementations.
Their checkpoint-only QoR aggregators are also removed; old bundles require the
matching Git revision. The current complete-flow validators, original-path
timing coverage checks, Chimew pipeline, contest import/materialization and both
benchmark registries remain. A future terminal provider comparison must consume
compact evidence from complete isolated arms; it must not recreate the retired
checkpoint/DAG protocol. Removing that protocol is not physical or timing
qualification, and neither a unit fixture nor a partial run promotes a planned
registry entry.

## Functional and structural gates

- Preserve circuit connectivity, primitive parameters, clock/reset semantics,
  resources, and stable identities across representation changes.
- Check capacity, fixed/group ownership, SCC/stateful/cascade atomicity,
  topology, and route-hop constraints independently of the optimizer.
- Check complete route and schedule-entry coverage, direction, ratio/lane/slot
  legality, and transport boundary identity.
- Check macro-cycle equivalence and startup/shadow semantics for the selected
  transport. A finite trace is not general formal proof.
- Admit each physical primitive and constraint explicitly; account for every
  cell and check site/BEL/pin ownership, macro/cascade legality, DRC, and
  unrouted nets.

A zero-combinational-cut flow is a compatibility test, not Static Exact
feature evidence. Real-design qualification must use naturally selected cuts,
not assignments forced to make a feature appear exercised.

## Physical and timing store publication contracts

Run `tests/test_physical_store_publication.py` and
`tests/test_physical_owner_publication.py` with the built shared native entry,
together with the boundary, logic-segment, local-path, OpenPARF source,
Phase 6 lowering and both physical backend tests. Native rejection must not
select a reference publisher. Changing a selector/store while a derived writer
consumes records, or during independent reader validation, must fail without
rebinding the reader to the new generation. Test direct paths and hardlink
aliases before acquiring a writer lease, closed views, and source changes both
during relocation copying and after the new database is staged. Preserve the
prior selector, query sidecar and SQL owner and remove unpublished staging on
failure. Exercise the full indexed placement lowering and mapped atomic-source
producer with poisoned input generations; the normal differential tests still
compare complete logical records and ownership with their independent oracles.
Cover native rejection and input changes at mapped-index, name-map,
placement-certificate and typed handoff publication. A live WAL on a registered
source store must be rejected even when the main database generation has not
changed. Readers must retain selector as well as SQL generations. Include
`tests/test_chimew_pipeline_lifetime.py`: retain the validation exception and
traceback while verifying that owned report lookups and leases have already
closed and a new writer can proceed. Close corruption-fixture SQLite handles
before rename/unlink and close physical summaries before directory retirement;
do not ignore NFS artifacts or relax the exact inventory checks to pass.
These component gates are required but do not constitute full NVDLA physical or
global timing qualification.

## Native store publication qualification

Private EmuIR finalization gates compare the deterministic transport topology and
routed timing model with the independent reference. Observe that the prior public
owner remains unchanged during finalization, then inject a completed-transform
failure, changed named input and native rejection. Trace transport SQL to reject
implicit integrity/quick checks while retaining electrical checks. Streamed routed
endpoint payloads must retain their original generation along with their selector
and mapped source. Typed Chimew relocation gates reject changed sources and failed
selector commits while preserving the previous selector/payload pair; they prohibit
logical graph materialization during the copy.

Component gates exercise current upstream EmuIR, assignment, route, schedule,
timing/weight, QoR and physical auxiliary owners. Inject native rejection and
source replacement after row construction or independent validation; require the
prior committed result to remain readable and prohibit reconstruction fallback.
For TimingPathDB and weights, fail selector writes both before and after replacement
and interrupt the prior-selector backup. Check the selector and its original SQL
payload together. Geometry alias failures must preserve the old target and
successful aliases must share the sealed inode. Rejected lazy readers must close
their SQLite connections and generation leases even when the exception traceback
remains alive; NFS cleanup must not depend on garbage collection.

Logic-segment identity commits use the common selector helper. Fail its selector
backup after writing a partial file, both with and without a query companion;
the old selector, SQL and query must remain intact. Fail before/after selector
replacement and at parent-directory sync. If selector restoration itself fails,
retain both SQL generations and the previous-selector recovery file instead of
deleting the SQL referenced by the visible selector. The stage must report failure.

`tests/test_phase6_selector_publication.py` applies the same late-failure gates to
pin plans, position hints, electrical binding and Chimew bank input/report owners.
Fail after selector replacement, change a bound source during replacement, interrupt
the previous-selector backup, and fail rollback. Require the prior pair or complete
recovery generations respectively. Keep each existing independent view validator;
after it returns, changed sources must still reject publication. A returned SQLite
reader must close before rejected-payload removal. If reader close itself fails,
preserve both generations and the recovery selector. Exercise typed bank-input
relocation with the same source and rollback gates.


The noninstalled `scripts/validation/qualify_phase6_publication.py` executes the
complete production Phase 6 API. Its bounded JSON request uses
`emuflow.phase6-publication-qualification/v1` and declares the source commit,
workspace, fresh output directory, current typed schedule, compact EmuIR,
assignment, BoardDB, explicit clock periods, expected design/platform/FPGA IDs
and a positive instance floor. Static Exact requests also freeze the semantic
contract identity. The selected native runtime must name the same
source revision. Temporary files remain inside the invocation workspace.

Both qualification modes use identical row construction and independent
validators. `native` calls the shared publisher. `python-reference` replaces
only closed-file fsync/rename with the prior I/O boundary inside that isolated
qualification process. No production option or error fallback selects this
reference. This comparison measures publication integration overhead, not a
different scheduler, split algorithm or physical backend.

Run each arm in a separate process. The tool measures complete API wall time,
sampled process-tree RSS and workspace logical/allocated file bytes. Its Linux
disk sampler includes the producer's unlinked workspace file descriptors and
deduplicates hardlinked inodes; borrowed symlinks are excluded. Record sampling
and CPU scopes explicitly; these samples are not guaranteed high-water values.
The current result contract is
`emuflow.phase6-publication-qualification-result/v2`. `compare_results()` requires
matching source/configuration and input generations, all generated RTL/XDC hashes,
and full-record seals of both SQLite owners. Each seal records the file hash,
application/version identity, every table's column/primary-key signature, row count
and a SHA-256 over all fields in primary-key order. Type tags, explicit byte lengths
and binary string encoding distinguish integers, reals, text, blobs and NULL;
signed floating zero follows SQLite equality. Different page layouts or insertion
orders do not change logical identity. Unknown columns/keys, nullable identities,
generated/hidden columns and changed generations fail closed. Small differential
fixtures compare the seals against independent two-direction typed SQL relations.

Sealing is post-API verification. Its wall time is reported separately and excluded
from the measured API wall/CPU/RSS/disk intervals; whole-worker measurements still
include it. It is not a production handoff scan. Each arm may retain its compact
result and full-record seals, prove worker termination, and retire its payloads
before starting the next arm. Comparison must continue to work after that
retirement, and may not substitute sampling or omit a field. Storage admission
must cover one arm's construction/verification peak and the shared owned inputs.
Three alternating pairs must meet the shared-core
integration threshold of at most 5% wall-time regression. Preserve compact
identities, comparison and resource evidence, then retire the invocation's
scratch after its processes exit. Component qualification does not establish
complete Phase 1--7/7C, physical or global timing acceptance.

Final cluster catalog component gates compare every decoded section, member,
resource, sparse fixed/group entry, candidate name and document against the
independent reference serializer. Include nonempty eligibility, Unicode and
source-sequence order distinct from lexical ordinals. Prohibit host cluster/name
iteration on the native path. Reject resealed duplicate members, mismatched names,
foreign/missing native owners, malformed checked requests and source changes
between native generation and final adoption. Selector tests cover pre/post-write
failure, incomplete backup and failed rollback recovery. Small fixtures establish
these contracts only; real NVDLA equivalence, wall/RSS/I/O and complete physical
and global timing acceptance retain their normal gates.

## Native producer differential

The offline command
`PYTHONPATH=src python scripts/validation/qualify_phase3_native_producers.py --ir INPUT.sqlite --platform BOARD.json --out OUTPUT --expected-design TOP`
compares the reference and native candidate/primary-cluster producers on the
same immutable compact input. Optional `--constraints`, `--minimum-instances`
and `--source-commit` freeze explicit constraints, a scale floor and versioned
runtime identity. It does not rerun synthesis or partition search.

Each arm executes in its own supervised process with separate temporary storage.
The tool hashes the input once before both arms and compares all ordered bitmap,
name, offset, member, source-owner, resource and fixed/group columns. Hashing is
outside the measured producer intervals. Results retain arm order, sampled
process-tree RSS, wall time, parent CPU and separately scoped native operation
metrics. A native process RSS high-water value is not combined tree RSS.
Only compact identities and measurements remain after worker exit and scratch
retirement. Failure cannot publish a passing certificate.

This is a representation-boundary diagnostic, not Static Exact feature evidence,
a whole-Phase-3 performance result, an algorithm/provider comparison or complete
Phase 1–7/7C qualification. The declared real workload, naturally selected cuts,
physical gates and original-path timing coverage still need their normal terminal
acceptance; fixtures and this producer comparison do not replace them.

## Native physical export differential

The offline command
`PYTHONPATH=src python scripts/validation/qualify_rwroute_native_export.py --mapped MAPPED.sqlite --physical PHYSICAL.sqlite --expected-top TOP --out OUTPUT`
executes the reference and native exporters in separately supervised processes.
Pass `--virtual-io-contract` and `--resource-limits` to freeze the actual board
configuration, `--minimum-logical-cells` for a real-design scale floor, and
`--source-commit` to require the exact deployed source/runtime. Inputs are hashed
once before either arm; complete TSV hashing follows the measured export.
Compact output identities, counts and separate sampled-tree/worker/native metrics
remain after both workers exit and their TSV/scratch payloads are retired. A
failed worker cannot publish a passing qualification.

For the typed RWRoute boundary, compare the entire TSV byte stream and every
cell/expansion/gate/net count against the separately retained Python reference
using identical mapped index, physical handoff, I/O, resource limits and source
digests. Cover LUT6_2 pin expansion, RAM integer/binary parameters, CARRY8 shared
exits, constants, driverless/sinkless/intra-site nets, Unicode identities, fixed
I/O direction/bit ownership and transitive clock-gate closure. Include closed
gate cycles, invalid roots/enables/sinks, mixed input/output/inout directions,
shuffled source insertion order and sparse nonnegative source sequences. Reject
orphan pin sequences and sequence/name mismatches. Require complete byte parity
for these cases; catalog and visited-row counters check the bounded native
representation without serving as an electrical oracle. Poison Python
cell/pin/gate cursors on the normal typed entry to prove it uses the native
owner. Corrupt request CRC/version/length/generation and input relations; reject
aliased destinations and preserve prior publications. Retained mapper objects
must be closed before rejected semantic staging is retired, including on NFS. Borrowed
native record iterators and decoders must keep their mapping/scratch alive after
the outer view is dropped; exhausted/closed borrowers release it. Test normal
release, traceback cycles, malformed readers and idempotent explicit close with
retained mmap objects, and assert unmapping precedes the retirement callback.

Fixture parity is a contract gate. Performance claims require the same real
NVDLA input and measured export scopes; routing, physical legality, full original
path timing and complete Phase 1–7/7C qualification remain separate gates.

## Primary-store handoff differential

W0 reader qualification also builds
`scripts/validation/native_store_view_probe.cpp` against each compared core's own
headers and library. Run both binaries over the same immutable NVDLA EmuIR with
the explicit registered schema. Compare complete ordered cell-state and
connectivity facts, record counts and SHA-256, including pin identities and sparse
endpoint ordinals. Hash the shared input once outside both measured intervals;
record each core/header/source identity and common operation metrics separately.
The probe performs an explicit offline full scan; it must never be inserted into
normal phase handoffs. Reader parity and overhead do not establish full Phase
1–7/7C or physical/timing qualification.

The test-only `native_patron_ingress_test` compares the direct primary model
with an independently generated typed reference model. Complete field
signatures include every cluster weight, owner, fixed constraint, resource
bound, route, capacity domain, net member and timing path. This oracle lives in
tests; the retired membership/resource snapshot reader is not retained for it.
These contracts do not qualify algorithm-selected cuts, whole-Phase-3 speedup
or complete Phase 1–7/7C physical/global timing.

`PYTHONPATH=src python scripts/validation/qualify_primary_loads.py --ir INPUT.sqlite --platform BOARD.json --out OUTPUT --expected-design TOP`
performs an explicit offline arithmetic differential. Optional `--constraints`,
`--minimum-instances` and `--source-commit` freeze workload and installation identity.
It builds the shared primary store once and compares complete per-partition
cluster/cell/resource sums from independently expanded reference records with
the native reduction. Both consume the same declared round-robin integer vector.
This arithmetic vector is not a legal candidate or feature qualification and is
never submitted to partition search; no capacity or balance policy is relaxed.
The compact result includes the complete load identity, both reduction wall
times and native subprocess metrics. Whole-worker RSS is a lifetime high-water
value, not an incremental reference/native memory comparison. Parent-owned
scratch is removed only after the supervised worker and native readers exit.
Unit contracts also poison compact record expansion in assignment construction
and final validation, compare full independent balance reports, and require
coverage, catalog, version, checksum, generation, ownership and capacity failures
to preserve any previously published output.

Native cut publication compares every SQL record, the canonical cut digest and
exact-binary bytes with the independent reference writer. Tests poison host cut
expansion and normalization only during native production; the separate output
validator still runs normally. Coverage includes Unicode identities,
noncanonical source order, sparse endpoint ordinals and zero cuts. Malformed
requests and source-bound node envelopes, identities, ownership, offsets and
launch flags must reject without replacing the prior index/binary pair. Node and
capture generation changes, including ctime-only changes after independent
validation, must fail before pair commit.

Independent native cut validation compares its complete count/digest with a host
record oracle on semantic, nonsemantic, Unicode, sparse and empty inputs. Poisoned
host iteration and JSON formatting must not affect normal validation. Negative
cases cover orphan/missing/self references, ownership, order, incomplete and
mistyped scalars, metadata, versions and invalid UTF-8; the source and previous
summary must remain unchanged. Paired ordered storage populations verify complete identity and bounded host
memory growth after protocol warmup, separately from fixed interpreter/subprocess
buffers. This storage fixture is not circuit or feature qualification.
Malformed or oversized native summaries and source-generation changes reject
without a host fallback. Native node/capture fixture scopes explicitly close
mapped owners on success and exceptions before replacing files or retiring their
directories; retained scalar borrowers must then reject access. This ordering
also applies to differential hashing and publication tests on network filesystems.
These are component gates; full Phase 3 and complete NVDLA acceptance still
require their declared real-design evidence.

## Timing acceptance

Native compact capture contracts compare complete output bytes with the explicit
legacy producer and complete records with the independent Python source oracle.
Fixtures include sparse endpoint ordinals, Unicode identities, repeated physical
endpoints, top outputs, zero captures, relay dependencies and the largest compact
integer bit. Negative cases mutate record coverage/order/identities/bits/offsets
and require existing outputs and proofs to survive rejection. A source-resource
mutation accepted by the producer's selected-flag query must still be rejected by
the independent shared resource decoder before publication. Poisoned Python
capture replay verifies that normal production crosses only the native boundary.
These fixtures are contract tests, not a substitute for real-design feature,
whole-phase runtime/RSS or complete physical/global timing acceptance.

A complete physical flow requires explicit clock periods, routed logic and
interface timing, concrete board routes/TDM events, and original-path binding.
Missing data makes the result incomplete; it must not be replaced by guessed
delays or silently dropped paths.

Global WNS is the minimum slack over the declared original-path population.
Global original-path TNS sums each negative member slack once. Same-FPGA and
cross-FPGA paths must form a disjoint union of that population. Report:

- source/original-member coverage and any extraction limit;
- target-clock and virtual-runtime-clock WNS/TNS;
- negative-path counts and the critical path;
- per-FPGA physical diagnostics, separately labelled;
- exact-delay, analytical-model, and conservative-bound qualifications;
- transport readiness/relay/commit failures.

An optimizer representative may establish a worst path, but it must be expanded
to its original members before original-path TNS is accumulated. The metric is
not conventional unique-register-endpoint TNS. See
[global timing scope](global-opensta-qualification.md).

## Native OpenSTA path import

Compare every imported path, endpoint certificate, ordered net membership,
normalization value and original-path QoR with the test-only independent
producer. Include escaped provider identities, constants, scalar/bus top ports,
legacy path IDs and cancellation-sensitive sums. Poison Python path parsing,
object-table construction and full-graph loading in the native path.
Malformed hex/UTF-8, duplicate IDs/nets, unknown nets, invalid timing, request
version/CRC/truncation and changed source generations must reject without
replacing a previous selector or payload. Independently mutate final ordering,
membership, source endpoint identities, schema/index definitions, normalization
and summary seals to exercise the final-record auditor. Verify bounded Python
memory with distinct path/net populations and avoid enumerating unreached wide
port bits. Immediate proof reuse must reject altered manifests or generations
and must not trigger another full-row validation. Fixtures establish component
correctness only; complete real NVDLA qualification remains required.

Timing-weight gates compare all typed rows, policy fields and summary values with
the test-only independent producer. Include fractional exponents, zero-criticality
populations and values sensitive to fused multiplication/addition. Poison Python
path and weight iteration during both production and independent validation.
Mutate stored criticality/count/weight, membership coverage, source binding, policy,
summary and payload descriptors; request CRC/version/generation rejection must
preserve the prior publication. A distinct path/net population must not create
Python dictionaries proportional to the population. Exercise the default CLI
extraction-to-weights chain without private output-mode arguments, and reject
legacy EmuIR before invoking OpenSTA. The compiled consumer must reuse the original
generation-bound path proof without a second full audit and reject changed
owners, manifests or a bare boolean masquerading as that proof. Retain full NVDLA equivalence and performance
qualification separately from these component gates.

## Independent native schedule validation

Native compiler gates must prove that TDM never invokes a Python adapter. Compare
`phase5 compile` with the independent Python configuration bridge on the same
current typed owners, including empty/asymmetric bank loads, ratio/fixed delays,
multicast, ordering and exact record/certificate seals. Retain route table/column,
sequence, orphan, optimization relation and selector seal rejection. Wrong board,
assignment semantic identity or corrupted sources must preserve prior generations.
Exercise the versioned phase-report reference codec with foreign schemas, changed
generations and marker-like ordinary user lists. Chimew device/catalog scopes must
survive a native TDM step and still reject mutation at the next consumer.

Whole-stage gates invoke `phase5 run` and compare every logical schedule record
with the independent reference for fixed/affine latency, shared direction banks,
multicast and repeated demands. Poison Python producer, validator and projection
entry points to prove that the complete stage owns their execution in C++.
Verify the report's canonical identity and every generation-member seal using
the normal consumer. The native API progress observer permits test-only injected
exceptions or process exit after each of the four member publications. Exceptions
must restore all previous bytes and retire uncommitted staging. Process exit
must leave either the complete old generation, a generation rejected by its old
report seal, or the complete new generation after the report commit. Reject
invalid checked requests and original-source bindings before replacing any prior
generation. These fixture gates do not establish real NVDLA performance or W7
completion; the native compiler still has bounded Python configuration adapters.

Compare the native indexed validator with the small independent dictionary oracle
for fixed and affine latency, shared direction banks, dependency chains, multicast
relays, different net/demand order and empty populations. Poison Python route/hop
cursors, dependency ordering, logical-seal replay and temporary validation indexes
while exercising the normal producer/report boundary. Require full metric,
certificate and record equivalence, with an explicit validator implementation ID.

Forge a new valid storage seal after changing events, route identity/edges/sinks,
completion/readiness, capacity, ratio/lane, domain counts, metrics or certificates.
The independent reconstruction must still reject. Separately reject broken section
and complete logical seals, orphan child records, stale source generations,
truncated/version/CRC failures, input/report aliases and native execution failure;
previous summaries and published generations must survive rejection. No failed
native operation may retry the Python oracle. These component gates do not replace
real-design Phase 1–7/7C runtime, memory and complete timing qualification.

## Native Phase 5 timing projection

Compare complete timing summaries and canonical path-binding projections against
the independent inline reference. Cover fixed and ratio delays, shared banks,
explicit multi-cut chains, shuffled multicast trees, Unicode identities, missing
optional member/required fields, equal normalized slack, signed zero and quantile
boundaries. Compare the full projected array digest including every member and cut
field, even though production publishes only its descriptor. A large compressed
member group must leave the descriptor bounded; poisoning Python route/path/hop
iterators must not affect typed production.

Freshly resealed bad source records must reject unknown cuts, malformed transitions,
missing/duplicate members, invalid clock/required/fixed values, negative waits and
ratio errors. Check request CRC/truncation, input generation, source/output aliases,
prior-summary preservation and absent-schema rejection before opening circuit data.
Run Phase 7C and physical-binding consumers with v3 and require normal Phase 5
report-last rollback/revalidation. Numerical equivalence preserves the existing
pre-physical diagnostic population and must not be relabelled as original-path
post-route WNS/TNS. Real NVDLA runtime/RSS and whole-flow Phase 7/7C gates remain
separate required evidence.

## Fair algorithm comparisons

Use real RTL and an explicitly identified platform. Raw contest communication
graphs cannot substitute for synthesis and FPGA-internal physical implementation.

Freeze the common inputs, target periods, mapping/device models, tool versions,
and physical seed. Only the intended variable may change. For a Phase 6
comparison, assignment, routes, and TDM schedule are common inputs. For a
partitioning comparison, downstream assignment/routes/schedules may differ and
must not be mistaken for controlled inputs.

Default acceptance uses physical seed 1; multiple seeds require an explicitly
declared variance study. Small exact references qualify correctness. Provider
or default-selection changes additionally need a materially sized real-design
comparison through complete physical Phase 7 and global timing. Intermediate
metrics do not establish end-to-end QoR.

Report absolute slack changes and negative-deficit reduction. If the baseline
already has nonnegative WNS or zero TNS, percentage deficit improvement is not
defined. Keep regressions in secondary metrics visible.

## Workload and platform identity

Every full-flow experiment must declare its exact workload and platform
separately. The VTR-only planned KOIOS matrix and its validator are retired;
its IDs cannot be reused for runs with a different mapping profile.
[The contest registry](../benchmarks/contest_validation_matrix.json) covers
communication inputs only. Raw contest topology combined with a VTR device
template is a derived academic platform, not the original contest's physical
hardware.

A successful run does not prove untested workload/platform combinations.
Bitstream generation, measured-link timing, CDC/hold/PVT signoff, and actual
board operation require their own evidence.

## Execution and retention

Keep experiment configuration, machine paths, raw reports, timing populations,
placement/routing directories, and iteration histories outside the source
repository. Use isolated active-run scratch; retain compact final summaries
outside the repository after terminal validation.

Do not restart computation merely because an observer disconnected. Check its
recorded identity and process tree. Remove scratch only when its producers and
consumers have stopped. Do not publish a partial stage as a completed flow.

### Topology hop legalization interface

The shared `phase3 refine-hops` operation must retain the same numerical model,
solver decisions, objective metrics and ordered assignments. Compare summary
and diagnostic output on a model that requires moves: the summary omits `MOVE`
records but reports the same committed count and final assignment. The existing
independent hop, capacity and assignment validators remain authoritative.

Malformed, duplicate, truncated, oversized-count or incomplete model records
must fail before publishing. Input/output aliases and stale `ArtifactRef`
generations must be rejected; a prior output survives every rejected request.
Exercise the in-process API as well as the unified CLI and process supervisor.
These are component interface gates; they do not replace real-design NVDLA
physical and global timing acceptance or claim native model construction.

### Native-control Chimew boundaries

Exercise the actual Chimew lookahead and typed kernels across serialized split,
physical and signoff control boundaries for both supported Xilinx backends.
Keep the same upstream, physical options, fixed wiring and baseline/candidate
physical/global timing validation. Control tests with physical or timing engine
doubles qualify these boundaries only; real NVDLA remains the design acceptance
workload and must independently complete both arms.

The device catalog is bounded independently of circuit size. Restore it without
rereading the ArchitectureDB site population; retain and check the original
source generation. Changed/missing sources, malformed geometry, boolean or
negative coordinates, oversized pin counts and duplicate baseline report owners
must reject. The C++ multi-process controller must stop on such a rejection and
must keep `native_data_plane_complete=false` while Python phase adapters remain.


### Native Yosys hierarchy validation

The hierarchy producer is checked against the independent former Python
implementation under `tests/yosys_hierarchy_reference.py`. Compare complete
flattened cells and connections, canonical signal names, top ports and hierarchy
counts, including repeated/nested modules, late attributes, blackbox/whitebox
leaves, open ports, aliased outputs and late constant output propagation.
Malformed/truncated JSON, duplicate identities, recursion, width mismatch and
conflicting constants must reject. Stream cancellation must reap the native
consumer and retire its private writer files. A large distinct-cell source checks
that the Python byte host has bounded memory and that the importer adopts one
staging database without copying cell/name populations. These component gates do
not establish NVDLA equivalence, speedup or complete Phase 1–7 qualification.


### Native hierarchical Xilinx normalization validation

Compare the entire normalized cell population and primitive audit against the
independent existing Python normalizer on the same expanded records. Coverage
includes paired/single CARRY4, omitted O/CO ports, CI/CYINIT helpers, external
versus cascade consumers, shared carry exits, LUT6_2 route-throughs, INV,
negative-edge FF parameters and MUXF7 constant leaves. Preserve exact helper
names and allocated signal identities, not only total counts.

Reject unknown primitives, invalid widths/directions, conflicting clock
parameters, helper/source identity collisions, changed primitive-library
generations and inconsistent library resources before completed output. A large
negative-edge FF population checks bounded Python host memory and in-place FF
updates. An integration test forbids the Python primitive normalizer in the
production hierarchical path. Final native primitive census remains separate
from rewrite decisions. These are component gates; they do not establish
complete frontend ownership or real NVDLA Phase 1–7 equivalence/performance.


### Native hierarchical compact EmuIR producer

Compare the complete source-to-owner result with the independent original Python
hierarchy producer and existing flat importer. Check every cell, parameter,
attribute, resource, constant, port, clock, net ID/name/alias, driver/sink ordinal,
fanout, cut class and warning. Payload checks retain arbitrary-width integers,
precise floating-point values and nested Unicode data. Include explicit versus
inferred clocks, canonical-name collisions and late child constant outputs.

The producer's independent audit merges source and final endpoints and separately
checks payload preservation, aliases, canonical identities and classification.
Mutation of either original output generation, callback failure, source changes
and publication failure must preserve the previous owner. Large fanout verifies
bounded Python host memory with its row writer and endpoint grouping disabled.
The final catalog must contain only the current compact schema and required
index, without source staging tables or raw-record free-page inflation. These
checks qualify the producer boundary; full Phase 1–7 NVDLA provenance, runtime,
process-tree RSS, disk, physical and global timing acceptance remain separate.


### Complete native RTL frontend qualification

The native frontend component gate compares complete hierarchical/normalized
EmuIR records against the independent former source producer and compares
resource, cut-class and clock reports with the independent Phase 1 API. It
exercises a large fanout alongside stderr exceeding pipe capacity, real engine
failure after a complete JSON stream, early parser failure, original source
mutation, strict fabric-clock rejection, checksum corruption, wrong output
ownership, existing publication, deadline and signal cancellation, detached pipe
writers and complete-stage controller dispatch without a Python frontend adapter.
The controller result must satisfy its current schema and retain implementation
and RSS scopes. Both supported assignment generations must stream exact physical
segments/captures without a compatibility fallback or full reconstruction.
These component gates do not qualify the NVDLA design; current-source complete
physical/global timing evidence remains a separate required gate.
