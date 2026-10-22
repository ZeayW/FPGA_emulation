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
| W0 shared core and stores | **Started** | `libemuflow_core` now owns bounded streaming native artifact reads, synchronized atomic binary publication, representation-scoped typed IDs, the common magic/version envelope check, and shared CRC-32/SHA-256 primitives used by the Phase 6 grouping, refinement and assignment protocols. The shared reader no longer copies a complete native input into memory. The Static Exact analyzer and exact scheduler use that shared boundary. | Move the remaining native tools onto the shared storage/error/metrics interfaces; extend the stage-neutral schema catalog and add SQLite RAII views as consumers require them. |
| W1 Phase 5 exact schedule | **Partial** | Phase 4 now publishes a selector plus sealed typed RouteStore instead of a graph-sized `routes.json`; demands are projected from authoritative route rows, while route sinks/edges, exact predecessors and timing paths are normalized relations. Phase 5 consumes ordered route and hop cursors directly. For indexed Static Exact designs with at least 100,000 routed cuts, `emuflow_tdm_exact_scheduler` performs dependency ordering, readiness propagation, lane/slot occupancy, and completion calculation in C++. Its adapter derives integer ranks in SQL and retains compact columns instead of Python route dictionaries and net/demand maps. Fixed hop and variable completion records remain in memory-mapped lazy views; completion ordering and offsets use fixed-width arrays, while certificate sink arrivals use a packed net-rank by FPGA-rank matrix. Phase 5 publishes ScheduleStore v4 with typed hops, routes, route sinks/edges, completions and sink arrivals. Both logical seals remain compatible, legacy route JSON and ScheduleStore v1--v3 readers remain available, and stores use selector-last content-addressed publication. RouteStore validates and can restore the previous generation, binds the artifact filename to its digest, validates the new view before old-sidecar cleanup, omits unjustified append-only `VACUUM`, and joins every generic archive selector/sidecar pair into one retention group. The ScheduleStore, selector, cross-layer contract, optional ratio plan, standalone derivatives and report are staged as one generation. The report records a canonical self-identity and the name, size and SHA-256 of every member. Publication fsyncs all members and pre-report directory entries before `phase5_report.json` commits the generation; caught failures restore the prior bytes, while uncaught termination leaves a mixed generation that every direct validator, Phase 7C and canonical/shared consumer rejects before schedule/contract access. Report-less legacy data requires an explicit compatibility option. Production Phase 6 dispatch reads only the typed entry count; Phase 7C and physical/local timing readers keep RouteStore lazy. Small designs retain the Python scheduling reference. Historical canonical readers inventory the complete route/TDM output directories; shared replay dynamically resolves and copies both content-addressed sidecars before their selectors and rejects dangling generations. Managed roots use bounded strict-envelope, basename and size checks because the root checkpoint already sealed the bytes and binds the Phase 5 report/member table; standalone replay retains full hashing and semantic validation. | Independent typed validation now reconstructs exact ready order with packed CSR/rank arrays, uses a compact arrival matrix, streams certificate digests and compares completion rows through a bounded temporary SQLite relation instead of route-level Python maps. The native adapter retains R-scale compact columns, the ratio-delay baseline branch performs H-scale work when enabled, and completion identity columns remain route-scale, while completion payloads are lazy and sink arrivals use fixed-width packed storage. A synthetic 100,000-route/two-sink comparison reduced retained traced completion/arrival allocation from 65.70 MiB to 2.67 MiB of packed arrays plus a 3.05 MiB native record file; a complete lazy traversal added 0.014 MiB traced peak. This is adapter evidence, not NVDLA qualification. At 100,000 routes, independent-root validation used 13.88 MiB versus 316.65 MiB peak (4.55 versus 6.94 seconds); a 99,999-edge chain used 5.51 MiB versus 338.55 MiB (5.25 versus 6.33 seconds). Cross-layer contract v2 now merges ordered RouteStore/ScheduleStore hop cursors, binds their digests and checked count, and omits the former H-scale `hop_bindings`; materialized v1 artifacts remain validatable. A synthetic 100,000-hop typed-store comparison reduced retained Python allocation from 38.69 MiB to 0.17 MiB, peak allocation from 82.58 MiB to 44.06 MiB, and contract JSON from 6.87 MiB to 0.00063 MiB at 0.746 versus 0.730 seconds. This is adapter evidence, not NVDLA qualification. Terminal full-bundle replay still materializes compatibility views in some paths. A synthetic 100,000-route one-hop build reduced Python peak allocation 346.1 MB to 108.2 MB and route bytes 46.60 MB to 32.19 MB; two repeated builds were 8.16--8.32 seconds versus 8.92--9.59 seconds. This is adapter evidence only. NVDLA wall-time, RSS and temporary-I/O qualification remain required. |
| W2 Phase 7 lowering and transport import | **Partial** | Indexed DUT lowering and placement stores avoid the former whole-DUT JSON path. Generic Yosys now streams synthesized transport JSON directly into compact EmuIR SQLite, so `transport-synthesized.json` is never materialized. Lowering opens the store as a bounded view and the placement writer uses SQLite `ATTACH` to copy, namespace and filter transport cells/nets/endpoints without reconstructing the transport graph. RX signal/placeholder coverage uses ordered store cursors. Boundary identity production now streams endpoint records into a content-addressed typed owner and publishes only a bounded selector; independent identity and timing validation merge ordered endpoint cursors instead of building expected/actual dictionaries, and timing construction no longer rebuilds an identity map. The physical summary relocates the selector and immutable generation without expansion. Whole-design physical assembly publishes sealed per-FPGA evidence references instead of embedding all boundary/path databases again in `physical-summary.json`. A 100,000-endpoint producer comparison reduced process peak RSS from 179,355,648 to 53,002,240 bytes and artifact bytes from 42,855,750 to 29,086,441; fsync-inclusive wall time changed from 1.008 to 1.546 seconds. A separate 100,000-record validator comparison reduced process peak RSS from 82,395,136 to 17,645,568 bytes while wall time changed from 0.104 to 0.201 seconds. Boundary timing producers now publish a bounded selector plus a content-addressed typed SQLite owner; RapidWright timing generation streams directly into the writer, the physical summary relocates the complete selector/store pair, and readers validate each FPGA lazily. Local-path timing now uses the same selector-backed typed-owner model for VPR, RapidWright, inactive partitions, physical-summary relocation and lazy consumers. A 100,000-record local-path read and full seal validation reduced process peak RSS from 251,887,616 to 32,653,312 bytes and artifact bytes from 30 MiB to 24 MiB, while wall time changed from 0.73 to 0.90 seconds. | Interface-net overlay records and backend measurement/query builders still retain endpoint-scale maps. Move the remaining query/import joins to native/indexed cursors, then qualify the full per-FPGA path for RSS and I/O on NVDLA. |
| W3 Phase 6 split and binding | **Partial** | DUT ownership/split uses SQLite joins. Phase 6 writes endpoint, lane, virtual-anchor, virtual-board binding and differential adapter-port relations once to sealed `transport.sqlite`. A bounded `binding_physical_ports` relation records one row per real differential pair, its member count and ordered member seal. Indexed physical-I/O contract v2 independently recomputes those memberships from one ordered authoritative binding-entry cursor, then merges them with the compact relation and adapter ports; it does not serialize every hop for each FPGA. Legacy v1 remains readable. Typed pin-plan and position-hint stores own hop-sized rows behind small selectors, validators use ordered merges/indexed joins, and Transport RTL plus XDC/SystemVerilog stream atomically. The production Chimew builder reads ScheduleStore directly, validates member coverage through a file-backed join, and streams position, pin-plan and electrical-binding rows into typed stores without hop-sized schedule, coverage or output containers. Chimew lookahead publishes bank pairs/channels and group/member/fanin records once behind a sealed selector; qualification and native serialization walk lazy sequences, while the Phase 6 binding cursor is store-ordered by group ID and avoids an H-sized sort. Native assignment input now walks those validated cursors once and streams a versioned fixed-width protocol over stdin; exact counts, tags, CRC-32 and matching logical source digests reject truncation, trailing bytes and mixed generations without publishing `input.txt`. The explicit `--legacy-text` CLI remains for differential compatibility, unflagged input is rejected, and production always selects `--typed-input`. The native assignment certificate streams on unchanged stdout into a temporary typed index: an exact-order protocol state machine and typed record normalization precede certificate checking, which uses fixed-width vectors for only the active bank and a bounded repeated-cost cache instead of graph-sized assignment/alternative/potential dictionaries. Certificate headers must exactly match the input-derived Stage 1 and per-bank node counts before any potential rows are accepted. Its final assignment report is another selector plus sealed typed relation consumed by ordered qualification and adapter cursors. Qualification stages bank/channel identity in a temporary relation, batches channel rows at 4,096 even within one large bank, and recomputes every metric and direction priority instead of retaining bank/channel maps. Both pipeline and adapter generations inventory the selector and sidecar. Production readers reject legacy JSON unless an explicit compatibility caller passes `allow_legacy=True`. Exact selector/table envelopes, sidecar path confinement and raw-header-derived normalized contracts reject extensions, symlinks, typed-field coercion and re-sealed semantic relabelling. Store-level POSIX shared leases span each reader's last cursor; exclusive leases span staging and selector commit. An old selector is eligible for post-commit sidecar GC only after its complete selector and store view validate, so malformed selector bytes cannot nominate an arbitrary file for deletion. All stores are built and semantically validated in sibling staging, then immutable sidecars and selectors are published before an adapter report commit. Standalone adapters own their fixed report and garbage collection; pipeline-managed adapters use an immutable report generation and retain the preceding generation until the outer `pipeline_report.json` commits. The complete outer input/source/kernel/adapter inventory is staged and validated before its generation directory is atomically installed, and `pipeline_report.json` alone commits that generation. Orphan recognition reuses complete bundle semantics: ScheduleStore selector/sidecar, platform, every grouping/RUDY/bank artifact, qualification and source provenance, adapter pairing, paths and aliases must all validate after the seals. Manifest design/platform identity is bound to the sealed schedule and BoardDB. A resealed malformed or relabelled payload remains invalid and is never collected as a generation. Per-output writers hold an exclusive POSIX lock; validators and production consumers retain a shared lease across report and referenced-artifact reads, preventing GC from removing an older reader's generation. Phase 7 resolves binding entries with indexed lookups, and Phase 6 explicitly inventories every selector sidecar for relocation. RUDY production dispatch now uses the unified native entry and streams its existing grammar over stdin/stdout, eliminating the net-sized Python line list and both payload scratch files while preserving the independent Python bin replay. | Chimew lookahead grouping and position refinement now use sealed fixed-width stdin protocols and ordered output streams; position refinement no longer constructs a complete text line list or publishes input/output scratch files. A 100,000-signal refinement serialization comparison reduced traced Python peak allocation from 14,577,179 to 1,192,370 bytes and process peak RSS from 96,387,072 to 75,825,152 bytes, while the two-pass logical seal changed operation time from 0.531 to 1.591 seconds; the 3,300,104 typed bytes are IPC volume rather than a persistent production artifact. This is adapter evidence, not NVDLA qualification. Grouping uses a sealed fixed-width stdin protocol and fixed-width result arrays; the native kernel assigns directly from sorted signal indices and no longer retains a group-of-vectors copy. Native group IDs now reuse each processed signal's domain field, so the separate assignment array is gone. A 100,000-signal byte-identical A/B reduced native peak RSS from 57,704,448 to 57,278,464 bytes while wall time changed from 23.04 to 23.30 seconds. Native signal/order/candidate arrays and the Python qualification oracle remain linear in signal count. The prior 20,000-channel/group input-store comparison reduced retained traced allocation 52.38 to 0.17 MiB, peak 59.39 to 43.52 MiB, RSS 159.75 to 121.63 MiB and bytes 12,292,219 to 6,125,401, while validation rose from 0.772 to 1.138 seconds. A serialization-only 100,000-channel/100,000-group comparison removed the 8,833,627-byte input scratch, reduced time from 1.146 to 0.693 seconds and traced peak from 0.042 to 0.006 MiB, while sending 12,800,193 typed bytes through the pipe; it proves disk/text removal, not lower IPC volume. A 30,000-group certificate-parser comparison reduced retained allocation 24.18 to 0.30 MiB, peak 31.34 to 1.74 MiB and scratch bytes 6,414,799 to a 4,907,008-byte temporary index, while parsing rose from 0.621 to 1.634 seconds. A 100,000-assignment report comparison reduced retained allocation 55.89 to 0.57 MiB, peak 55.96 to 2.99 MiB and bytes 15,156,114 to 1,068 plus 10,211,328, while publication/validation rose from 0.961 to 7.043 seconds. These are adapter measurements; native-producer and NVDLA wall/RSS/I/O qualification remain required. |
| W4 Phase 3 model and publication | **Partial** | Large dependency/capture traversal is native; assignment index v4 separates bounded SQLite control data from typed binary Static Exact data and now owns the normalized cut records, sinks and predecessor identities. Assignment-storage v4 omits the former complete `cut_nets` JSON array and binds the typed relation by count and canonical ordered-record SHA-256. Production Phase 4--7 consumers receive a repeatable ordered cursor; compatibility reads reconstruct the unchanged logical assignment. Capture protocol v3 preserves original EmuIR ordinals and returns only fixed-width `(cut, partition, net ordinal, sink ordinal)` records. The production native-input bridge now streams compact `cell_records`, `net_records` and integer `endpoint_records` directly, so it does not build another instance-name ordinal map or decode the graph into Python endpoint dictionaries. Cut identity/class validation uses one ordered store merge, and capture coordinates are independently resolved with one ordered authoritative sink cursor instead of random whole-net decoding. Exact-binary v2 encodes bounded canonical capture-bit bytes and validates counts before allocation while keeping v1 read compatibility. Phase 3 carries the net ordinals discovered by cut construction directly into native input, removing the analyzer adapter's cut-name lookup/match/coordinate hash containers; name-only historical callers retain a checked compatibility lookup. The analyzer returns predecessor CSR plus fixed-width launch/level/depth arrays and production publication derives all four graph-scale semantic relations as repeatable byte-backed streams. Its internal instances, nets, cuts, incoming/outgoing incidence and dependencies now also use flat fixed-width columns and CSR rather than object and vector-of-vector populations. On a byte-identical 500,000-instance/net/cut input, this reduced native peak RSS from 136.89 MB to 53.92 MB and user CPU from 0.12 to 0.07 seconds; output SHA-256 was unchanged. Capture discovery now spills sorted unique runs of at most 1,048,576 fixed-width records, merges at most 64 runs at a time, and bulk-copies the final canonical run into the atomic output. A 2,200,000-capture multi-run comparison was byte-identical, reduced process peak RSS from 185,663,488 to 133,627,904 bytes and changed wall time from 0.69 to 0.74 seconds; the 35,200,029-byte output left no sort files. The exact-binary writer consumes the native CSR plus raw cut/capture fields directly, validates their agreement with the canonical view, and emits unchanged sections without rebuilding cut-name or dependency/logic-record populations. Native canonical hashing consumes the same compact cut/capture bytes, launch flags and counter-derived ordinals without invoking dependency, logic or capture dictionary factories. Compact clustering uses ordered SQLite cell/endpoint cursors plus fixed-width ordinal and union-find arrays. The authoritative production cluster owner stores cell names once in a UTF-8 pool, membership as CSR ordinals, resources in uint32 columns and only sparse fixed/group constraints; it remains a lazy list-compatible v1 view for unchanged providers and canonical JSON. Final Static Exact assignment, cut discovery, semantic input, online validation, packing and assignment-index publication consume its dense source-cell ownership column directly. A 200,000-singleton comparison reduced retained traced cluster allocation from 141.62 MB to 16.13 MB, process peak RSS from 323.91 MB to 34.18 MB and construction time from 0.55 to 0.26 seconds; streamed canonical hashes were byte-identical, while encoding changed from 2.01 to 3.12 seconds. The final production assignment no longer builds, sorts or rescans a graph-sized Python `instance -> FPGA` dictionary. Compact cut construction also retains net identities once with fixed-width source/sink CSR, endpoint-count and round columns; the final Static Exact record view overlays typed dependency fields lazily. A 200,000-cut comparison reduced retained traced allocation from 85,428,138 to 10,995,827 bytes and process peak RSS from 246,202,368 to 35,536,896 bytes, with an identical canonical SHA-256; construction plus traversal changed from 1.82 to 3.74 seconds. A separate 200,000-singleton assignment comparison reduced retained traced allocation from 10.11 MiB and peak allocation from 27.21 MiB to 2.29 MiB of total fixed-width ownership columns with 1.62 MiB incremental peak. PATRON pressure construction exposes cluster summaries as a lazy projection and stores timing paths in fixed-width identity, timing, source-net, pressure-net and endpoint-cluster columns; native input consumes those columns without rebuilding per-path dictionaries or net-name lists. TimingPathDB v3 supplies unique path identity, so production does not retain a second path-ID set. Native output parsing now consumes PATRON records incrementally; production retains final canonical cluster ownership as one uint32 part-index column through candidate construction, online validation and assignment packing. A 200,000-record comparison reduced retained traced allocation from 10,493,049 to 804,115 bytes and peak allocation from 15,757,674 to 1,606,908 bytes; the packed ownership payload was 800,000 bytes. Production partition summaries now keep counts and resource totals without retaining a second complete set of cluster names; compatibility expansion reconstructs those lists from the packed cluster-part column. A 200,000-cluster comparison reduced retained traced allocation from 12,986,704 to 5,816 bytes at 1.079 versus 1.098 seconds. On an isolated 100,000-cut chain, moving the cut records from duplicate JSON into the typed owner reduced total cut artifact bytes from 25,603,517 to 17,117,431 and sealed read peak from 119,371,676 to 3,962,233 traced bytes, while traversal changed from 0.39 to 1.54 seconds. In other isolated comparisons, 200,000 cluster summaries reduced retained traced allocation from 90.05 MiB to about 1 KiB of projection state, while 100,000 single-net timing paths reduced retained allocation from 46.54 MiB to 8.59 MiB. Combinational-candidate characterization consumes ordered compact endpoints, deduplicates arcs in a temporary relation and runs SCC over fixed-width CSR arrays; per-net state is bounded and high fanout is inserted in batches of at most 8,192. Production cluster publication writes membership, resource, fixed/group and eligible-net relations once as bounded-memory, chunked zlib typed columns in `clusters.sqlite`; `clusters.json` is a small SHA-256-sealed selector. Compatibility reads reconstruct the unchanged logical v1 document explicitly. On 200,000 singleton clusters plus 200,000 eligible nets, the typed selector/store used 1,589,621 bytes versus 2,133,472 bytes for the former compressed/base64 logical JSON and took 1.85 versus 1.90 seconds. Materialized and legacy contracts retain the generic encoder, while the independent binary reader reconstructs the logical contract and verifies the unchanged canonical SHA. These are adapter measurements rather than NVDLA qualification. | Canonical hashing remains linear in the logical record count. The analyzer graph arrays remain linear, while capture sorting now trades its former complete resident vector for bounded private run I/O. Production MFSPart and non-replicating RePart now defer the semantic contract until final hop refinement; diagnostics and RePart replication retain explicit intermediate evidence. Their provider-native graph populations remain to migrate. NVDLA RSS, wall-time and temporary-I/O qualification for the packed and external-sort changes is still required. |
| W5 physical exporters | **Partial** | One typed physical handoff replaced production `packed-sites.json` and `placement.json`. The production bridge appends normalized clusters directly to a private SQLite artifact; legalization retains only MUX-bearing slices and independent topology validation retains only required MUX/CARRY records. Assignment/site/BEL ownership uses unique constraints, counts, a single ordered scan and bounded per-site state. Source-certificate ownership and full mapped-output driver uniqueness use temporary disk-backed relations rather than million-entry Python maps. Publication occurs only after all checks pass. Lazy typed views feed RWRoute and routed timing; mapped-index v2 owns typed cell pins and RWRoute streams route-cell, physical-binding, endpoint, deterministic safe-name ordinal and clock-candidate rows. The production atomic-source artifact is now a compact selector plus sealed typed sidecar: only macro/cascade/constant facts are stored, while ordinary singleton clusters are lazily derived from the authoritative mapped-index cell/type cursor. The downstream atomic name map is also selector-backed: atom and CARRY8 cascade identities live in a typed sealed sidecar, the selector binds mapped/packed/architecture sources, and production consumers use the lazy logical v2 view. Both historical graph-sized JSON forms require explicit compatibility export. Placement validation now streams native `.pl` coordinates into a temporary file-backed relation and joins them directly to the leased typed name-map sidecar. SQLite proves duplicate, unknown and missing atom coverage, owns normalized site/BEL records and rejects repeated physical slots through a unique occupancy relation. Macro and cascade checks perform bounded instance queries, while one ordered site cursor performs control-set/resource checks. Production writes those validated site groups directly into a versioned content-addressed placement-certificate store instead of constructing and then renormalizing the final cluster tree. The small selector binds exact store identity/counts plus mapped, packed, architecture, name-map, native-placement and optional native-constraint/provider seals; selector-last publication, shared/exclusive leases and post-commit GC preserve complete generations across failures and concurrent readers. The archive retains selector and sidecar atomically, and the physical handoff path rejects inline legacy clusters. Bookshelf net construction now stages fixed endpoint rows in a private SQLite relation, scans them by numeric net and source ordinal, keeps only the active net fanout, and atomically streams `design.nets`; production never materializes the graph-wide endpoint dictionary, net-line list or final net text. A 100,000-cell/100,000-net byte-identical comparison reduced incremental traced peak allocation from 65,399,671 to 1,202,189 bytes and process peak RSS from 311,623,680 to 188,874,752 bytes, while measured operation time changed from 2.204 to 1.377 seconds and both outputs were 4,166,670 bytes. This is isolated adapter evidence, not NVDLA qualification. The final export atom relation is now a private file-backed lexical cursor rather than a Python list of per-atom dictionaries; duplicate ownership is rejected by a unique key, mapped coverage is validated by ordered merge, and every downstream pass reopens the bounded cursor. A 300,000-atom construction plus full traversal reduced incremental traced peak allocation from 112,101,868 to 29,567 bytes and process peak RSS from 257,196,032 to 46,989,312 bytes. It used a 46,149,632-byte temporary relation and changed wall time from 1.41 to 2.98 seconds. The temporary relation is removed after the sealed name map is published; direct promotion into that final store remains a future I/O optimization. This is isolated adapter evidence, not NVDLA qualification. The atomic exporter now derives deterministic `aN` identities from the already sorted atom ordinal instead of retaining an additional instance-to-safe-name dictionary; library/net construction uses one ordered mapped-cell/atom merge without rebuilding an atom dictionary or coverage set, and `design.nodes` is written record by record. A 500,000-atom safe-name comparison reduced incremental traced allocation from 50,920,742 to 2,654 bytes and process peak RSS from 323,371,008 to 212,008,960 bytes, while wall time changed from 0.67 to 0.31 seconds. This isolates the removed identity map; it is not NVDLA qualification. A 100,000-cluster publication comparison reduced retained traced allocation from 108.90 MiB to 0.045 MiB, peak from 110.88 MiB to 2.02 MiB and process peak RSS from 281.81 MiB to 45.27 MiB, with identical 22,085,632-byte typed stores; fsync-inclusive wall time was 1.705 versus 1.953 seconds. Earlier evidence remains: a 100,000-singleton source projection used a 1,073-byte selector and 45,056-byte sidecar with 1.10 MiB traced peak allocation versus 28,500,397 bytes and 167.93 MiB when materialized; a 100,000-atom name-map writer reduced peak from 28.21 MiB to 2.34 MiB; `_collect_atoms` reduced peak from 38.00 MiB to 29.00 MiB; and shuffled end-to-end placement normalization before this final publication change reduced peak from 188.98 MiB to 43.61 MiB. These are adapter measurements, not NVDLA qualification. | Electrical-binding maps, clock-gate qualification subset, parameter lookups and the remaining exporter loop still scale with design data. Temporary atom and endpoint relations are still separate staging stores; the atom relation is copied once into the sealed name-map store rather than promoted directly. Explicit compatibility calls can still materialize the certificate tree. DeviceRegionIndex and pin-aware SLR planning are separate semantic/QoR work. NVDLA wall-time, process-RSS and temporary-I/O qualification remain required. |
| W6 global STA stitching | **Started** | OpenSTA remains the numerical engine, indexed timing readers exist, and Phase 7C no longer expands the whole ScheduleStore before binding. Measurement TSV import validates endpoint order and arc equations in one pass. Production event/check construction now appends interned strings and fixed-width columns to `_IndexedEventChecks`, validates target/runtime and TX/commit coverage online, and creates representative indices without a second complete compression scan. Original-path metadata is a sorted, interned, fixed-width mapping instead of one nested dictionary per path. Canonical report stitching consumes one path bundle at a time. OpenSTA measurement import stores only three fixed-width double columns and reuses the typed check identities. Representative membership uses a fixed-width integer map and exposes original-check measurements through a repeatable lazy sequence, so stitching and TSV publication no longer allocate object/arc lists or a second dictionary list for all expanded checks. A 200,000-row comparison reduced retained traced result allocation from 73,624,528 to 5,205,354 bytes and process peak RSS from 188,334,080 to 83,722,240 bytes; the typed scalar payload is 4,800,000 bytes. Final OpenSTA stitching now stores five fixed-width result columns over the existing path metadata and computes WNS/TNS with bounded compensated partials. Phase 7C publishes the authoritative rows in a content-addressed typed QoR owner, while `qor_report.json` is a bounded selector and compatibility reads reconstruct the unchanged logical report explicitly. On 100,000 paths, result construction reduced traced peak from 69,804,657 to 4,112,523 bytes and process peak RSS from 263,176,192 to 150,814,720 bytes; the build took 1.31 versus 0.56 seconds. The selector plus typed owner used 8,983,131 bytes versus 45,900,216 bytes for the former JSON and took 1.79 versus 0.68 seconds including fsync and validation. Logic-segment producers now write a content-addressed typed store directly; selectors and physical summary bind route, schedule and Phase 3 logical seals. Boundary identities, boundary timing and local-path timing are leased typed stores, and physical-summary readers resolve each FPGA lazily. A leased bounded multi-FPGA ordered merge validates global segment/path-key uniqueness and supplies Global STA/system timing without the former `path -> member -> segments` tree or per-member/per-FPGA queries. Archive retention follows the complete summary/selector/store group. Logic-segment query preparation now spools ordered rows on disk and atomically publishes the query with its content-addressed identity selector. Measurement import streams into a temporary typed relation, proves exact coverage and equations with set joins, and writes the final timing store directly without per-row identity queries. Interleaved legacy input remains a checked compatibility fallback and WNS/TNS still count every original path once. Local-path timing producers now publish ordered typed rows directly, and validation projects identity fields through a repeatable lazy sequence while accumulating statistics in one pass. A 100,000-path typed-owner comparison reduced process peak RSS from 251,887,616 to 32,653,312 bytes and artifact bytes from 30 MiB to 24 MiB, while read and full seal validation changed from 0.73 to 0.90 seconds. | String interning, path IDs and representative identity tables remain linear in unique checks/paths, and the legacy interleaved fallback still requires graph-sized Python work. Compatibility callers that use generic `read_json` intentionally reconstruct the full logical path list; production summary consumers must use the leased QoR view. Other per-FPGA evidence remains to migrate. NVDLA Phase 7C wall/RSS/I/O qualification remains required. |
| W7 native production entry | **Started** | The installed `emuflow-native` executable now dispatches the real Phase 3 Static Exact analyzer, Phase 4 TLR router, Phase 5 exact scheduler and Phase 6 Chimew signal-grouping, position-refinement, RUDY and bank/channel assignment kernels in-process through one linked stage library. All seven production adapters resolve the unified entry directly without requiring a redundant standalone executable; explicit comparison overrides remain standalone, and older installs fall back to them. Differential checks require byte-identical output. | There is no complete `emuflow-native compile` path yet. Phase 1/2/4/6/7 orchestration remains in Python, and Python production memory can still scale with routes, endpoints, and timing paths. Move stage ownership into this entry incrementally rather than delegating `compile` back to the Python CLI. |

An October 23 live-process sample corrected the immediate Phase 3 diagnosis.
Two still-running NVDLA PATRON jobs at earlier series commits (`68958333` and
`5be3f127`) had elapsed 1:21:34 and 1:34:16 with 99.5--99.7% CPU. Their Python
high-water RSS was 7,974,808 and 8,076,304 KiB rather than the former 42.7 GiB,
but each process had only the 1,209,520,128-byte indexed TimingPathDB open.
Over ten seconds its SQLite file position advanced about 1.20 MB and 0.24 MB,
with no additional writes. This is live hotspot evidence, not terminal or
exact-HEAD qualification: retained-memory work has succeeded, while the
TimingPathDB-to-pressure projection remained CPU-bound.

Production compact construction now attaches TimingPathDB v3 read-only to the
compact EmuIR connection and uses ordered SQLite joins to resolve path nets and
launch/capture instances directly to fixed-width source-net, pressure-net and
cluster ordinals. It avoids a Python dictionary per timing path and two UTF-8
binary searches per path-net incidence. On a same-input synthetic case with
100,000 paths, 800,000 incidences and 20,000 compact nets, the old adapter took
7.31 seconds and the joined adapter took 1.68 seconds (-77.0%). Both emitted an
identical 17,560,024-byte packed path payload; process peak RSS remained in the
same roughly 78--90 MB range. The runtime report records
`pressure_indexed_sql_projection=1`, so the next NVDLA replay can prove that it
exercised this path.

The October 22 gap review changes the implementation order. Exact-HEAD NVDLA
profiling now gates the remaining Phase 3 provider/native-model work. The next
structural boundary is Phase 4: its C++ search still receives a complete
Python-built demand/path model through text and Python reconstructs all native
route/candidate output before RouteStore publication. Typed Global STA binding
then removes the upstream `route_paths`/`scheduled_hops` object graph that is
still built before the packed check writers. Direct native ScheduleStore
publication follows. W0 grows only to support these concrete boundaries, and
W7 becomes the final production-entry consolidation rather than an early
wrapper milestone. Phase 6 qualification maps and the remaining Phase 7
exporter maps follow those structural gaps. Further local OpenPARF
staging-store cleanup is deferred unless NVDLA stage profiling shows it on the
critical path.

The TritonPart initializer now preserves compact Phase 3 ownership through
input construction. Canonical cluster order and vertex weights are lazy views
of the packed cluster columns, legal hyperedges are built from ordered compact
endpoint ordinals into fixed-width CSR-style storage, and both hMetis variants
plus fixed/edge-class files are streamed atomically. Deferred candidates keep
the compact instance-owner view so cut discovery uses the ordered SQLite
endpoint scan rather than recreating an instance-name dictionary and decoding
the graph. The independent balance check now computes totals and assigned
loads in one pass. A 200,000-hyperedge representation comparison used
8,372,861 traced bytes (7,888,906-byte payload) instead of 147,099,118 bytes;
a separate 200,000-cluster balance comparison reduced traced peak allocation
from 56,891,500 to 22,376 bytes. Logical records and generated input bytes are
differentially identical. These measurements do not replace the required
NVDLA replay.

The following topology hop refinement boundary now remains compact as well.
One ordered endpoint scan emits directed net membership into packed ordinal
columns, the native input writer streams every record atomically, and ordered
native `ASSIGN` records become one fixed-width part column. A 200,000-net
representation comparison reduced retained traced allocation from 107,113,418
to 9,007,799 bytes (8,488,906-byte payload). A separate 200,000-cluster
assignment comparison reduced retained allocation from 31,886,472 to 845,764
bytes (800,000-byte payload). Differential tests require byte-identical native
input and identical refined ownership. NVDLA remains the qualification gate.

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
indexed views. Assignment index/storage v4 applies the same ownership rule to
the complete cut-record population. The JSON selector omits `cut_nets`; its
descriptor binds the typed rows by schema, count and canonical record digest.
The writer normalizes each cut once and inserts bounded batches into the cut,
sink and predecessor relations. Readers merge three ordered cursors and verify
the seal without retaining the relation.

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
graph arrays and the capture sort's temporary I/O. The eligible-name policy no longer adds a second
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
Canonical digest construction now consumes the same typed relation without
replaying logical dictionaries. The C++ analyzer also writes discovered
captures through bounded sorted runs and a bounded-fan-in merge rather than a
complete capture vector. Production cut construction now retains one UTF-8
identity pool plus fixed-width ownership CSR, endpoint-count and round columns;
the analyzer, contract builder, validator and publisher consume lazy logical
records without a second net-name list. A 200,000-record comparison reduced
retained traced allocation from 85,428,138 to 10,995,827 bytes and process peak
RSS from 246,202,368 to 35,536,896 bytes, with the same canonical SHA-256;
construction plus full traversal changed from 1.82 to 3.74 seconds. The
Production MFSPart and non-replicating RePart now defer semantic-contract
construction until final hop refinement has fixed the assignment; diagnostics
and RePart replication keep their explicit intermediate evidence. The
remaining Phase 3 work is the provider-native graph population plus the
analyzer's graph arrays. These isolated
results are not an NVDLA stage-level claim. A separate 1,100,000-way duplicate
fan-in exercised cross-run deduplication and produced the same one-record
canonical output without leaving private runs.

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
| 3 | Assignment JSON, SQLite, and exact relations previously repeated the same graph. | Assignment-index v4 owns normalized cut records, sinks and predecessors; assignment-storage v4 publishes only their sealed descriptor, and Phase 4--7 consume a repeatable ordered cursor. `assignment.exact.bin` remains the Static Exact segment/capture data plane. Native predecessor CSR and fixed-width launch/level/depth arrays replace four analyzer-side Python maps. Capture protocol v3 returns fixed-width original net/sink ordinals. The production adapter streams compact cell/net/integer-endpoint rows directly into native input, validates cut ordinals/classes with an ordered merge, and resolves capture coordinates against one ordered sink cursor. It therefore avoids the extra instance-name ordinal map, decoded endpoint graph, per-cut random queries and per-capture whole-net decoding while preserving name-only compatibility. Compact clustering separately uses ordered integer cell/endpoint cursors and fixed-width union-find state, so the final clusters are no longer built beside instance/name/group graph copies. Exact-binary v2 provides bounded canonical capture-bit storage with v1 read compatibility. Production PATRON derives all four semantic relations as repeatable byte-backed streams. The exact-binary writer consumes the native predecessor CSR plus raw cut/capture bytes directly, checks them against the canonical record view, and avoids cut-name/hash and dependency/logic-record expansion while preserving byte identity. The native analyzer externally sorts capture coordinates in bounded runs, deduplicates before and during a bounded-fan-in merge, and streams the final run into atomic output. Combinational-candidate characterization and PATRON pressure construction use ordered compact endpoints, temporary arc deduplication, fixed-width CSR/membership arrays and streamed native input. Cluster publication stores cluster membership, resources, constraints and the eligible-net policy once in sealed compressed typed columns; its JSON boundary is a small selector and no longer contains the graph-sized logical v1 payload. The production policy writer walks the packed lexical candidate view without retaining a second Python string list. Cut construction keeps the same identities and ownership in compact columns and projects final Static Exact records lazily. Explicit compatibility readers can still materialize logical v1; provider candidates and analyzer graph arrays remain pending work. |
| 4 | The former `routes.json` repeated demands and routed records inside one graph-sized JSON document, forcing Phase 5 to parse and retain it before scheduling. The global-router adapter also rebuilt up to seven candidate trees per demand, then created path/demand/domain sets to reconstruct the native refinement batches. | `routes.json` is now a small selector for a content-addressed typed RouteStore. Demand records are projected from their authoritative routes; sinks, edges, predecessors and timing paths are normalized once. RouteStore user version 2 moves master selections and refinement-batch path membership out of the JSON header into typed relations with lazy logical views. In managed production, candidate output is checked record by record for tree shape, sink coverage, predicted delay, hop limit, direction locks and refined-final identity. Only one capacity-domain bitset per demand survives parsing, and fixed-width CSR reconstructs the independent conflict-batch certificate. Full candidate-pool materialization remains an explicit diagnostic/export path. The production TLR kernel is linked into `emuflow-native phase4 route`; explicit comparison tools remain standalone and older installs retain a checked fallback. Phase 4 and the final flow report inventory the sidecar, and legacy `read_json` remains an explicit materializer. A synthetic 50,000-demand/350,000-candidate parser comparison reduced process peak RSS from 252,018,688 to 45,809,664 bytes; online validation changed parse time from 0.606 to 1.148 seconds. This is isolated adapter evidence, not NVDLA qualification. |
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
