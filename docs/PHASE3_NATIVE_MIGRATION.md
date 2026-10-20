# Phase 3 native migration

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

The remaining 613 MB JSON and 11.81 GiB index are still implementation costs,
not logical requirements. The next Phase 3 tranche should therefore eliminate
the remaining full Python contract materialization, write typed native records
directly, and evaluate denser columnar or binary storage for relations whose
SQLite B-tree overhead dominates their payload.

## Priority follow-on: OpenPARF to RapidWright handoff

The same repeated-materialization pattern exists in the Phase 7 physical hot
path and is the next priority after direct Phase 3 index streaming. The current
implementation already has useful foundations:

- `partition.mapped.sqlite` is the sole production mapped-netlist data plane.
- The OpenPARF placement certificate stores assignment rows in a normalized
  SQLite sidecar and keeps only a sealed descriptor in JSON.
- RWRoute TSV output is streamed to a temporary file, flushed, synchronized
  and atomically renamed.

The remaining bridge still defeats much of that work. It loads all placement
certificate rows into Python lists, constructs complete packed and placement
trees, writes `packed-sites.json` and `placement.json`, reads both files back,
repeats packing and placement validation, constructs whole-design physical and
endpoint dictionaries, and finally writes `rwroute.tsv`. On million-instance
partitions this repeats the same ownership, site, BEL, macro and resource data
across memory, JSON, SQLite and TSV.

The migration boundary is the OpenPARF placement certificate through RWRoute
launch. Work proceeds in this order:

1. Add per-partition runtime, peak-RSS, byte and row-count measurements around
   certificate loading, bridge materialization, packing validation, placement
   validation, RWRoute export and RapidWright startup. Qualification includes
   a partition with at least one million instances.
2. Define one typed, versioned physical-handoff index containing clusters,
   instance-to-site/BEL assignments, cascades/macros, constants, source
   identities and validation summaries. JSON retains only compact summaries
   and sealed references. Legacy JSON expansion remains an explicit
   compatibility operation outside the production hot path.
3. Make packing, placement and resource/SLR checks operate over bounded index
   scans with database uniqueness, foreign-key and occupancy constraints.
   Keep an independent read-only validator; do not replace validation with
   trust in the writer.
4. Nativeize the bridge/export loop so it consumes the mapped-netlist and
   placement indexes and emits the RapidWright input once. Prefer direct
   certificate consumption by the RapidWright adapter when the pinned Java
   runtime can support it; otherwise emit one streaming typed or TSV contract
   without intermediate packed and placement JSON.
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
