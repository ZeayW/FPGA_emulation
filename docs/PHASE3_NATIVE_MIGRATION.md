# Phase 3 native migration

## Program status and storage decision

The staged migration plan remains the implementation direction, with one
important storage correction. SQLite is the indexed control plane for facts
that require joins, uniqueness constraints, or bounded random lookup. Large
relations that are consumed in deterministic order use versioned typed binary
sections or ordered record streams. They are not copied into SQLite merely to
make every boundary use one container. `assignment.exact.bin` is the first
production instance of this split. Compatibility JSON is an explicit exporter,
not a second authoritative payload.

Normal phase handoffs validate schema, source identity, required row counts,
keys, and circuit semantics. They do not run SQLite `quick_check` or
`integrity_check`; those remain explicit offline corruption diagnostics.

The implementation status at this revision is:

| Wave | Status | Implemented boundary | Remaining production work |
| --- | --- | --- | --- |
| W0 shared core and stores | **Started** | `libemuflow_core` now owns checked native artifact reads, synchronized atomic binary publication, and representation-scoped typed IDs. The Static Exact analyzer uses that shared boundary. | Move the remaining native tools onto the shared storage/error/metrics interfaces; add the schema registry and SQLite RAII views as consumers require them. |
| W1 Phase 5 exact schedule | **Partial** | Phase 4 now publishes a selector plus sealed typed RouteStore instead of a graph-sized `routes.json`; demands are projected from authoritative route rows, while route sinks/edges, exact predecessors and timing paths are normalized relations. Phase 5 consumes ordered route and hop cursors directly. For indexed Static Exact designs with at least 100,000 routed cuts, `emuflow_tdm_exact_scheduler` performs dependency ordering, readiness propagation, lane/slot occupancy, and completion calculation in C++. Its adapter derives integer ranks in SQL and retains compact columns instead of Python route dictionaries and net/demand maps. Fixed hop records remain in a memory-mapped lazy view before Phase 5 publishes ScheduleStore v4 with typed hops, routes, route sinks/edges, completions and sink arrivals. Both logical seals remain compatible, legacy route JSON and ScheduleStore v1--v3 readers remain available, and stores use selector-last content-addressed publication. RouteStore validates and can restore the previous generation, binds the artifact filename to its digest, validates the new view before old-sidecar cleanup, omits unjustified append-only `VACUUM`, and joins every generic archive selector/sidecar pair into one retention group. Production Phase 6 dispatch reads only the typed entry count; Phase 7C and physical/local timing readers keep RouteStore lazy. Small designs retain the Python scheduling reference. | Standalone independent Phase 5 validation still constructs route-level exact-node and topological maps. The native adapter retains R-scale compact columns, cross-layer timing materializes H-scale `hop_bindings`, the ratio-delay baseline branch performs H-scale work when enabled, and completions/sink arrivals remain route/sink-scale. Terminal full-bundle replay still materializes compatibility views in some paths. A synthetic 100,000-route one-hop build reduced Python peak allocation 346.1 MB to 108.2 MB and route bytes 46.60 MB to 32.19 MB; two repeated builds were 8.16--8.32 seconds versus 8.92--9.59 seconds. This is adapter evidence only. NVDLA wall-time, RSS and temporary-I/O qualification remain required. |
| W2 Phase 7 lowering and transport import | **Partial** | Indexed DUT lowering and placement stores avoid the former whole-DUT JSON path. Generic Yosys now streams synthesized transport JSON directly into compact EmuIR SQLite, so `transport-synthesized.json` is never materialized. Lowering opens the store as a bounded view and the placement writer uses SQLite `ATTACH` to copy, namespace and filter transport cells/nets/endpoints without reconstructing the transport graph. RX signal/placeholder coverage uses ordered store cursors. Whole-design physical assembly publishes sealed per-FPGA evidence references instead of embedding all boundary/path databases again in `physical-summary.json`; publish, parse and seal share one read pass. | Interface-net overlay records and boundary identity production still scale in Python with transport endpoints. Per-FPGA evidence remains JSON. Move those joins and certificate publication to native/indexed cursors, then qualify the full per-FPGA path for RSS and I/O. |
| W3 Phase 6 split and binding | **Partial** | DUT ownership/split uses SQLite joins. Phase 6 writes endpoint, lane, virtual-anchor, virtual-board binding and differential adapter-port relations once to sealed `transport.sqlite`. A bounded `binding_physical_ports` relation records one row per real differential pair, its member count and ordered member seal. Indexed physical-I/O contract v2 independently recomputes those memberships from one ordered authoritative binding-entry cursor, then merges them with the compact relation and adapter ports; it does not serialize every hop for each FPGA. Legacy v1 remains readable. Typed pin-plan and position-hint stores own hop-sized rows behind small selectors, validators use ordered merges/indexed joins, and Transport RTL plus XDC/SystemVerilog stream atomically. The production Chimew builder reads ScheduleStore directly, validates member coverage through a file-backed join, and streams position, pin-plan and electrical-binding rows into typed stores without hop-sized schedule, coverage or output containers. All three stores are built and semantically validated in sibling staging, then immutable sidecars and selectors are published before an adapter report commit. Standalone adapters own their fixed report and garbage collection; pipeline-managed adapters use an immutable report generation and retain the preceding generation until the outer `pipeline_report.json` commits. The complete outer input/source/kernel/adapter inventory is staged and validated before its generation directory is atomically installed, and `pipeline_report.json` alone commits that generation. Orphan recognition reuses complete bundle semantics: ScheduleStore selector/sidecar, platform, every grouping/RUDY/bank artifact, qualification and source provenance, adapter pairing, paths and aliases must all validate after the seals. Manifest design/platform identity is bound to the sealed schedule and BoardDB. A resealed malformed or relabelled payload remains invalid and is never collected as a generation. Per-output writers hold an exclusive POSIX lock; validators and production consumers retain a shared lease across report and referenced-artifact reads, preventing GC from removing an older reader's generation. Phase 7 resolves binding entries with indexed lookups, and Phase 6 explicitly inventories every selector sidecar for relocation. | Pin-plan and Chimew input production remain Python. The validated Chimew bank/group input is still graph-sized, and indexed validators perform full relation sorts. Native producers and NVDLA lookup/sort/I/O profiling remain required. |
| W4 Phase 3 model and publication | **Partial** | Large dependency/capture traversal is native; assignment index v3 separates bounded SQLite control data from typed binary Static Exact data. Capture protocol v3 preserves original EmuIR ordinals and returns only fixed-width `(cut, partition, net ordinal, sink ordinal)` records. Python dereferences the returned capture population and independently validates each record, eliminating ordered instance/net copies, the graph-sized net map and full endpoint replay. Exact-binary v2 encodes bounded canonical capture-bit bytes and validates counts before allocation while keeping v1 read compatibility. Phase 3 now carries the net ordinals discovered by cut construction directly into native input and validates each ordinal against authoritative EmuIR, removing the native analyzer's three cut-name lookup/match/coordinate hash containers on the production path. Name-only historical callers retain a checked compatibility lookup. The analyzer also returns predecessor CSR plus fixed-width launch/level/depth arrays and production publication derives all four graph-scale semantic relations as repeatable byte-backed streams. The canonical contract, digest and independent validation are unchanged. | Clustering/model construction, the instance-name map and remaining native provider input construction, direct native binary emission, and the C++ capture vector still scale with the graph. RePart/MFSPart initial candidates can still materialize a transient contract before their final rebuild. |
| W5 physical exporters | **Partial** | One typed physical handoff replaced production `packed-sites.json` and `placement.json`. The production bridge appends normalized clusters directly to a private SQLite artifact; legalization retains only MUX-bearing slices and independent topology validation retains only required MUX/CARRY records. Assignment/site/BEL ownership uses unique constraints, counts, a single ordered scan and bounded per-site state. Source-certificate ownership and full mapped-output driver uniqueness use temporary disk-backed relations rather than million-entry Python maps. Publication occurs only after all checks pass. Lazy typed views feed RWRoute and routed timing; mapped-index v2 owns typed cell pins and RWRoute streams route-cell, physical-binding, endpoint, deterministic safe-name ordinal and clock-candidate rows. | The mapped atomic-source view, electrical-binding map, clock-gate qualification subset, parameter lookups, boundary-endpoint JSON and remaining exporter loop still scale with design data. DeviceRegionIndex and pin-aware SLR planning are separate semantic/QoR work. |
| W6 global STA stitching | **Started** | OpenSTA remains the numerical engine, indexed timing readers exist, and Phase 7C no longer expands the whole ScheduleStore before binding. Measurement TSV import validates endpoint order and arc equations in one pass. Production event/check construction now appends interned strings and fixed-width columns to `_IndexedEventChecks`, validates target/runtime and TX/commit coverage online, and creates representative indices without a second complete compression scan. Original-path metadata is a sorted, interned, fixed-width mapping instead of one nested dictionary per path. Canonical report stitching consumes one path bundle at a time. Representative membership uses a fixed-width integer map and exposes original-check measurements through a repeatable lazy sequence, so stitching and TSV publication no longer allocate object/arc lists or a second dictionary list for all expanded checks. Interleaved legacy input remains a checked compatibility fallback and WNS/TNS still count every original path once. | Each per-FPGA evidence file is still decoded as JSON. String interning, path IDs and representative identity tables remain linear in unique checks/paths, and the legacy interleaved fallback still requires graph-sized Python work. |
| W7 native production entry | **Not implemented** | Individual native kernels and the first shared core library exist. | There is no complete `emuflow-native compile` path and Python production memory can still scale with routes, endpoints, and timing paths. |

Representation-only waves must preserve the effective provider, seed,
constraints, ordering, and external tool versions. DeviceRegionIndex,
pin-aware SLR cost, and other placement-policy changes are qualified in
separate commits and A/B runs so storage migration cannot hide a QoR change.
The completion gate remains a fresh, isolated NVDLA Phase 1--7/7C run with
stage wall time/RSS/I/O, zero unrouted nets and DRC violations, complete
original-path coverage, and global OpenSTA WNS/TNS. That terminal gate is
still pending.

## Scope

The first migration tranche covers the production Phase 3 path from compact
EmuIR loading through clustering, provider input construction, Static Exact
semantic generation, validation and publication. Provider search, imported
solutions and frozen-output replay are measured separately so search time is
not attributed to Python orchestration.

## Evidence contract

Every qualification run publishes `phase3-runtime.json`. Its nested records
separate input loading, cluster construction, TritonPart/PATRON work, Static
Exact semantic construction, final validation, packing, JSON writes and
assignment-index publication. Parent records include their children and are
not summed. Comparisons use the same source inputs, platform, constraints,
provider version, imported TritonPart solution and frozen PATRON output.

The migration is accepted only when all of the following hold:

1. The final Phase 3 validation status and all resource, cut, dependency and
   capture populations agree.
2. The Static Exact v3 semantic contract is byte-canonical and has the same
   SHA-256 identity on reference fixtures.
3. The indexed terminal validator proves the complete instance relation,
   resource loads and cut source/sink relation against compact EmuIR.
4. Phase 4 through Phase 7 consumers return the same logical segment and
   capture records through the assignment-index API.
5. Peak RSS, artifact sizes and wall time are recorded for both runs; an
   optimization is not accepted from a synthetic microbenchmark alone.

## Migration boundaries

The current first boundary moves dependency-cone and capture-cone traversal to
`emuflow_static_exact_analyzer` for designs with at least 100,000 instances.
Python writes a versioned integer-only binary graph, validates the native
result, and materializes the unchanged semantic contract. Small designs use
the original Python traversal as a continuously tested reference.

Assignment index v2 replaces per-row JSON copies with typed segment and capture
columns plus compact integer enums. Version 1 remains readable. Assignment
storage v3 removes the duplicate compressed semantic graph from
`assignment.json`; the JSON document carries a schema-and-digest descriptor,
while the typed index is the canonical persisted payload. Full-document reads
reconstruct the historical logical contract from the index and reject any
digest mismatch. Production Phase 4--7 readers continue to request bounded
indexed views.

The final assignment is the authoritative owner of the canonical Static Exact
digest. Production assignment-index publication passes that already validated
identity into the binary writer rather than iterating the complete resident
contract a second time. The explicit migration path for detached historical
artifacts continues to recompute the digest because it does not share the
producer's validated in-process object.

The next Phase 3 data boundary will emit native semantic records directly into
the binary sections. Capture protocol v3 already avoids a token/string
round-trip: native input carries original EmuIR ordinals and output identifies
only the requested cut, partition, net and sink ordinals. Python performs
O(capture-count) random dereference and independent semantic checks; it does
not rebuild ordered instance/net copies or replay all endpoints. Stable segment
numbering is derived from ordered counters rather than duplicate graph-sized
lookup maps. Cut construction also forwards its already-known net ordinal
column to the analyzer, which checks every `(name, ordinal)` pair against EmuIR
and avoids rebuilding cut-name lookup, matched-name and coordinate maps. The
name-only compatibility entry remains available for old callers. The current
implementation transfers predecessor lists directly
into a byte-backed cut-node sequence, while dependency edges, logic segments
and capture records are repeatable typed streams. Exact-binary v2 bounds
capture bits to 8192 bits, uses canonical unsigned byte encoding, and validates
section sizes before allocation. Independent validators still replay these
streams and compare the canonical semantic digest. NVDLA qualification must
measure the remaining decode CPU and analyzer-side maps before direct emission
is accepted.

## NVDLA baseline

The reference run
`nvdla-phase3-runtime-f4e62aad-v1` uses commit `f4e62aad`, the imported
four-way TritonPart solution and the frozen PATRON v14 replay. It completed in
7,828.99 seconds (2:10:29 measured inside Phase 3; 2:10:32 by `/usr/bin/time`)
with a 49.73 GiB peak process-tree RSS. Selected records are below. Parent
records include their named children and must not be added to those children.

| Step | Wall time | Share of total |
| --- | ---: | ---: |
| Cluster construction | 841.82 s | 10.75% |
| TritonPart initialization | 662.57 s | 8.46% |
| PATRON pressure-model construction | 335.00 s | 4.28% |
| PATRON native refinement | 2,350.46 s | 30.02% |
| `patron.build_candidate_assignment` child | 2,259.03 s | 28.85% |
| Final PATRON and online validations | 245.15 s | 3.13% |
| Cluster packing and write | 143.97 s | 1.84% |
| Assignment packing and JSON write | 1,428.76 s | 18.25% |
| Assignment-index publication | 1,734.99 s | 22.16% |
| All publication steps | 3,307.79 s | 42.25% |

The baseline assignment JSON is 1,078,736,794 bytes and its v1 index is
16,276,533,248 bytes. These measurements show two independent hot regions:
Static Exact construction is inside `patron.build_candidate_assignment`, and
the duplicate JSON/index publication path consumes another 55.13 minutes.

## NVDLA qualification result

The candidate run `nvdla-phase3-runtime-c639b12b-v1` uses the same EmuIR,
platform, timing inputs, imported four-way TritonPart solution and frozen
PATRON v14 output as the baseline. It passed in 5,877.25 seconds (1:37:57
inside Phase 3; 1:37:59 by `/usr/bin/time`). The final report is identical to
the baseline after excluding the expected storage-schema descriptor.

| Metric | Baseline | Candidate | Change |
| --- | ---: | ---: | ---: |
| Phase 3 wall time | 7,828.99 s | 5,877.25 s | -24.93% |
| Peak process-tree RSS | 49.73 GiB | 42.69 GiB | -14.16% |
| Parent bytes read | 9,243,000,832 | 5,771,280,384 | -37.56% |
| Parent bytes written | 19,443,142,656 | 15,418,499,072 | -20.70% |
| `assignment.json` | 1,078,736,794 B | 613,148,446 B | -43.16% |
| `assignment.sqlite` | 16,276,533,248 B | 12,677,718,016 B | -22.11% |
| Candidate-assignment construction | 2,259.03 s | 2,113.69 s | -6.43% |
| Assignment packing | 1,407.53 s | 13.71 s | -99.03% |
| Assignment-index publication | 1,734.99 s | 1,296.98 s | -25.25% |
| All publication steps | 3,307.79 s | 1,458.63 s | -55.90% |

Both indexes carry semantic SHA-256
`45555fdcb6c612e311bbc181ee48ed81f123ebf1306fda1af3eb9f2c6d7c12a5`.
Their complete indexed populations also agree: 3,941,369 instance copies,
496,941 cut sinks, 473,287 exact cut nodes, 5,830,661 cut-source records,
17,422,850 logic segments, 17,779,692 segment bindings and 11,949,031 capture
requirements. The unchanged digest, row counts and report prove that the
storage reduction did not weaken Static Exact semantics or validation.

The remaining 613 MB JSON and 11.81 GiB index were implementation costs, not
logical requirements. Assignment-index v3 now keeps only the bounded control
relations (`parts`, `copies`, and `cut_sinks`) in SQLite. Static Exact nodes,
segments and captures use the versioned `assignment.exact.bin` data plane with
integer identities, per-FPGA sections, section SHA-256 seals and one descriptor
sealed by the SQLite index. Phase 6 transfers this artifact by hard link when
the filesystem permits it; split and placement consumers read the required
sections directly instead of rebuilding the same B-trees.

A 20,000-cut-node synthetic population with the NVDLA relation proportions
(240,000 source segments, 720,000 logic segments and 480,000 captures) occupies
30,142,145 bytes in the binary data plane versus 134,692,864 bytes in the v2
exact-relation SQLite tables, a 77.62% reduction. Binary publication took
1.657 seconds and the independent full scan took 1.913 seconds on the
development host; legacy SQLite insertion alone took 1.898 seconds. This is a
format benchmark, not a replacement for the required full NVDLA rerun.

For the cut-coordinate preparation substep, a 300,000-record synthetic check
measured 19.66 MiB of incremental Python allocation and 0.149 seconds for the
name-only compatibility lookup, versus no measurable incremental allocation
and 0.110 seconds when validating the ordinal column already owned by cut
construction. This isolates the removed hash containers; it is not an NVDLA
stage-level performance claim.

The next Phase 3 tranche remains direct native emission into the binary
sections so the final Python semantic-contract tree no longer has to exist
before publication. Sparse capture replay and counter-derived segment IDs have
removed auxiliary graph-sized populations, but do not yet remove that final
tree.

### Digest-reuse NVDLA A/B

A later same-input NVDLA Phase 3 replay compared `8d802686` with `2040572a`.
Both used the same prepared EmuIR and timing inputs, imported four-way
TritonPart solution, frozen PATRON v14 replay, platform, constraints, and
native executable hashes. They ran on different otherwise-idle compute nodes,
so this is a stage-level qualification rather than a substitute for the final
same-host Phase 1--7/7C gate.

| Metric | `8d802686` | `2040572a` | Change |
| --- | ---: | ---: | ---: |
| Phase 3 wall time | 5,719.81 s | 4,999.22 s | -12.60% |
| Parent CPU time | 5,697.84 s | 4,974.99 s | -12.69% |
| Peak process-tree RSS | 44,772,444 KiB | 44,773,952 KiB | +0.003% |
| Assignment-index publication | 1,086.81 s | 361.53 s | -66.73% |
| Parent bytes read | 6,462,756,864 | 8,850,466,816 | +36.95% |
| Parent bytes written | 6,517,743,616 | 6,517,723,136 | unchanged |

The candidate index publication spent 233.93 seconds writing the exact binary,
102.30 seconds writing primary copies, 3.89 seconds writing cut sinks,
0.42 seconds committing, and 15.21 seconds in the independent indexed
validator. Its semantic digest, binary manifest digest, row counts, validation
metrics, artifact sizes, and byte hashes for `clusters.json`, `assignment.json`
and `phase3_report.json` all match the baseline. The unchanged 42.70 GiB peak
shows that removing the repeated digest traversal saves CPU and wall time but
does not solve the resident Python semantic-contract tree; direct native
emission remains the Phase 3 memory priority. The higher read count is retained
as measured node/page-cache variance and is not claimed as an I/O improvement.

## Phase 1--7 repeated-materialization audit

The Phase 3 failure mode was defined as a graph-sized population being
expanded, sorted, serialized, indexed, reread, or revalidated more than once
without adding a new semantic guarantee. The production flow was audited with
that definition rather than treating every stage boundary as removable.

| Phase | Finding | Current action |
| --- | --- | --- |
| 1 | The current frontend writes compact EmuIR SQLite and computes reports with indexed scans. The JSON report and normalized BoardDB are bounded control data. | No graph-sized duplicate found. |
| 2 | TimingPathDB uses a compact JSON manifest plus a sealed streaming payload/index. Selected-path readers query only requested paths. | No duplicate path population found in the current indexed producer. The older standalone placement `phase2.py` remains a compatibility flow outside the canonical multi-FPGA Phase 2 timing path. |
| 3 | Assignment JSON, SQLite, and exact relations previously repeated the same graph. | Assignment-index v3 and `assignment.exact.bin` are the implemented control/data split. Native predecessor CSR and fixed-width launch/level/depth arrays replace four analyzer-side Python maps. Capture protocol v3 returns fixed-width original net/sink ordinals and Python dereferences only the capture population, eliminating ordered EmuIR copies, the graph-sized net map and full endpoint replay. Cut construction carries authoritative net ordinals into the native analyzer, where each pair is rechecked; this removes the production cut-name lookup, matched-name set and coordinate map while preserving name-only compatibility. Exact-binary v2 provides bounded canonical capture-bit storage with v1 read compatibility. Production PATRON derives all four semantic relations as repeatable byte-backed streams. The instance-name input map, native input/capture vectors and direct native emission remain pending. |
| 4 | The former `routes.json` repeated demands and routed records inside one graph-sized JSON document, forcing Phase 5 to parse and retain it before scheduling. | `routes.json` is now a small selector for a content-addressed typed RouteStore. Demand records are projected from their authoritative routes; sinks, edges, predecessors and timing paths are normalized once. Phase 4 and the final flow report inventory the sidecar, and legacy `read_json` remains an explicit materializer. |
| 5 | The former canonical `schedule.json` serialized every routed hop and managed mode only wrapped that complete JSON in zlib/base64. Phase 5 also sorted and indexed the complete Phase 4 route dictionary population. | RouteStore ordered cursors feed native Static Exact input, SQL supplies stable net/demand ranks and cross-layer hop order, and Phase 5 retains compact route columns plus lazy schedule-route projections. ScheduleStore v4 stores hops, routes, route sinks/edges, completions and arrivals as typed relations and keeps only domain-scale JSON records. Both logical seals and legacy readers remain supported. Standalone independent exact validation still has route-level maps, so NVDLA I/O qualification is pending. |
| 6 | Streaming split metadata formerly rebuilt every hop as endpoint/lane/binding dictionaries and repeated the population in several JSON artifacts. | Endpoint, lane, anchor, board-binding and adapter-port relations are stored once in sealed `transport.sqlite`. The bounded `binding_physical_ports` relation lets physical-I/O contract v2 emit one record per real pair with a member count/seal. Its reader replays one ordered authoritative binding cursor to verify those memberships without serializing them into the contract. Pin plans, position hints and Chimew electrical groups/members use compact selectors plus sealed indexed stores; the Chimew adapter consumes ScheduleStore through a file-backed member join and streams all three outputs without hop-sized schedule, coverage or output containers. Phase 7 performs indexed binding lookup, and reports inventory every sidecar required for relocation. The upstream Chimew bank/group document remains graph-sized. |
| 7 | Transport synthesis previously wrote a full Yosys JSON, imported it into another full Python `EmuIR`, and loaded the graph again. The OpenPARF bridge also built and reread separate packing/placement trees and validators rebuilt multiple owner maps. | Generic Yosys pipes directly to indexed EmuIR and lowering copies records set-wise. The production bridge writes one private typed handoff incrementally, validates ownership and packing/placement identity with SQLite constraints, ordered scans and bounded MUX/CARRY state, then atomically publishes. Source ownership and full output-bit driver uniqueness use temporary disk relations. RWRoute and timing consume lazy views; compatibility mode alone exports the historical large JSON contracts. |

Cross-layer and global timing paths were also checked. Their current indexed
readers stream complete payloads or selected identities. Physical timing
evidence remains in one authoritative per-FPGA file and the whole-design
summary stores only a sealed reference, so that population is no longer copied
into a second JSON tree. Independent terminal validation is retained where it
proves a different boundary; redundant replay inside the same producer process
is removed. Moving the per-FPGA boundary/path formats and event-check stitching
to indexed/native stores remains W2/W6 work.

## Priority follow-on: OpenPARF to RapidWright handoff

The same repeated-materialization pattern exists in the Phase 7 physical hot
path and is the next priority after direct Phase 3 index streaming. The current
implementation already has useful foundations:

- `partition.mapped.sqlite` is the sole production mapped-netlist data plane.
- The OpenPARF placement certificate stores assignment rows in a normalized
  SQLite sidecar and keeps only a sealed descriptor in JSON.
- RWRoute TSV output is streamed to a temporary file, flushed, synchronized
  and atomically renamed.

The first bridge tranches are implemented. Certificate rows stream from
SQLite, the indexed atomic-source view remains in process, and one normalized,
versioned physical-handoff database replaces the packed and placement JSON
publications in the production native path. The producer appends clusters to a
private same-directory SQLite artifact. Legalization retains only MUX-bearing
slices; an independent streaming validator keeps the required MUX/CARRY
topology and uses uniqueness,
counts, ordered assignment scans and bounded per-site state; source ownership
is compared with temporary disk relations. It no longer invokes the generic
packing/placement validators that rebuild full owner and cluster maps. All
checks finish before atomic replacement. RWRoute export and routed timing then
use relocatable lazy views; compatibility JSON alone expands them explicitly.
RWRoute TSV export now generates route-cell rows from the physical binding
instead of retaining a second whole-design route-cell dictionary; its report
exposes per-substep timing. Mapped-index v2 owns cell
pins as one typed ordered relation and reconstructs historical cell mappings
lazily. RWRoute streams that relation by net and materializes only pins needed
to qualify clock gates, removing its whole-design Python endpoint index. V1
indexes remain readable. The production exporter obtains mapped cell types,
site/BEL bindings, and net-ordered pins from read-only joins between
mapped-index v2 and the typed physical handoff, so it no longer builds the
former million-entry physical-binding dictionary. That ordered join also
computes each route-cell ordinal, including the two-cell LUT6_2 expansion, so
the production exporter formats textual `cN` names as rows are emitted without
retaining a million-entry safe-name map. Remaining design-scale costs include
the mapped atomic-source view and selected exporter relations. Clock-gate
discovery now queries only typed `LUT2` records and their pins rather than
reconstructing every mapped cell;
candidate qualification, hard-block parameter access, and the final TSV loop
remain Python work.

The migration boundary is the OpenPARF placement certificate through RWRoute
launch. Work proceeds in this order:

1. The bridge report now records certificate-validation and
   publication/validation wall time, row counts, artifact bytes, and whether
   streaming/in-process paths were used. Extend this with process-tree peak RSS,
   RWRoute export, and RapidWright startup measurements during qualification on
   a partition with at least one million instances.
2. **Implemented:** define one typed, versioned physical-handoff index
   containing clusters,
   instance-to-site/BEL assignments, cascades/macros, constants, source
   identities and validation summaries. JSON retains only compact summaries
   and sealed references. Legacy JSON expansion remains an explicit
   compatibility operation outside the production hot path.
3. **Implemented for the production OpenPARF bridge:** make packing, placement
   and resource/SLR checks operate over bounded index
   scans with database uniqueness, foreign-key and occupancy constraints.
   Keep an independent read-only validator; do not replace validation with
   trust in the writer.
4. Nativeize the remaining bridge/export loop so it consumes the mapped-netlist
   and
   placement indexes and emits the RapidWright input once. Prefer direct
   certificate consumption by the RapidWright adapter when the pinned Java
   runtime can support it; otherwise emit one streaming typed or TSV contract
   without intermediate packed and placement JSON. The current Python exporter
   already consumes the shared SQLite views and emits no intermediate physical
   JSON; moving its whole-design maps into native/Java code remains pending.
5. Publish the index, compact manifest and RapidWright input through temporary
   files plus atomic replacement. Launch RapidWright only after source seals,
   row counts, unique placement, site/BEL occupancy, macro/cascade legality,
   resource limits and SLR constraints all pass.

Acceptance requires identical placed-instance ownership, site/BEL bindings,
macro and cascade populations, resource/SLR summaries, source digests,
RapidWright route certificate and downstream timing results. The optimization
is measured against the existing handoff using identical OpenPARF output; a
smaller artifact alone is not evidence of equivalent behavior.

The current cross-wave regression covers the Phase 3 capture protocol,
ScheduleStore v4 consumers, Phase 6 binding/adapter contracts, typed physical
handoff and Global STA stitching together: 569 tests passed, 5 optional-tool
tests skipped and 2 unavailable external-fixture cases were explicitly deselected.
Randomized review additionally compared 300 capture populations,
200 STA expansion/stitching cases and 20 shuffled physical-certificate orders.
These results establish local contract compatibility; they do not replace the
pending isolated NVDLA Static Exact Phase 1--7/7C qualification.

### Parallel execution model

Independent FPGA implementations may run concurrently and may be at different
internal stages. The current physical-flow executor already supports this
shape: `--physical-workers N` runs each FPGA's complete partition task in its
own worker, so one FPGA can be routing while another is placing. The default is
one worker. Selected-FPGA batches can also run on separate HPC nodes and be
assembled only after every partition record passes.

Parallelism remains constrained by real data dependencies:

- Phase 3 partitioning and the global Phase 4--5 route/schedule contracts are
  common barriers because they define cross-FPGA cuts and shared-link use.
- After a sealed per-FPGA split input exists, mapping, placement and routing for
  different FPGAs are independent. Within one FPGA, mapping precedes placement,
  placement legality precedes routing, and routing precedes timing.
- Whole-design assembly, boundary checking and global timing wait for all
  active FPGA records. Reports and legacy compatibility exports that are not
  consumed by production stages should run asynchronously after their source
  seal is available.

A fixed thread count is insufficient for large partitions. The scheduler must
admit work using memory, CPU and I/O budgets and prefer one large physical job
per HPC node when a partition approaches the node memory limit. It should
record ready, start, finish and blocked-on-contract timestamps for each
per-FPGA stage. Qualification compares both total work and end-to-end makespan,
and repeats concurrent runs to prove deterministic digests and the absence of
shared-directory races.
