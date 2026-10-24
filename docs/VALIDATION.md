# Validation requirements

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

[The full-flow registry](../benchmarks/end_to_end_validation_matrix.json)
identifies real workloads and platforms separately.
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
