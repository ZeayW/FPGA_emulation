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
| W0 shared core and stores | **Started** | `libemuflow_core` now owns bounded streaming native artifact reads, synchronized atomic binary publication, representation-scoped typed IDs, and the common magic/version envelope check. The shared reader no longer copies a complete native input into memory. The Static Exact analyzer and exact scheduler use that shared boundary. | Move the remaining native tools onto the shared storage/error/metrics interfaces; extend the stage-neutral schema catalog and add SQLite RAII views as consumers require them. |
| W1 Phase 5 exact schedule | **Partial** | Phase 4 now publishes a selector plus sealed typed RouteStore instead of a graph-sized `routes.json`; demands are projected from authoritative route rows, while route sinks/edges, exact predecessors and timing paths are normalized relations. Phase 5 consumes ordered route and hop cursors directly. For indexed Static Exact designs with at least 100,000 routed cuts, `emuflow_tdm_exact_scheduler` performs dependency ordering, readiness propagation, lane/slot occupancy, and completion calculation in C++. Its adapter derives integer ranks in SQL and retains compact columns instead of Python route dictionaries and net/demand maps. Fixed hop and variable completion records remain in memory-mapped lazy views; completion ordering and offsets use fixed-width arrays, while certificate sink arrivals use a packed net-rank by FPGA-rank matrix. Phase 5 publishes ScheduleStore v4 with typed hops, routes, route sinks/edges, completions and sink arrivals. Both logical seals remain compatible, legacy route JSON and ScheduleStore v1--v3 readers remain available, and stores use selector-last content-addressed publication. RouteStore validates and can restore the previous generation, binds the artifact filename to its digest, validates the new view before old-sidecar cleanup, omits unjustified append-only `VACUUM`, and joins every generic archive selector/sidecar pair into one retention group. The ScheduleStore, selector, cross-layer contract, optional ratio plan, standalone derivatives and report are staged as one generation. The report records a canonical self-identity and the name, size and SHA-256 of every member. Publication fsyncs all members and pre-report directory entries before `phase5_report.json` commits the generation; caught failures restore the prior bytes, while uncaught termination leaves a mixed generation that every direct validator, Phase 7C and canonical/shared consumer rejects before schedule/contract access. Report-less legacy data requires an explicit compatibility option. Production Phase 6 dispatch reads only the typed entry count; Phase 7C and physical/local timing readers keep RouteStore lazy. Small designs retain the Python scheduling reference. Historical canonical readers inventory the complete route/TDM output directories; shared replay dynamically resolves and copies both content-addressed sidecars before their selectors and rejects dangling generations. Managed roots use bounded strict-envelope, basename and size checks because the root checkpoint already sealed the bytes and binds the Phase 5 report/member table; standalone replay retains full hashing and semantic validation. | Independent typed validation now reconstructs exact ready order with packed CSR/rank arrays, uses a compact arrival matrix, streams certificate digests and compares completion rows through a bounded temporary SQLite relation instead of route-level Python maps. The native adapter retains R-scale compact columns, the ratio-delay baseline branch performs H-scale work when enabled, and completion identity columns remain route-scale, while completion payloads are lazy and sink arrivals use fixed-width packed storage. A synthetic 100,000-route/two-sink comparison reduced retained traced completion/arrival allocation from 65.70 MiB to 2.67 MiB of packed arrays plus a 3.05 MiB native record file; a complete lazy traversal added 0.014 MiB traced peak. This is adapter evidence, not NVDLA qualification. At 100,000 routes, independent-root validation used 13.88 MiB versus 316.65 MiB peak (4.55 versus 6.94 seconds); a 99,999-edge chain used 5.51 MiB versus 338.55 MiB (5.25 versus 6.33 seconds). Cross-layer contract v2 now merges ordered RouteStore/ScheduleStore hop cursors, binds their digests and checked count, and omits the former H-scale `hop_bindings`; materialized v1 artifacts remain validatable. A synthetic 100,000-hop typed-store comparison reduced retained Python allocation from 38.69 MiB to 0.17 MiB, peak allocation from 82.58 MiB to 44.06 MiB, and contract JSON from 6.87 MiB to 0.00063 MiB at 0.746 versus 0.730 seconds. This is adapter evidence, not NVDLA qualification. Terminal full-bundle replay still materializes compatibility views in some paths. A synthetic 100,000-route one-hop build reduced Python peak allocation 346.1 MB to 108.2 MB and route bytes 46.60 MB to 32.19 MB; two repeated builds were 8.16--8.32 seconds versus 8.92--9.59 seconds. This is adapter evidence only. NVDLA wall-time, RSS and temporary-I/O qualification remain required. |
| W2 Phase 7 lowering and transport import | **Partial** | Indexed DUT lowering and placement stores avoid the former whole-DUT JSON path. Generic Yosys now streams synthesized transport JSON directly into compact EmuIR SQLite, so `transport-synthesized.json` is never materialized. Lowering opens the store as a bounded view and the placement writer uses SQLite `ATTACH` to copy, namespace and filter transport cells/nets/endpoints without reconstructing the transport graph. RX signal/placeholder coverage uses ordered store cursors. Boundary identity production now streams endpoint records into a content-addressed typed owner and publishes only a bounded selector; independent identity and timing validation merge ordered endpoint cursors instead of building expected/actual dictionaries, and timing construction no longer rebuilds an identity map. The physical summary relocates the selector and immutable generation without expansion. Whole-design physical assembly publishes sealed per-FPGA evidence references instead of embedding all boundary/path databases again in `physical-summary.json`. A 100,000-endpoint producer comparison reduced process peak RSS from 179,355,648 to 53,002,240 bytes and artifact bytes from 42,855,750 to 29,086,441; fsync-inclusive wall time changed from 1.008 to 1.546 seconds. A separate 100,000-record validator comparison reduced process peak RSS from 82,395,136 to 17,645,568 bytes while wall time changed from 0.104 to 0.201 seconds. Boundary timing producers now publish a bounded selector plus a content-addressed typed SQLite owner; RapidWright timing generation streams directly into the writer, the physical summary relocates the complete selector/store pair, and readers validate each FPGA lazily. Local-path timing now uses the same selector-backed typed-owner model for VPR, RapidWright, inactive partitions, physical-summary relocation and lazy consumers. A 100,000-record local-path read and full seal validation reduced process peak RSS from 251,887,616 to 32,653,312 bytes and artifact bytes from 30 MiB to 24 MiB, while wall time changed from 0.73 to 0.90 seconds. | Interface-net overlay records and backend measurement/query builders still retain endpoint-scale maps. Move the remaining query/import joins to native/indexed cursors, then qualify the full per-FPGA path for RSS and I/O on NVDLA. |
| W3 Phase 6 split and binding | **Partial** | DUT ownership/split uses SQLite joins. Phase 6 writes endpoint, lane, virtual-anchor, virtual-board binding and differential adapter-port relations once to sealed `transport.sqlite`. A bounded `binding_physical_ports` relation records one row per real differential pair, its member count and ordered member seal. Indexed physical-I/O contract v2 independently recomputes those memberships from one ordered authoritative binding-entry cursor, then merges them with the compact relation and adapter ports; it does not serialize every hop for each FPGA. Legacy v1 remains readable. Typed pin-plan and position-hint stores own hop-sized rows behind small selectors, validators use ordered merges/indexed joins, and Transport RTL plus XDC/SystemVerilog stream atomically. The production Chimew builder reads ScheduleStore directly, validates member coverage through a file-backed join, and streams position, pin-plan and electrical-binding rows into typed stores without hop-sized schedule, coverage or output containers. Chimew lookahead publishes bank pairs/channels and group/member/fanin records once behind a sealed selector; qualification and native serialization walk lazy sequences, while the Phase 6 binding cursor is store-ordered by group ID and avoids an H-sized sort. Native assignment input now walks those validated cursors once and streams a versioned fixed-width protocol over stdin; exact counts, tags, CRC-32 and matching logical source digests reject truncation, trailing bytes and mixed generations without publishing `input.txt`. The explicit `--legacy-text` CLI remains for differential compatibility, unflagged input is rejected, and production always selects `--typed-input`. The native assignment certificate streams on unchanged stdout into a temporary typed index: an exact-order protocol state machine and typed record normalization precede certificate checking, which uses fixed-width vectors for only the active bank and a bounded repeated-cost cache instead of graph-sized assignment/alternative/potential dictionaries. Certificate headers must exactly match the input-derived Stage 1 and per-bank node counts before any potential rows are accepted. Its final assignment report is another selector plus sealed typed relation consumed by ordered qualification and adapter cursors. Qualification stages bank/channel identity in a temporary relation, batches channel rows at 4,096 even within one large bank, and recomputes every metric and direction priority instead of retaining bank/channel maps. Both pipeline and adapter generations inventory the selector and sidecar. Production readers reject legacy JSON unless an explicit compatibility caller passes `allow_legacy=True`. Exact selector/table envelopes, sidecar path confinement and raw-header-derived normalized contracts reject extensions, symlinks, typed-field coercion and re-sealed semantic relabelling. Store-level POSIX shared leases span each reader's last cursor; exclusive leases span staging and selector commit. An old selector is eligible for post-commit sidecar GC only after its complete selector and store view validate, so malformed selector bytes cannot nominate an arbitrary file for deletion. All stores are built and semantically validated in sibling staging, then immutable sidecars and selectors are published before an adapter report commit. Standalone adapters own their fixed report and garbage collection; pipeline-managed adapters use an immutable report generation and retain the preceding generation until the outer `pipeline_report.json` commits. The complete outer input/source/kernel/adapter inventory is staged and validated before its generation directory is atomically installed, and `pipeline_report.json` alone commits that generation. Orphan recognition reuses complete bundle semantics: ScheduleStore selector/sidecar, platform, every grouping/RUDY/bank artifact, qualification and source provenance, adapter pairing, paths and aliases must all validate after the seals. Manifest design/platform identity is bound to the sealed schedule and BoardDB. A resealed malformed or relabelled payload remains invalid and is never collected as a generation. Per-output writers hold an exclusive POSIX lock; validators and production consumers retain a shared lease across report and referenced-artifact reads, preventing GC from removing an older reader's generation. Phase 7 resolves binding entries with indexed lookups, and Phase 6 explicitly inventories every selector sidecar for relocation. | Chimew lookahead grouping now uses a sealed fixed-width stdin protocol and fixed-width result arrays; the native kernel assigns directly from sorted signal indices and no longer retains a group-of-vectors copy. Native group IDs now reuse each processed signal's domain field, so the separate assignment array is gone. A 100,000-signal byte-identical A/B reduced native peak RSS from 57,704,448 to 57,278,464 bytes while wall time changed from 23.04 to 23.30 seconds. Native signal/order/candidate arrays and the Python qualification oracle remain linear in signal count. The prior 20,000-channel/group input-store comparison reduced retained traced allocation 52.38 to 0.17 MiB, peak 59.39 to 43.52 MiB, RSS 159.75 to 121.63 MiB and bytes 12,292,219 to 6,125,401, while validation rose from 0.772 to 1.138 seconds. A serialization-only 100,000-channel/100,000-group comparison removed the 8,833,627-byte input scratch, reduced time from 1.146 to 0.693 seconds and traced peak from 0.042 to 0.006 MiB, while sending 12,800,193 typed bytes through the pipe; it proves disk/text removal, not lower IPC volume. A 30,000-group certificate-parser comparison reduced retained allocation 24.18 to 0.30 MiB, peak 31.34 to 1.74 MiB and scratch bytes 6,414,799 to a 4,907,008-byte temporary index, while parsing rose from 0.621 to 1.634 seconds. A 100,000-assignment report comparison reduced retained allocation 55.89 to 0.57 MiB, peak 55.96 to 2.99 MiB and bytes 15,156,114 to 1,068 plus 10,211,328, while publication/validation rose from 0.961 to 7.043 seconds. These are adapter measurements; native-producer and NVDLA wall/RSS/I/O qualification remain required. |
| W4 Phase 3 model and publication | **Partial** | Large dependency/capture traversal is native; assignment index v3 separates bounded SQLite control data from typed binary Static Exact data. Capture protocol v3 preserves original EmuIR ordinals and returns only fixed-width `(cut, partition, net ordinal, sink ordinal)` records. The production native-input bridge now streams compact `cell_records`, `net_records` and integer `endpoint_records` directly, so it does not build another instance-name ordinal map or decode the graph into Python endpoint dictionaries. Cut identity/class validation uses one ordered store merge, and capture coordinates are independently resolved with one ordered authoritative sink cursor instead of random whole-net decoding. Exact-binary v2 encodes bounded canonical capture-bit bytes and validates counts before allocation while keeping v1 read compatibility. Phase 3 carries the net ordinals discovered by cut construction directly into native input, removing the analyzer adapter's cut-name lookup/match/coordinate hash containers; name-only historical callers retain a checked compatibility lookup. The analyzer also returns predecessor CSR plus fixed-width launch/level/depth arrays and production publication derives all four graph-scale semantic relations as repeatable byte-backed streams. The exact-binary writer consumes the native CSR plus raw cut/capture fields directly, validates their agreement with the canonical view, and emits unchanged sections without rebuilding cut-name or dependency/logic-record populations. Native canonical hashing now consumes the same compact cut/capture bytes, launch flags and counter-derived ordinals without invoking dependency, logic or capture dictionary factories. One immutable launch-flag snapshot feeds both the factories and direct source, preventing caller-owned bytearray mutation from splitting their identities. Compact clustering now uses ordered SQLite cell/endpoint cursors plus fixed-width ordinal and union-find arrays, emitting final cluster rows without a decoded cell/net graph, instance-name index or grouped-member copy. Combinational-candidate characterization likewise consumes ordered compact endpoints, deduplicates arcs in a temporary relation and runs SCC over fixed-width CSR arrays; per-net state is bounded and high fanout is inserted in batches of at most 8,192. Its primary endpoint scan reads only integer columns: cell types are reduced to one-byte sequential-input policies during the cell pass and the interned pin vocabulary is loaded once, eliminating two indexed joins and repeated type/port text for every endpoint. Cell/net identity uniqueness is verified from complete single-column SQLite unique indexes rather than two additional full-table GROUP BY scans; removed or partial constraints fail closed. PATRON pressure construction now reads ordered compact endpoints, interns identities once, stores cluster/net membership and driver/sink relations in fixed-width ordinal arrays, and streams native input record by record through an atomic writer instead of materializing joined text. Production cluster publication now streams canonical JSON through zlib and incremental base64 directly into its atomic temporary file; it emits byte-identical v2 artifacts without retaining compressed chunks, joined bytes and a complete base64 string. A 100,000-cluster comparison reduced traced publication peak from 3,203,902 to 400,854 bytes while wall time changed from 2.246 to 2.494 seconds. Compact eligibility reconstruction now stores one ordinal bit per net plus a lexical UTF-8 name pool and fixed-width offsets; compact clustering and native validation use ordinal membership without recreating a Python name set. Production cluster publication iterates a private list-compatible view over that pool, while default and explicitly mutable APIs retain ordinary-list behavior. A 1,000,000-name representation comparison reduced process peak RSS from 139,264,000 to 57,245,696 bytes with a 16,125,008-byte packed payload (0.24 versus 0.30 seconds). A separate construction-plus-canonical-encoding comparison emitted the same 14,000,014 characters while reducing incremental traced peak allocation from 68,009,482 to 11,327 bytes and process peak RSS from 203,161,600 to 64,749,568 bytes; wall time changed from 1.618 to 1.780 seconds. Materialized and legacy contracts retain the generic encoder, while the independent binary reader reconstructs the logical contract and verifies the unchanged canonical SHA. Digest tests reject mixed generations and truncated raw nodes; the binary publisher independently rejects stateful-CSR disagreement and million-range lexical-order errors. A synthetic 10,000-cut/79,964-edge comparison measured 0.832--0.847 seconds for record replay and 0.736--0.762 seconds for the typed cursor, with 0.039 MiB traced peak; a separate 300,000-cell clustering comparison reduced traced peak from 436.58 MiB to 212.52 MiB and wall time from 9.94 to 5.70 seconds. Both are adapter evidence rather than NVDLA qualification. | Final cluster/path objects and the C++ analyzer's internal graph/capture vectors still scale with the graph. The emitted eligible-name policy array remains graph-sized on disk until the cluster artifact schema becomes ordinal/typed, although production no longer retains a second decoded Python list; internal reconstruction and consumers use the packed ordinal/name view. Canonical hashing remains linear in the logical record count even though it no longer constructs the three derived dictionary streams. RePart/MFSPart initial candidates can still materialize a transient contract before their final rebuild. |
| W5 physical exporters | **Partial** | One typed physical handoff replaced production `packed-sites.json` and `placement.json`. The production bridge appends normalized clusters directly to a private SQLite artifact; legalization retains only MUX-bearing slices and independent topology validation retains only required MUX/CARRY records. Assignment/site/BEL ownership uses unique constraints, counts, a single ordered scan and bounded per-site state. Source-certificate ownership and full mapped-output driver uniqueness use temporary disk-backed relations rather than million-entry Python maps. Publication occurs only after all checks pass. Lazy typed views feed RWRoute and routed timing; mapped-index v2 owns typed cell pins and RWRoute streams route-cell, physical-binding, endpoint, deterministic safe-name ordinal and clock-candidate rows. The production atomic-source artifact is now a compact selector plus sealed typed sidecar: only macro/cascade/constant facts are stored, while ordinary singleton clusters are lazily derived from the authoritative mapped-index cell/type cursor. The downstream atomic name map is also selector-backed: atom and CARRY8 cascade identities live in a typed sealed sidecar, the selector binds mapped/packed/architecture sources, and production consumers use the lazy logical v2 view. Both historical graph-sized JSON forms require explicit compatibility export. Placement validation now streams native `.pl` coordinates into a temporary file-backed relation and joins them directly to the leased typed name-map sidecar. SQLite proves duplicate, unknown and missing atom coverage, owns normalized site/BEL records and rejects repeated physical slots through a unique occupancy relation. Macro and cascade checks perform bounded instance queries, while one ordered site cursor performs control-set/resource checks. Production writes those validated site groups directly into a versioned content-addressed placement-certificate store instead of constructing and then renormalizing the final cluster tree. The small selector binds exact store identity/counts plus mapped, packed, architecture, name-map, native-placement and optional native-constraint/provider seals; selector-last publication, shared/exclusive leases and post-commit GC preserve complete generations across failures and concurrent readers. The archive retains selector and sidecar atomically, and the physical handoff path rejects inline legacy clusters. A 100,000-cluster publication comparison reduced retained traced allocation from 108.90 MiB to 0.045 MiB, peak from 110.88 MiB to 2.02 MiB and process peak RSS from 281.81 MiB to 45.27 MiB, with identical 22,085,632-byte typed stores; fsync-inclusive wall time was 1.705 versus 1.953 seconds. Earlier evidence remains: a 100,000-singleton source projection used a 1,073-byte selector and 45,056-byte sidecar with 1.10 MiB traced peak allocation versus 28,500,397 bytes and 167.93 MiB when materialized; a 100,000-atom name-map writer reduced peak from 28.21 MiB to 2.34 MiB; `_collect_atoms` reduced peak from 38.00 MiB to 29.00 MiB; and shuffled end-to-end placement normalization before this final publication change reduced peak from 188.98 MiB to 43.61 MiB. These are adapter measurements, not NVDLA qualification. | Electrical-binding maps, clock-gate qualification subset, parameter lookups and the remaining exporter loop still scale with design data. The exporter still retains the final atom list and safe-name map. Explicit compatibility calls can still materialize the certificate tree. DeviceRegionIndex and pin-aware SLR planning are separate semantic/QoR work. NVDLA wall-time, process-RSS and temporary-I/O qualification remain required. |
| W6 global STA stitching | **Started** | OpenSTA remains the numerical engine, indexed timing readers exist, and Phase 7C no longer expands the whole ScheduleStore before binding. Measurement TSV import validates endpoint order and arc equations in one pass. Production event/check construction now appends interned strings and fixed-width columns to `_IndexedEventChecks`, validates target/runtime and TX/commit coverage online, and creates representative indices without a second complete compression scan. Original-path metadata is a sorted, interned, fixed-width mapping instead of one nested dictionary per path. Canonical report stitching consumes one path bundle at a time. Representative membership uses a fixed-width integer map and exposes original-check measurements through a repeatable lazy sequence, so stitching and TSV publication no longer allocate object/arc lists or a second dictionary list for all expanded checks. Logic-segment producers now write a content-addressed typed store directly; selectors and physical summary bind route, schedule and Phase 3 logical seals. Boundary identities, boundary timing and local-path timing are leased typed stores, and physical-summary readers resolve each FPGA lazily. A leased bounded multi-FPGA ordered merge validates global segment/path-key uniqueness and supplies Global STA/system timing without the former `path -> member -> segments` tree or per-member/per-FPGA queries. Archive retention follows the complete summary/selector/store group. Logic-segment query preparation now spools ordered rows on disk and atomically publishes the query with its content-addressed identity selector. Measurement import streams into a temporary typed relation, proves exact coverage and equations with set joins, and writes the final timing store directly without per-row identity queries. Interleaved legacy input remains a checked compatibility fallback and WNS/TNS still count every original path once. Local-path timing producers now publish ordered typed rows directly, and validation projects identity fields through a repeatable lazy sequence while accumulating statistics in one pass. A 100,000-path typed-owner comparison reduced process peak RSS from 251,887,616 to 32,653,312 bytes and artifact bytes from 30 MiB to 24 MiB, while read and full seal validation changed from 0.73 to 0.90 seconds. | String interning, path IDs and representative identity tables remain linear in unique checks/paths, and the legacy interleaved fallback still requires graph-sized Python work. Other per-FPGA evidence remains to migrate. NVDLA Phase 7C wall/RSS/I/O qualification remains required. |
| W7 native production entry | **Started** | The installed `emuflow-native` executable now dispatches the real Phase 3 Static Exact analyzer and Phase 5 exact scheduler in-process through one linked stage library. Production adapters prefer that unified sibling and retain the standalone executables only for older installs and explicit comparison overrides. Differential checks require byte-identical output. | There is no complete `emuflow-native compile` path yet. Phase 1/2/4/6/7 orchestration remains in Python, and Python production memory can still scale with routes, endpoints, and timing paths. Move stage ownership into this entry incrementally rather than delegating `compile` back to the Python CLI. |

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

Compact EmuIR clustering now follows the same integer-first rule. One ordered
cell cursor builds a four-byte original-sequence to lexical-ordinal
permutation; ordered endpoint records drive compact union-find arrays; a final
ordered cell/resource cursor emits the canonical cluster payload directly.
The production path therefore does not decode the graph into cell/net
dictionaries or retain the old instance dictionary, name index and grouped
member copy beside the final result. The legacy in-memory implementation
remains the differential oracle. Tests cover group/fixed semantics and reject
an invalid endpoint owner before publication. A synthetic 300,000-cell
singleton comparison measured loading plus clustering at 436.58 MiB traced
peak and 9.94 seconds for the in-memory path versus 212.52 MiB and 5.70 seconds
for the ordered compact path. These numbers isolate construction and do not
qualify NVDLA.

The production exact-binary boundary now emits sections directly from the
native integer topology and compact typed record buffers. Capture protocol v3 already avoids a token/string
round-trip: production native input scans the compact EmuIR integer relations
directly and output identifies only the requested cut, partition, net and sink
ordinals. Python resolves those coordinates with one ordered authoritative sink
cursor and performs independent semantic checks; it does not rebuild ordered
instance/net copies, an instance-name ordinal map, or whole-net dictionaries
for captures. Stable segment
numbering is derived from ordered counters rather than duplicate graph-sized
lookup maps. Cut construction also forwards its already-known net ordinal
column to the analyzer, which checks every `(name, ordinal)` pair against EmuIR
and avoids rebuilding cut-name lookup, matched-name and coordinate maps. The
name-only compatibility entry remains available for old callers. The current implementation transfers predecessor lists directly into a
byte-backed cut-node sequence, while dependency edges, logic segments and
capture records remain repeatable typed streams for the canonical digest. The
exact-binary writer no longer replays the dependency or logic record streams:
it consumes predecessor CSR, launch flags, raw cut-node fields and raw capture
fields, checks the integer relation against the canonical node records, and
stages each validated edge once in a typed temporary spool before canonical
section assembly. Capture-node segment identities are checked in the same
lexical order used by the v3 contract across six-digit boundaries, while the
binary logic section remains in numeric global-ordinal order. Exact-binary v2 bounds capture bits to
8192 bits, uses canonical unsigned byte encoding, and validates section sizes
before allocation. Independent readers still reconstruct and validate the
published relation. NVDLA qualification must measure the remaining digest
decode CPU, final cluster/path and eligible-candidate memory, and analyzer-side
graph/capture vectors. The eligible-name policy no longer adds a second
retained Python string list in production, but its logical JSON array still
requires linear encoding and artifact bytes.

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

A 100,000-cell/100,000-net synthetic compact-input bridge comparison measured
15.42 MiB peak Python allocation and 1.58 seconds for the former decoded
instance map plus endpoint loop, versus 2.03 MiB and 1.50 seconds for the direct
SQLite integer-relation stream (-86.85% peak allocation). This isolates the
native input adapter and is not an NVDLA stage-level claim.

The typed writer now emits the binary sections directly from the native CSR
and compact byte-backed cut/capture relations, so publication no longer builds
a cut-name hash table or expands dependency and logic-segment dictionaries. A
100,000-cut/100,000-capture comparison measured 23.77 MiB versus 9.71 MiB peak
Python allocation and 14.368 versus 6.135 seconds, with byte-identical output.
The next Phase 3 tranche is to compute the canonical digest from the same typed
relation without replaying logical dictionaries and to reduce the C++
analyzer's internal graph/capture vectors. This isolated writer result is not
an NVDLA stage-level claim.

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
| 3 | Assignment JSON, SQLite, and exact relations previously repeated the same graph. | Assignment-index v3 and `assignment.exact.bin` are the implemented control/data split. Native predecessor CSR and fixed-width launch/level/depth arrays replace four analyzer-side Python maps. Capture protocol v3 returns fixed-width original net/sink ordinals. The production adapter streams compact cell/net/integer-endpoint rows directly into native input, validates cut ordinals/classes with an ordered merge, and resolves capture coordinates against one ordered sink cursor. It therefore avoids the extra instance-name ordinal map, decoded endpoint graph, per-cut random queries and per-capture whole-net decoding while preserving name-only compatibility. Compact clustering separately uses ordered integer cell/endpoint cursors and fixed-width union-find state, so the final clusters are no longer built beside instance/name/group graph copies. Exact-binary v2 provides bounded canonical capture-bit storage with v1 read compatibility. Production PATRON derives all four semantic relations as repeatable byte-backed streams. The exact-binary writer consumes the native predecessor CSR plus raw cut/capture bytes directly, checks them against the canonical record view, and avoids cut-name/hash and dependency/logic-record expansion while preserving byte identity. Combinational-candidate characterization and PATRON pressure construction now use ordered compact endpoints, temporary arc deduplication, fixed-width CSR/membership arrays and streamed native input. Cluster publication streams its compressed/base64 envelope directly to the atomic file without another complete encoded payload. The production policy writer also walks the packed lexical candidate view without retaining a second Python string list. The logical v1 policy array, cluster/path objects and the C++ analyzer's internal graph/capture vectors remain pending schema/native work. |
| 4 | The former `routes.json` repeated demands and routed records inside one graph-sized JSON document, forcing Phase 5 to parse and retain it before scheduling. | `routes.json` is now a small selector for a content-addressed typed RouteStore. Demand records are projected from their authoritative routes; sinks, edges, predecessors and timing paths are normalized once. Phase 4 and the final flow report inventory the sidecar, and legacy `read_json` remains an explicit materializer. |
| 5 | The former canonical `schedule.json` serialized every routed hop and managed mode only wrapped that complete JSON in zlib/base64. Phase 5 also sorted and indexed the complete Phase 4 route dictionary population. | RouteStore ordered cursors feed native Static Exact input, SQL supplies stable net/demand ranks and cross-layer hop order, and Phase 5 retains compact route columns plus lazy schedule-route projections. ScheduleStore v4 stores hops, routes, route sinks/edges, completions and arrivals as typed relations and keeps only domain-scale JSON records. Cross-layer timing v2 verifies exact route/schedule hop coverage with an ordered cursor merge and stores only the two artifact digests, merge policy and hop count; it no longer emits a second H-scale `hop_bindings` array. Materialized v1 contracts, both logical seals and legacy readers remain supported. Standalone independent exact validation still has route-level maps, so NVDLA I/O qualification is pending. |
| 6 | Streaming split metadata formerly rebuilt every hop as endpoint/lane/binding dictionaries and repeated the population in several JSON artifacts. | Endpoint, lane, anchor, board-binding and adapter-port relations are stored once in sealed `transport.sqlite`. The bounded `binding_physical_ports` relation lets physical-I/O contract v2 emit one record per real pair with a member count/seal. Its reader replays one ordered authoritative binding cursor to verify those memberships without serializing them into the contract. Pin plans, position hints and Chimew electrical groups/members use compact selectors plus sealed indexed stores; the Chimew adapter consumes ScheduleStore through a file-backed member join and streams all three outputs without hop-sized schedule, coverage or output containers. The upstream Chimew bank/channel input and final assignment report are strict selectors plus ordered SQLite relations, and every pipeline/adapter generation inventories their sidecars. Native certificate output streams directly into a temporary typed index; independent primal/residual-dual validation uses ordered cursors, fixed-width active-bank vectors and a bounded cost cache instead of Python certificate maps. Store-level leases protect old readers during replacement, normalized headers are rederived from sealed facts, and production rejects implicit legacy input. Phase 7 performs indexed binding lookup, and reports inventory every sidecar required for relocation. Chimew grouping now uses a sealed typed stdin stream and fixed-width result arrays; the C++ group-of-vectors copy is gone. Native group IDs reuse the processed signal domain field, so no separate assignment array remains. Native signal/order/candidate arrays and the Python oracle remain linear in signal count. |
| 7 | Transport synthesis previously wrote a full Yosys JSON, imported it into another full Python `EmuIR`, and loaded the graph again. Boundary identities repeated every endpoint in JSON, and the OpenPARF bridge built and reread separate packing/placement trees while validators rebuilt multiple owner maps. | Generic Yosys pipes directly to indexed EmuIR and lowering copies records set-wise. Boundary identities now stream into a typed owner and are validated by ordered merge. The production bridge writes one private typed handoff incrementally, validates ownership and packing/placement identity with SQLite constraints, ordered scans and bounded MUX/CARRY state, then atomically publishes. Source ownership and full output-bit driver uniqueness use temporary disk relations. RWRoute and timing consume lazy views; compatibility mode alone exports the historical large JSON contracts. |

Cross-layer and global timing paths were also checked. Their current indexed
readers stream complete payloads or selected identities. Physical timing
evidence remains in one authoritative per-FPGA file and the whole-design
summary stores only a sealed reference, so that population is no longer copied
into a second JSON tree. Independent terminal validation is retained where it
proves a different boundary; redundant replay inside the same producer process
is removed. Boundary and logic-segment identities now use content-addressed
typed owners, while logic query and measurement handoff use typed stores plus
set-based import. Boundary timing now uses an indexed typed owner. Moving local-path timing and
the remaining event-check identity tables to indexed/native stores remains
W2/W6 work.

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
SQLite. The atomic-source control file is now a selector over a typed sidecar
and the already-authoritative mapped index: macro/cascade/constant facts are
stored once, ordinary singleton clusters are derived by an ordered cell/type
cursor, and only an explicit compatibility operation expands the historical
JSON tree. Its physical-stage evidence records source-build wall time,
selector/sidecar bytes, stored macro count and derived singleton count. One
normalized, versioned physical-handoff database replaces the
packed and placement JSON publications in the production native path. The
atomic-source publisher discovers the previous sealed generation without
opening the replacement mapped artifact, validates the new store and selector
in staging, commits selector-last, and removes the old sidecar only after that
commit. Its selector/header/count/mapped envelopes reject unknown keys, while
canonical meaning is independent of JSON key order and whitespace. The
derived `atomic-source-*` identity namespace is reserved, and macro/cascade
identity collisions fail closed.
The placement-certificate boundary follows the same fail-closed generation
contract. Its reader takes the shared lease before reading the selector and
keeps it through descriptor validation and the final ordered assignment row.
It compares exact `PRAGMA table_xinfo` results, whole-table counts, cluster and
per-cluster assignment continuity, and orphan coverage. A versioned canonical
digest streams the compact selector header plus every ordered cluster and
assignment record; it is stored in both metadata and selector, independently
recomputed by readers, and remains stable across equivalent SQLite page
layouts. The SQLite file digest continues to protect the physical bytes.
Archive discovery rejects mixed generic and `cluster_storage` declarations as
ambiguous rather than choosing either sidecar.
The atomic placement name map uses the same production rule. Its JSON path is
a small selector; the typed sidecar owns ordered `aN` atom identity/type/source
cluster/resource rows and CARRY8 cascade records. Exact envelopes, byte seals,
logical record seals, source hashes, contiguous ordinals and unique
OpenPARF/instance identities are checked without materializing the rows.
Readers hold a POSIX shared lease through their last lazy row and writers hold
the exclusive lease through selector commit and old-sidecar collection, so an
old view remains valid while replacement waits. Post-commit unlink failures
become deferred GC and cannot contradict the visible selector. Selector/store
symlinks and sidecars outside the selector directory fail closed. Legacy JSON
requires an explicit compatibility option and must carry every source seal a
caller asks to verify. CARRY8 records normalize ordered adjacent links and
member ownership; writer and reader separately require existing CARRY8 atoms
and unique ownership within and across chains. Selector-last replacement keeps
the prior generation valid on failure, the atomic manifest inventories both
files plus mapped/packed/architecture identity, all production placement
consumers check packed identity, and a separately named materializer is the
only route to the historical complete v2 JSON.
The physical-handoff producer appends clusters to a
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
selected exporter relations after the lazy mapped-source scan. Clock-gate
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

The current cross-wave regression covers Phase 3 compact clustering,
candidate/pressure characterization and capture protocol; packed Phase 5
independent validation and ScheduleStore v4 consumers; Phase 6 typed native
grouping input and binding/adapter contracts; typed physical handoff and
placement-certificate publication; and typed logic-segment identity, query,
measurement and Global STA stitching: 864 tests passed in 339.10 seconds, 5
optional-tool tests skipped and 2
unavailable external-fixture cases were explicitly deselected.
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
