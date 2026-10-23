# Native data-plane migration

## Architecture and ownership

The production target is one `libemuflow_core` and an `emuflow-native compile`
entry that owns execution of the complete flow. External Yosys, OpenSTA,
OpenROAD/TritonPart, OpenPARF, RapidWright/RWRoute and optional Vivado remain
separate engines. OpenPARF may retain its required thin Python/PyTorch host.
A native command that only invokes the Python compiler is not this target.

The current native executable links real Phase 3, 4, 5 and 6 kernels, but its
stage dispatch is only a foundation. Complete native orchestration and the
remaining production data-plane ownership are unfinished. W0 shared-core
consolidation and W7 native compilation are the priority architectural delivery;
stage adapters must converge on those interfaces rather than add independent
parsers, identity maps, error formats and publication implementations.

See [phase contracts](FLOW_PLAN.md) for the whole-flow architecture,
[algorithm implementations](ALGORITHMS.md) for providers, and
[physical acceptance](XILINX_PHYSICAL_FLOW.md) for backend requirements.

## Shared core (W0)

The core owns representation-scoped typed IDs, checked artifact I/O, common
CRC-32/SHA-256, SQLite statement/transaction lifetimes, and atomic publication.
Complete consolidation additionally requires one versioned schema registry,
artifact/view API, structured error protocol and runtime-metric contract across
all production stages. An integer identity is meaningful only with its owning
artifact; equal integers in different stores are not interchangeable.

SQLite owns relations requiring joins, uniqueness or bounded lookup. Large
ordered populations may use typed binary sections or ordered record streams;
they must not be expanded into SQLite solely for container uniformity.
Compatibility JSON is an explicit exporter, not a second authoritative owner.

Normal handoffs verify schema, source identity, required row counts and circuit
semantics. They do not run SQLite `quick_check` or `integrity_check`. Strong
hashing belongs to publication/import and explicit terminal verification,
not repeated routine reads of already validated immutable artifacts.

## Native production entry (W7)

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
| Phase 3 | Compact clustering/pressure views; native search and semantic/capture production; native exact-binary writer | Migrate cluster/pressure production owners and remaining assignment publication; qualify complete stage costs |
| Phase 4 | Native routing; typed RouteStore/results | Remove remaining graph-level adapter work and integrate complete native stage lifecycle |
| Phase 5 | Native exact scheduler; typed ScheduleStore and cursor validation | Complete producer ownership and explicit non-exact/compatibility interfaces |
| Phase 6 | Indexed split/transport/binding owner; streamed RTL/XDC | Complete native producers and adapter queries while preserving electrical contracts |
| Phase 7 | Indexed lowering; typed physical handoff and native Bookshelf pin export | Complete remaining backend/export/validation ownership and full physical qualification |
| Global STA | Typed segments/results and bounded identity/stitching views | Native check/input construction, result import and ordered complete-path stitching |

Native computation does not eliminate algorithmic search cost. Compare search,
imported-solution and frozen-output replay as separate execution modes. Porting
an adapter must not change the algorithm budget or weaken independent legality.

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
