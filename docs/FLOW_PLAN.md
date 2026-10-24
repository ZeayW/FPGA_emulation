# Architecture and phase contracts

EmuFlow separates logical compilation, board-level communication, per-FPGA
physical implementation, and global timing analysis. Providers exchange
versioned artifacts rather than sharing tool-internal state.

## Data flow

```text
RTL + constraints + mapping profile
  -> mapped EmuIR and resource summary
  -> original TimingPathDB and optimization weights
  -> clusters and FPGA assignment
  -> board-level routes
  -> TDM ratio/lane/slot assignment
  -> per-FPGA netlists and transport RTL
  -> physical placement/routing and delay bindings
  -> whole-design global timing
```

## Canonical inputs and outputs

| Boundary | Canonical information | Consumer responsibility |
| --- | --- | --- |
| Frontend -> timing/partitioning | EmuIR connectivity, primitive parameters, resources, clocks, identities | Use the mapped circuit; do not reinterpret it as a contest communication graph |
| Timing -> partitioning/routing/TDM | Original-path identities, clock constraints, timing weights | Preserve the relationship between optimization representatives and original members |
| Partitioning -> system routing | Assignment, cut endpoints, BoardDB, route constraints | Choose legal source-to-sink communication paths |
| System routing -> TDM | Ordered routes, directions, link demand | Allocate ratios, lanes, and slots without changing route identity |
| TDM -> splitting/transport | Schedule-entry identities and event semantics | Bind logical boundaries and generate matching transport RTL |
| Splitting -> physical implementation | Per-FPGA netlists, constraints, device/architecture inputs | Account for every cell and enforce physical resource/ownership legality |
| Physical implementation -> global STA | Logic/interface delays, original-path bindings, concrete schedule, link timing | Check event readiness and launch-to-capture timing for the declared population |

Phase 5 has one production provider, the native Static Exact dependency
scheduler. It consumes the typed RouteStore and the Phase 3 semantic binding,
writes one typed ScheduleStore, and publishes its compact selector and report
only after independent validation. Full compilation rejects retired TDM
providers and registered-boundary mode before frontend work. Minimum-frame
search varies the frame bound using that same engine; it is not another TDM
provider. Hardware ratio/bank constraints and Chimew's lane/slot consumption
remain part of the supported contracts.

One owner supplies each large population; reports summarize it rather than
embedding duplicate netlists, maps, or timing-path trees.

The optional native one-shot controller executes this dependency sequence with
isolated shared phase adapters. It exchanges bounded configuration, artifact paths
and report projections; each phase releases its working state before the next
process starts. The transitional Python data planes remain explicit. See
[native compile control](USER_GUIDE.md#native-compile-control) for implementation
selection and unsupported modes, and the [migration contract](PHASE3_NATIVE_MIGRATION.md)
for the remaining native ownership requirements.

## Logical model

EmuIR retains directed endpoints, hierarchy, cell types and parameters,
resource vectors, clock/reset classification, and cut eligibility. Logical
schema identity is separate from its physical storage representation.

Phase 6 copies the bounded `resource_reservations_by_fpga` seal directly from
the Phase 3 assignment index into its split manifest. Its independent validator
compares that seal with the assignment index, rejecting missing or changed
reservations before the physical backend consumes the split. This control
metadata check does not load the instance ownership graph.

BoardDB describes FPGA nodes, resource budgets, and inter-FPGA links. An
academic BoardDB can omit package pins. Concrete deployment additionally
needs authoritative device resources, physical connections, electrical rules,
reference clocks, reset handling, and a board shell.

TimingPathDB identifies source logical paths and endpoints. Physical binding
must keep same-FPGA and cross-FPGA paths disjoint and preserve each original
member exactly once in the declared global population.

## Boundary semantics and timing

Sequential-only cuts retain unsupported combinational connectivity as atomic
partition units. Generalized Static Exact releases structurally supported
combinational boundaries under its explicit synchronous semantic contract.
See [Static Exact](STATIC_EXACT_COMBINATIONAL_CUT.md).

Phase 3 enforces structural capacity, fixed/group, SCC/cascade, topology, and
hop constraints. It may optimize predicted delay or TDM pressure, but does
not certify future slot readiness or physical deadlines.

Phase 4 chooses system routes. Phase 5 assigns concrete TDM resources/events.
Phase 6 materializes transport and checks macro-cycle semantics. Phase 7
provides physical delays and the global timing result. The
[contest/Phase 6 contract](CONTEST_PHASE6_INTEGRATION_CONTRACT.md) defines the
ownership of route and schedule fields.

## Physical providers

- The open academic route uses VPR packing/routing and OpenPARF placement.
- The RapidWright route uses device facts and RWRoute, with native OpenPARF
  responsible for placement and legalization.
- The optional Vivado route uses the proprietary device implementation tools.

A bridge translates identities and validates the provider result; it must not
silently choose a different placement or architecture. Resource, macro,
cascade, clock-region, and SLR requirements must be admitted explicitly.
Unsupported requirements are errors, not fallback opportunities.

## Scope

A physical flow must retain source timing identities and explicit clock periods
even when optimization is timing-oblivious. OpenSTA is the default global
numeric timing engine; its model consumes physical segment/interface and
transport-event bindings rather than precomputed Python slack.

Academic physical models and board-link models are not measured silicon or
board signoff. Bitstream generation and hardware operation require a compatible
concrete platform. See [timing scope](global-opensta-qualification.md) and
[validation requirements](VALIDATION.md).

## Shared native core (W0)

W0 provides the common infrastructure used by the phase migrations. Its public
boundary is `ArtifactRef → Store/View → Writer → Summary`; the internal API is
C++17. Match headers and library revisions when linking. Versioned artifact and
request protocols are the inter-process compatibility boundary; STL objects,
SQLite handles and circuit-sized Python containers do not cross it.

| Contract | Shared owner | Required behavior |
| --- | --- | --- |
| Build closure | `libemuflow_core`, `emuflow_core/sources.txt` | CMake, native fixtures and thin runtime deployment consume one checked source manifest; compatibility tools link the same facilities. |
| Schema and source binding | `SchemaRegistry`, `ArtifactRef`, `SqliteStore` | Explicit versions, registered logical envelopes, immutable generations and mixed-source rejection; bounded metadata checks rather than integrity scans or recounts. |
| Identity and order | `ArtifactScope`, `ArtifactId`, typed views | Cell/net/path/endpoint/FPGA/link identities retain their artifact namespace. Route source sequence and canonical net order are distinct coordinates. |
| Storage and publication | SQLite connection/statement leases, `SqliteArtifactWriter`, `AtomicBinaryWriter` | Last borrowers retain connections and private staging. Close connections before retirement; reject active transactions/statements, input aliases and changed sources before atomic publication. |
| Common data structures | `ResourceView`, integer CSR helpers, source string/payload pools | Deterministic record order, bounded resource decoding and explicit extraction of kernel-local integers; no second persistent graph catalog. |
| Failure and measurements | `StageError`, `RunStage`, `StageResult` | Shared error codes and bounded reports; wall/CPU/I/O intervals and process lifetime RSS retain their stated scopes. |
| External engine lifecycle | `RunExternalTool`, `ToolRequest`, `ToolResult` | Literal arguments/environment, bounded diagnostics, limits, deadlines, cancellation and reaping; engines remain separate processes. |

`EmuirView` reads indexed v1 and compact v2 directly. Explicit v1 reads derive
counts because that historical format has no count manifest; compact production
v2 construction reads bounded metadata. Both expose the same cell-state and
connectivity semantics. Interned pin IDs exist only in v2; validated v1 pins
retain their original port/bit fields without inventing an integer string pool.
No read silently rewrites or upgrades its input.

`AssignmentView` supports public assignment v1–v4 and checks primary coverage as
its ordered copy cursor is consumed. `SplitView` supports split v2, with scoped
partition/cell/net/pin coordinates, replica names, top endpoints and synthetic
shadow identities. References to upstream source sequences require an explicitly
paired source artifact; equal integers never prove a cross-store identity.
`RouteView` and the native ScheduleStore producer use the same ownership and
publication facilities. Candidate construction, primary clustering and PATRON
joins borrow the existing `EmuirView` store, sharing its source connection/cache
and lifetime. The schema registry also names mapped/physical handoffs
and boundary, logic-segment and local-path timing stores. Their phase-specific
readers and independent electrical/timing validators retain semantic authority.

W0 closes only after versioned round trips, deterministic ordered records and
negative cases pass: unknown versions, truncation, duplicate keys, orphan or
mismatched references, mixed generations, active SQLite sidecars, interrupted
publication and borrowed cursor lifetime. The same frozen NVDLA input must
establish equivalent records and measure the pure integration overhead against
the prior implementation; the permitted regression is at most 5%. Keep hashes
and measurements outside production handoffs. Fixture/API gates alone cannot
establish the large-design performance gate.

W0/W7 closure also requires retirement of superseded execution paths. Once the
replacement has passed the required equivalence and performance gates, remove
the old production entry, configuration switches, deploy/build targets and
unconsumed protocol readers/writers together. Historical rollback uses the
matching Git revision. A schema version is retained only for a current producer
or an identified artifact consumer, not merely because it was once published.
Keep independent correctness oracles in validation code without preserving a
second production flow. Migrate remaining consumers before deleting their
entry; a test of an obsolete wrapper is not a reason to keep that wrapper.
Native production resolution requires the unified entry and does not recover
an incomplete selected installation by borrowing another tool installation.

W0 completion does not mean the remaining phase algorithms, Python data planes,
advanced compile controls or physical/global timing qualification have migrated.
Those retain the W1–W7 ownership and acceptance requirements. An infrastructure
closure must not be reported as completion of the whole native production flow.
