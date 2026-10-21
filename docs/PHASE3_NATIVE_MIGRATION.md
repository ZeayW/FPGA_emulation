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
| W1 Phase 5 exact schedule | **Partial** | For indexed Static Exact designs with at least 100,000 routed cuts, `emuflow_tdm_exact_scheduler` performs dependency ordering, readiness propagation, lane/slot occupancy, and completion calculation in C++. Phase 5 publishes a compact manifest plus a versioned, sealed SQLite ScheduleStore; Phase 6, physical orchestration, logic-timing preparation and Phase 7C consume it lazily. Small designs retain the Python implementation as a differential reference. | Native output still becomes hop-sized Python records before ScheduleStore publication, and Phase 6 still decodes each record while streaming its own store. Certificate publication/validation and global-check consumers must move to native cursors before Python CPU cost stops scaling with hops. |
| W2 Phase 7 lowering and transport import | **Partial** | Indexed DUT lowering and placement stores avoid the former whole-DUT JSON path. Generic Yosys now streams synthesized transport JSON directly into compact EmuIR SQLite, so `transport-synthesized.json` is never materialized. Lowering opens the store as a bounded view and the placement writer uses SQLite `ATTACH` to copy, namespace and filter transport cells/nets/endpoints without reconstructing the transport graph. RX signal/placeholder coverage uses ordered store cursors. Whole-design physical assembly publishes sealed per-FPGA evidence references instead of embedding all boundary/path databases again in `physical-summary.json`; publish, parse and seal share one read pass. | Interface-net overlay records and boundary identity production still scale in Python with transport endpoints. Per-FPGA evidence remains JSON. Move those joins and certificate publication to native/indexed cursors, then qualify the full per-FPGA path for RSS and I/O. |
| W3 Phase 6 split and binding | **Partial** | DUT ownership/split uses SQLite joins. Phase 6 now writes endpoint, lane, virtual-anchor, and virtual-board binding relations once to a sealed `transport.sqlite`; the public JSON paths are compact relocatable manifests. Transport RTL streams atomically from slot-ordered cursors, while validation, XDC/adapter generation, physical orchestration, and compatibility reads use lazy selectors. | Record construction is still Python, pin-plan input can still be hop-sized, and remaining adapter strings are assembled in memory. Native producers and validators are still required before Python memory is fully independent of hop count. |
| W4 Phase 3 model and publication | **Partial** | Large dependency/capture traversal is native; assignment index v3 separates bounded SQLite control data from typed binary Static Exact data. Production index publication reuses the final assignment's canonical Static Exact digest and no longer performs a second full-contract hash traversal. | Clustering/model construction, native provider input construction, direct native binary emission, and final publication/validation still cross graph-sized Python structures. |
| W5 physical exporters | **Partial** | One typed physical handoff replaced production `packed-sites.json` and `placement.json`; lazy views feed validation, RWRoute export, and routed timing. | OpenPARF/RWRoute exporter dictionaries and the remaining bridge loop must move into native/Java consumers. DeviceRegionIndex and pin-aware SLR planning are separate semantic/QoR work. |
| W6 global STA stitching | **Started** | OpenSTA remains the numerical engine, indexed timing readers exist, and Phase 7C no longer expands the whole ScheduleStore before binding. Boundary, logic-segment and local-path evidence is resolved one FPGA at a time from sealed references; canonical summary hashing is streaming and preserves the former logical digest. | Each per-FPGA evidence file is still decoded as JSON. Event/check construction still builds hop/path dictionaries; member expansion, result import, and original-path stitching remain graph-sized Python work. |
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

The next Phase 3 data boundary will stream native semantic records directly
into the index. The current implementation still materializes the final
logical contract once in Python so the independent validators can compare it
before publication. The NVDLA qualification below now permits that work to
proceed, but the streaming writer must retain an independent indexed validator
and the canonical semantic digest.

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

The next Phase 3 tranche is direct native emission into the binary sections so
the full Python semantic contract no longer has to exist before publication.

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
| 3 | Assignment JSON, SQLite, and exact relations previously repeated the same graph. | Assignment-index v3 and `assignment.exact.bin` are the implemented control/data split. Direct native emission is still pending. |
| 4 | `routes.json` is the canonical routed-demand population; it references rather than embeds the Static Exact graph. A candidate pool is an optional qualification diagnostic and is suppressed in compact managed runs. | No mandatory duplicate graph found. |
| 5 | The former canonical `schedule.json` serialized every routed hop and managed mode only wrapped that complete JSON in zlib/base64. The optional human-readable `schedule.tsv` formerly built another hop-sized string in memory. | `schedule.json` is now a compact relocatable manifest for the sealed `schedule.sqlite` ScheduleStore. Ordered relations are row-streamed, identity constrained and independently hashed; Phase 6, physical orchestration and Phase 7C use lazy cursors. Store v2 carries compact and historical indented logical seals, so provenance checks do not force expansion; v1 remains readable. Explicit compatibility reads reconstruct the logical document. TSV publication streams to an atomic file. |
| 6 | Streaming split metadata still loaded all exact relations, repeated segment-ID arrays in every endpoint, then rebuilt every hop as endpoint/lane/binding dictionaries and wrote the same population to multiple JSON files. `SplitNetlistView` also expanded exact relations for bounded queries. | Endpoints carry only boundary identity, role, digest, and one shared Static Exact descriptor. Endpoint, lane, anchor, and board-binding relations are batch-streamed once into sealed `transport.sqlite`; `transport.json`, `lane_map.json`, `virtual_anchors.json`, and `virtual_board_binding.json` are selector manifests. Their legacy logical documents remain explicit compatibility reads. Phase 7 lowering consumes indexed RX signal and placeholder cursors. |
| 7 | Transport synthesis previously wrote a full Yosys JSON, imported it into another full Python `EmuIR`, saved SQLite, then loaded the same graph again for lowering. The OpenPARF bridge also built and reread separate packing and placement trees. Whole-design physical assembly then embedded every per-FPGA boundary and path evidence database in another JSON document. | Generic Yosys now pipes directly to indexed EmuIR, and placement lowering copies the synthesized graph set-wise between attached SQLite stores. The production physical bridge publishes one typed `physical-handoff.sqlite`; packing, placement, RWRoute export and routed timing consume lazy views. `physical-summary.json` contains sealed relocatable per-FPGA evidence references, and downstream consumers resolve one FPGA at a time. Explicit compatibility mode can still export the standard packing/placement JSON contracts. |

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

The first two bridge tranches are implemented. Certificate rows stream from
SQLite, the indexed atomic-source view remains in process, and one normalized,
versioned physical-handoff database replaces the packed and placement JSON
publications in the production native path. Independent validators, RWRoute
export and routed timing use relocatable lazy views over that file. The bridge
also keeps a site-pin legalizer's FF BEL moves synchronized between its packing
and placement views. The validator's full-file SHA-256 is reused when those
views open, and the ordered production writer no longer builds a second
cluster lookup table. The remaining million-instance cost is the bridge's
temporary Python packing/placement trees and RWRoute's whole-design export
dictionaries.

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
