# EmuFlow

EmuFlow is a research-oriented multi-FPGA emulation flow. It compiles
synchronous RTL into per-FPGA netlists and a statically scheduled communication
fabric, with explicit interfaces between synthesis, partitioning, system
routing, TDM assignment, physical implementation, and global timing analysis.

The default research route uses RapidWright device resources, native OpenPARF
placement, and RWRoute routing for supported Xilinx FPGAs. Vivado is the other
maintained physical backend and requires an external proprietary installation.
Optional container launchers support native-tool ABI and Tcl script bindings,
with runtime selection separate from algorithm settings. See the
[build instructions](docs/USER_GUIDE.md#complete-build).
OpenSTA is the sole production global physical timing engine; the former Python
engine and historical dual-engine replay are retired. Numerical oracles remain
bounded test utilities. OpenSTA container evidence identifies the native engine
and launcher separately.
RapidWright endpoint binding consumes the mapped index and single typed physical
handoff in the shared C++ core. One native graph produces the typed segment
timing stores; the former Python endpoint and graph builders are test-only oracles.
Routed OpenSTA staging writes compact EmuIR and delay-cell Liberty directly
in the shared core, with source-bound model queries and independent A/Y checks.
Its run report retains the bounded model-owner coverage reference.
Indexed OpenSTA input export writes timing Verilog and the net map in the shared
core from ordered compact EmuIR cursors, preserving source-bound clock polarity.
The indexed Vivado adapter consumes current Phase 6 SQLite contracts with
canonical net identities, fixed LVDS I/O, native sequential control pins, and
the common physical-result contract. Real-device qualification is in progress. Its OOC implementation retains abstract external clock sources and
does not establish board clock/skew signoff. Used undriven source nets are
rejected before mapping or vendor implementation. Physical capacity accounting
handles native macro expansion and shared LUT halves.
Vivado tool censuses are imported by the shared native core into one sealed
lineage/placement store. Python passes file references; imported raw snapshots
are retired. Source lineage still does not prove functional equivalence.
Indexed mapped primitives carry provenance tags without global KEEP/DONT_TOUCH
constraints. The shared core emits their RTL and source-function index directly
from ordered mapped cell/pin cursors; source-bound coverage is checked after optimization. The timing
contract requires an explicit measured or proven optimized-away disposition for
every original path. Elimination-proof consumption by terminal global timing
is still incomplete; unresolved original members fail coverage. See the
[Xilinx physical flow](docs/XILINX_PHYSICAL_FLOW.md#vivado-indexed-provider) and
[user guide](docs/USER_GUIDE.md#physical-implementation).
The virtual BoardDB is a declared academic platform model rather than evidence
of a manufactured board or hardware signoff. See the
[hardware platform model](docs/HARDWARE_PLATFORM.md),
[Xilinx physical-flow contract](docs/XILINX_PHYSICAL_FLOW.md), and
[backend configuration](#configuration-and-limits).

## Continuing from completed indexed inputs

`multi-fpga compile --prepared-frontend PHASE1_DIRECTORY --prepared-timing PHASE2_DIRECTORY`
continues from validated Phase 1 and Phase 2 boundaries without rerunning
synthesis or pre-partition STA. The continuation validates the saved design,
mapping, clock, timing-path, and SQLite contracts, then uses the currently
selected BoardDB for Phases 3–7. See the
[user guide](docs/USER_GUIDE.md) for input requirements and failure behavior.
The indexed frontend preserves constant child-module outputs across hierarchy
boundaries, including consumers visited before their drivers.
The saved BoardDB supplies only the mapping device identity; older link-model
fields do not need migration to continue with the current BoardDB.
Terminal validation also verifies the declared read-only EmuIR binding and
hashes its original bytes without copying the database.

Phases 3–7 exchange compact selector manifests and content-addressed SQLite
sidecars for assignments, routes, schedules, transport, and physical handoffs.
Production readers stream typed indexed relations; whole-graph materialization is
an explicit compatibility operation. SQLite handoffs explicitly enable read-only
input URIs and encode filesystem paths across supported runtimes. A continued run uses the current
BoardDB and does not inherit an older interconnect configuration from the saved input.
Mapped physical pin indexes are built in bulk before their atomic publication.
Indexed OpenPARF Bookshelf export streams typed pins through the shared native
core and `emuflow-native phase7 export-bookshelf`, without reconstructing the
cell, pin or per-net fanout population in memory.
Physical macro derivation is shared within one immutable mapped reader;
independent artifact readers still derive and validate their own contracts.
The native SLL weight update follows LEAPS' fixed-step Adam equation with
`t_psi=0.002` and the paper's current-delta EMA weight `rho=0.9`. Absolute
SLL-count changes feed the EMA and moments once; the update no longer depends
exponentially on the absolute change in SLL count.
Validation uses an isolated one-shot compile and compact terminal evidence.
The persistent `experiment-cache`/`experiment-stage`, `validation-farm`, archive
and checkpoint-only QoR comparison chains are retired. Current flow validators,
benchmark matrices and Chimew remain; see the
[validation lifecycle](docs/VALIDATION.md#experiment-lifecycle).

The native migration keeps the large Phase 3–7 relations in typed, indexed
storage and streams them through unified native entry points. Phase 6 publishes
virtual wiring, binding, differential-I/O, and XDC contracts in the sealed
transport store; Phase 7 reads those contracts lazily. See the
[Phase 3 native migration](docs/PHASE3_NATIVE_MIGRATION.md) for the storage
and migration contracts. Native operations now share a typed C++ request/result
API, registered artifact envelopes, file-generation checks, and a structured
`--report` error/metrics contract. Phase 3 analysis consumes the shared compact
EmuIR view with artifact-scoped record IDs and ordered connectivity traversal.
PATRON production supports only v14. Earlier search versions, their native
execution/protocol/certificate branches and the old reference-search CLI are
retired; independent current-model and objective validation remain.
PATRON search uses this unified entry; its separate executable and legacy text
input are retired. Explicit input copies use the same checked typed binary
protocol as streamed input.
Compact production clustering uses the shared core for connectivity union,
canonical membership and resource reduction, with read-only columns borrowed by
Python consumers; their mapping owner also controls temporary-file retirement.
Structural candidate eligibility and final cluster catalog serialization also run
in the shared core. The catalog consumes original primary/candidate artifacts;
assignment packing reads ordinals without expanding member/resource records.
Remaining semantic/index orchestration still has Python work; this is not
complete Phase 3 native ownership.
Compact indexed PATRON v14 production builds its pressure model directly in
the shared core from EmuIR, TimingPathDB and the original native primary store;
it avoids a second membership/resource snapshot and transposed resource matrix.
Python carries configuration and packed initial ownership, not a pressure graph.
The old snapshot-based native model command is retired; direct model construction
requires the shared primary artifact and rejects missing ownership.
Greedy partitioning is a small test oracle only; it has no production entry.
RePart and MFSPart production routes, post-refinement, executables and build
dependencies are retired. Their post-refinement proofs are not accepted by the
current validator; historical runs require their matching Git revision.
Explicit TritonPart solution imports validate the current cluster contract
without requiring an unused OpenROAD executable.
Assignment summaries and independent capacity/balance validation also reduce
the shared primary columns in C++, without decoding cluster member records.
Assignment publication consumes final native cut records directly, carrying only
the compact endpoint-count column through Python. Independent shared-core cut
validation merges typed rows and computes their logical hash without host record
expansion; exact-binary checks remain separate. See the
[publication and validation contract](docs/PHASE3_NATIVE_MIGRATION.md#assignment-index-publication).
Compact exact captures are produced and independently checked in the shared
core before publication, without Python capture replay or per-record lookups.
The superseded `resolve-captures` entry and persistent capture seq index are
retired. Shared binary and SQLite writers check bound input generations
immediately before atomic replacement, including native routing, scheduling
and physical exporters.
SQLite adapters and typed record owners release connections and mappings before
publication or temporary-file retirement, including borrowed native cursors and
lazy physical and timing-store queries.
The shared native store API covers EmuIR v1/v2, assignment v3/v4 and split v2;
artifact-scoped identities and one build manifest bind its readers and writers.
Current upstream EmuIR, assignment, route, schedule and timing/weight stores,
plus Vivado function/proof and final QoR stores, use the shared publisher.
Transport buffering and routed OpenSTA normalization finish in the private
EmuIR generation before its atomic publication.
STA selectors reference immutable payload generations so a rejected selector
commit preserves the previous result.
Phase 6 split/transport, position/pin-plan and Chimew input/binding/report stores
use the shared publisher, which adopts closed SQLite staging without copying it.
Phase 7 mapped indexes, placement IR, atomic sources/name maps, placement
certificates, physical handoffs and boundary/logic-segment/local-path timing
stores use that same publication boundary and preserve bound input generations.
Transport selectors and returned views share one file seal within the producer
invocation; indexed Chimew inputs can be republished without restoring a Python graph.
Cluster, timing-path, logic-segment identity and Phase 6 auxiliary selectors share
a commit helper that preserves prior publications on failure and retains recovery
files if rollback fails. Native primary cluster columns require their current v2
protocol; the unused v1 registration is retired.
Assignment v1/v2 readers and the old JSON-to-v2 migration writer are retired;
historical artifacts require their matching Git revision.
Thin deployments borrow only maintained tool dependencies; old installations
cannot reintroduce retired provider programs or replace the rebuilt native entry.
Native clustering and PATRON joins share the live EmuIR reader's store.
See the [W0 contract and acceptance gates](docs/FLOW_PLAN.md#shared-native-core-w0).
The [publication qualification](docs/VALIDATION.md#native-store-publication-qualification)
compares the complete Phase 6 API with fixed inputs and algorithms. Offline
full-record seals allow sequential comparison without retaining both arms' payloads.
Production native stages require `emuflow-native`; an incomplete installation
does not select an older standalone tool. `EMUFLOW_NATIVE_ROOT` selects one
authoritative installation. Bookshelf export uses only the unified command.
Phase 4 supports only `timing-aware-global-candidate-v1` through the unified
entry. Retired routing providers, their standalone executable and dedicated
comparison commands are removed; candidate-generation kernels remain shared.
The standalone TDM/physical feedback loop, its CLI/API inputs and native `PRICE`
records are retired. Native congestion history, timing and TDM costs remain.
Production RouteStore sealing reads typed rows in the shared core, preserving
logical/section hashes without Python route-tree or candidate-list expansion.
AMF and DREAMPlaceFPGA feasibility adapters, native runners and the unused
candidate comparator are retired. The maintained Xilinx implementations are
OpenPARF/RWRoute and Vivado. Production RWRoute export accepts only a mapped SQLite
index and one typed physical handoff. The OpenPARF bridge also publishes only
that single store from a typed placement certificate; separate packing and
placement JSON output is retired. Indexed physical handoffs independently check
complete cell ownership, typed pin coverage and output-driver uniqueness in the
shared native core. Typed atomic sources and placement certificates are compared
directly in mapped-name order, without copying ownership into temporary tables;
local FF, site/BEL and macro checks remain required.
The separate carry-only adapter/bridge is
removed; CARRY8/LUT6_2 use the maintained mixed atomic backend. OpenPARF exposes all
qualified sites; its density target does not remove physical sites or impose a
second 75% clock-region cap. The old JSON exporter
is a noninstalled qualification oracle; it has no production API or fallback. Chimew consumes
their indexed Xilinx owners.
The VTR/open execution route, engine source, standalone adapters and build
options are retired. Shared site/BEL and handoff validation remain. See the
[backend contract](docs/XILINX_PHYSICAL_FLOW.md#backend-contract).
RapidWright uses only native OpenPARF placement. The legacy placer selector,
packed-cluster guidance flow and single-SLR planner are removed. The early
`phase2` placement spike, generic `placement` commands and standalone
`vpr fpga-open/full-open` flows are also removed; current OpenSTA Phase 2,
native OpenPARF placement and Chimew lookahead retain their shared kernels.
The obsolete `phase7d`/`phase8a` release chain that required the placement spike
is retired; current multi-FPGA BSP and fixed-I/O validation remain.
The multi-FPGA upstream uses OpenSTA for Phase 2. The independent Vivado timing
producer and old whole-EmuIR physical flow are retired; Vivado implementation
consumes the current indexed Xilinx input and complete timing queries. Old
standalone vendor STA map/import commands and provider policy are also removed.
The typed mapped/physical handoff also feeds a shared-core RWRoute exporter;
Python sends file references and board I/O configuration. Native export uses a
compact cell catalog, sparse clock-net reductions and one live net of endpoints.
See the
[physical export contract](docs/XILINX_PHYSICAL_FLOW.md#native-rwroute-export).
The [offline handoff differential](docs/VALIDATION.md#primary-store-handoff-differential)
can use the normal TritonPart initializer while keeping capacity/balance bounds.
The Phase 3 topology hop legalizer runs through the shared
`emuflow-native phase3 refine-hops` operation, preserving its algorithm and
independent hop audit. Production keeps assignment and counters; explicit
`--trace-output` retains diagnostic moves. The standalone executable is retired.
Current indexed Static Exact Phase 5 runs use one shared-core stage for model
construction, scheduling, readiness certificates, independent validation,
timing projection and report-last generation publication. Python supplies bounded
configuration and artifact references; it no longer assembles the phase report
or commits the output generation. Independent native
validation reconstructs events and certificates directly from the typed stores
before report publication, with no Python route/hop replay. Selection depends on
the input contract, not its route count. Native timing projection produces the
pre-physical summary and a [compact v3 cross-layer contract](docs/FLOW_PLAN.md#phase-5-indexed-timing)
without Python path/hop trees or a second path-binding payload. These operations
require the unified entry; compilation checks that capability before synthesis.
Production ScheduleStore reads and writes only storage v4; retired storage
versions and the Python snapshot publisher are removed from the installed API.
The logical schedule and typed relation schemas remain unchanged.
Phase 5 exposes only the Static Exact dependency scheduler. Independent
ratio/KKT, timing-DAG and slot optimizers, the old cross-stage feedback flow,
and their CLI/build entries and the unused Python `cp-sat` extra are retired.
The shared solver, hardware ratio
constraints, independent validators and optional Phase 6 Chimew remain.
Phase 6 maintains baseline transport and optional Chimew. The independent
placement-aware planner, its executable/CLI and its comparison arm are retired;
shared pin/electrical validation remains. Static Exact analysis and the four
Chimew kernels run only through `emuflow-native` and `libemuflow_core`; duplicate
standalone programs and their build switches are retired. Chimew grouping,
refinement and bank assignment accept only the current typed input protocol.
Chimew file outputs use the shared atomic writer with source checks before
publication; pipe outputs remain streamed and require successful process
completion. Fixed-board adapters support sealed LVDS P/N channels; the
lookahead uses the selected Xilinx backend and source-qualified package geometry.
Shared device site maps carry a producer content seal; legitimate hard-link
publication is distinguished from changed content. See the
[Phase 6 interface](docs/USER_GUIDE.md#phase-6-providers).
These production outputs stay compact without requiring a managed-storage flag;
Python still publishes the compact manifest.
Physical workers release consumed transport and placement inputs after their
last successful consumer; typed timing and Chimew geometry owners remain live.
See the [physical input lifecycle](docs/XILINX_PHYSICAL_FLOW.md#placement-preparation).
The shared core also supervises external tools with process-group cancellation,
resource limits, literal arguments and environment values (including empty
values), and bounded diagnostics. Installed Yosys streaming and OpenSTA
extraction use that lifecycle through small requests.
An opt-in `multi-fpga compile --compile-implementation native-control-v1` uses
the C++ one-shot `emuflow-native compile` controller. Its TDM stage prepares
configuration and executes directly in an isolated native process; other stages
retain shared Python adapters. Baseline and retained Chimew comparison are supported with
either Xilinx physical backend; fixed wiring and full physical/global timing
checks remain required. The default remains `python-control-v1`; provider and
algorithm defaults are unchanged. This is transitional orchestration, not
completed native data-plane migration or large-design qualification. Supported modes, remaining
integration and result contracts are described in the
[user guide](docs/USER_GUIDE.md#native-compile-control).
Each native-controlled phase releases its isolated temporary workspace after
execution and handoff validation; published artifacts remain in the flow directory.

## Flow

| Phase | Responsibility | Algorithms and tools |
| --- | --- | --- |
| 1 | Synthesize and map RTL; preserve connectivity, primitive parameters, and resource demand in EmuIR | [Yosys/ABC](https://github.com/YosysHQ/yosys); EmuFlow Xilinx UltraScale+ mapping and indexed EmuIR import |
| 2 | Extract timing paths and derive optimization weights | [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA); EmuFlow TimingPathDB export |
| 3 | Cluster and partition against resource and topology constraints | [TritonPart](https://github.com/ABKGroup/TritonPart) initialization → [PATRON](docs/ALGORITHMS.md#phase-3-initial-partitioning-and-post-processing) post-processing |
| 4 | Route cross-FPGA multicast communication | EmuFlow global-candidate C++ router, informed by the [DAC 2025 synergistic router](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf) and [ASP-DAC 2026 timing-aware routing](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf) |
| 5 | Assign concrete TDM lanes, slots, and dependency events | EmuFlow [Static Exact dependency scheduler](docs/ALGORITHMS.md#phase-5-tdm-ratio-lane-and-slot-assignment), integrated with [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) path identities and platform bank/ratio constraints |
| 6 | Plan pins/lanes, split the netlist, and generate transport RTL | EmuFlow baseline pin/transport flow; optional [Chimew, FPGA 2026](https://doi.org/10.1145/3748173.3779199) provider; static transport, shadow registers, and frame-barrier synchronization |
| 7 | Implement each FPGA and analyze whole-design timing | [RapidWright](https://github.com/Xilinx/RapidWright), [OpenPARF](https://github.com/PKU-IDEA/OpenPARF), and [RWRoute](https://www.rapidwright.io/docs/RWRoute_timing_driven_routing.html); optional Vivado implementation; [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) global timing |

Each stage has versioned artifacts and independent structural, legality,
coverage, or equivalence checks. Passing an intermediate stage does not
establish physical timing closure.

The bundled XCVU19P cable model admits only the discrete TDM ratios
`8, 16, ..., 256`. The schedule frame may contain more slots, but its length
does not expand this platform ratio domain.

The Xilinx Phase 7 path combines fanout-aware placement, RUDY/pin-density area
adjustment, and a reserved post-adjustment convergence window before exact
legalization and negotiated routing. Analytical LUT density uses the eight
qualified LUT BELs per slice, independently of the native sixteen-slot encoding.
Empty and reserved rows retain zero placement capacity; the driver checks the
parsed native density area before solving. Logic filler sampling preserves its
exact total area and limits each LUT/FF sample to half a density bin so the
density field retains enough spatial resolution before exact legalization.
The native certificate records the effective count, geometry, and span.
The `0.10` global-placement overflow target remains the normal routability
criterion. A representative placement may also restore a bounded `0.25`
checkpoint for diagnostic DirectLG. Once bounded, overflow-only progress does
not extend the HPWL patience window; the solver restores the lowest-HPWL
checkpoint instead of allowing density multipliers to destroy wirelength. The
diagnostic path records legalization displacement and still requires exact
site/BEL validation before any routing release.
The final route still requires zero
overused nodes and complete source-to-sink connectivity.

Phase 3 can seal per-FPGA downstream resource reservations in
`resource_reservations_by_fpga`. Every partition provider then limits the
original design to the 25%-capped physical capacity minus that reservation,
while balance shares remain based on the unchanged physical devices. Phase 6
carries the seal from the assignment index into the split manifest and
independently rejects missing or changed reservations; Phase 7 streams the synthesized
transport resource totals from SQLite, rejects any reservation overrun, and
still checks the combined implementation against the original physical cap.
Compact production Phase 3 publishes its assignment index through the shared C++ owner,
reading source instances directly rather than returning instance rows to Python.
See the [native publication contract](docs/PHASE3_NATIVE_MIGRATION.md#assignment-index-publication).

The table names the principal upstream tools and method sources; EmuFlow's
integration and extensions are mapped to implementation entry points in the
[algorithm/provider overview](docs/ALGORITHMS.md). See also the
[architecture and phase contracts](docs/FLOW_PLAN.md) and complete
[source/license inventory](OPEN_SOURCE_COMPONENTS.md).

## Design documentation

Detailed design material is kept outside this project entry point:

- [Architecture and phase contracts](docs/FLOW_PLAN.md)
- [Algorithms, providers, and research references](docs/ALGORITHMS.md)
- [Hardware architecture, resource policy, and inter-FPGA wiring](docs/HARDWARE_PLATFORM.md)
- [Xilinx placement, routing, fixed-I/O, and timing contract](docs/XILINX_PHYSICAL_FLOW.md)
- [Global OpenSTA timing scope](docs/global-opensta-qualification.md)
- [Static Exact combinational-cut semantics](docs/STATIC_EXACT_COMBINATIONAL_CUT.md)

The platform and physical-flow documents define reproducible public contracts
for the research implementation.

## Build

The root CMake project builds EmuFlow's native kernels and selected in-tree
engines. Python packaging alone does not build a complete flow.

Required tools include CMake 3.20+, GNU Make, a C++17 compiler, Python 3.9+,
Boost, and the SQLite 3.22+ and zlib development libraries. The complete build also needs the
development dependencies for OpenROAD, OpenSTA, and OpenPARF: Bison, Flex,
Tcl 8.6+ (including the OpenSTA runtime), SWIG, Eigen3,
spdlog, LEMON, OR-Tools, OpenMP, and the appropriate Python/PyTorch packages.
CUDA is optional and disabled by default. The OpenPARF Python/PyTorch ABI must
match the interpreter selected when compiling its native operators.

```bash
cmake --preset release
cmake --build --preset release --parallel 2
ctest --preset release
```

Build products are installed under `build/native/install/`, including the
default Xilinx primitive library and bundled BoardDB/I/O/schema data.
The installed-runtime CTest checks package inventory and default resources.
For dependency-prefix configuration and partial development builds, see the
[user guide](docs/USER_GUIDE.md).
Before an expensive run, execute `emuflow-native check-runtime` in the selected
execution environment to verify core library loading and SQLite/JSON support.

## Quick start

Use the CLI produced by the root build. The following smoke run uses the
current defaults: the `xilinx-ultrascaleplus-open-v1` mapping profile, the
bundled four-XCVU19P BoardDB, timing-driven PATRON v14 with Static Exact cuts,
native system routing and TDM assignment, and baseline Phase 6 generation:

```bash
export PATH="$PWD/build/native/install/bin:$PATH"
emuflow --help

emuflow platform validate \
  platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json

emuflow multi-fpga compile examples/rtl/counter.v \
  --top counter \
  --clock clk \
  --clock-period clk=10 \
  --out build/counter-multi-fpga

emuflow multi-fpga validate --flow build/counter-multi-fpga
```

Omitting `--platform`, `--mapping-profile`, and `--physical-backend` above is
intentional: the command exercises the actual defaults. The resulting
`split/` directory includes the fixed virtual-board wiring and binding,
per-FPGA `virtual_data_pins.xdc`, differential-I/O adapters, and transport RTL.
The counter is a smoke input rather than a multi-FPGA QoR workload.

Add `--physical` only after supplying the source-sealed Xilinx ArchitectureDB,
RapidWright runtime/device/timing inputs, native OpenPARF constraints, and
provider manifest listed by `emuflow multi-fpga compile --help`. Validate such
a result with `--require-physical`. See the
[physical-flow instructions](docs/USER_GUIDE.md#physical-implementation) and
[physical acceptance contract](docs/XILINX_PHYSICAL_FLOW.md).

## Configuration and limits

The CLI and Python API default to `rapidwright` with
`xilinx-ultrascaleplus-open-v1` mapping. Supply the matching
device/provider, architecture, timing, and runtime
inputs described in the [user guide](docs/USER_GUIDE.md#physical-implementation).
Add `--physical` to execute placement and routing.

The compile upstream supports only Xilinx UltraScale+ mapping; the independent
`generic-soft` and `vtr-hard-blocks` profiles and VTR synthesis entry are retired.
Both compile and standalone physical entries accept only `rapidwright` and
`vivado`. The VTR/open backend, architecture importer and timing conversion,
packed-placement/route adapters, and build/install targets are retired. Missing
inputs never select another toolchain.

Other CLI defaults are TritonPart initial partitioning followed by
timing-driven PATRON v14 post-processing, `static-exact-combinational` cuts,
baseline Phase 6, and OpenSTA global physical timing. Global timing consumes
complete, contiguous original-path bundles with concrete scheduled events.
Invocation-owned event stores feed shared-core validation, bundle compression
and OpenSTA model export without retaining whole check/identity arrays in Python.
The shared core imports representative engine scalars, restores all original
identities, reduces WNS/TNS and publishes typed QoR records. Current typed
physical facts bind directly to events through the same core,
without a Python endpoint dictionary or path/hop producer. Shared-core readers
validate the complete boundary and logic-segment identity/timing records and
local-path timing records. Vivado measurement TSVs import directly into these owners through
the shared core; both backends use the same typed local-path identity. Remaining
phase producers and backend adapters still require migration; W6/W7 are unfinished.
Historical regrouping and schedule reconstruction fallbacks are retired. See the
[global timing contract](docs/global-opensta-qualification.md#current-event-stream-contract).
CLI, Python phase APIs and experiment configurations share these defaults;
cut mode never implicitly selects an older PATRON version. The redundant
`--patron-flow-refinement` selector is retired. Chimew remains an explicit
Phase 6 option, with its implementation, dependencies and validation retained.
Its integrated lookahead requires the Xilinx profile, the selected RapidWright
or Vivado backend, and native package-pin geometry in the ArchitectureDB.
The implicit `auto` provider and VTR/virtual-pin lookahead adapter are retired.
This does not establish full-device Chimew QoR qualification.
Phase 3 supplies exact resource limits to TritonPart, checks feasibility during
native search, and independently validates its output. PATRON receives a legal
initial assignment: bounded native ILP establishes coarse resource feasibility
before optimizing cut.
No external balance
legalizer modifies the solver output.

For very large PATRON v14 designs, Phase 3 performs one local-move seed sweep
over the configured critical-cluster budget before the existing ejection/comove
and frontier-flow passes. Candidates are ordered by maximum path hops, maximum
path TDM-ratio sum, and timing exposure. The same order and the configured
legal-candidate budget bound frontier tail repair when high-fanout nets expose
very large endpoint sets. These limits prevent whole-design candidate scans
from consuming the native timeout before the two primary objectives are
reached. Small qualification models retain the
configured full search budgets; all capacity and lexicographic acceptance
checks remain unchanged.

Thin versioned runtime deployments rebuild the unified native core, including
Phase 3 analysis and hop legalization, from the same source revision. Installed runtime
checks bind current executables, modules and data to that source.

- State the period of every analyzed clock explicitly.
- Mapping profiles must match the physical device model; generic soft mapping
  can expand RAM/DSP-heavy designs substantially.
- Static Exact supports a constrained synchronous, single-virtual-clock
  semantic model. It is not arbitrary CDC or multi-clock signoff.
- Contest datasets supply communication graphs and topology constraints, not
  RTL or complete physical board specifications.
- Virtual BoardDB capacity and link assumptions must be distinguished from
  measured device and board properties.
- Bitstream generation and board bring-up require a supported concrete device,
  board support package, clock/reset configuration, and transport interface.
- RapidWright device/API dependencies have their own licensing conditions.
  Vivado is proprietary and is not bundled. Native installations include the
  primitive contracts, fixed wiring tables, and Tcl adapters required by the indexed Vivado route.
  Separate optional container launchers support Yosys and OpenSTA on older HPC
  hosts; see the [user guide](docs/USER_GUIDE.md#physical-implementation).
- WNS/TNS are scoped to the declared source TimingPathDB population.
  Original-path TNS sums negative slack per original path, not once per
  unique register endpoint. Per-FPGA metrics are separate diagnostics.
  See [timing-model scope](docs/global-opensta-qualification.md).
- Xilinx physical timing normalizes the RAMB18E2 BEL aliases WEA[2:3] to the
  logical Unisim WEA[0:1] pins only when the corresponding physical-width pin
  is absent; independently represented pins remain distinct and fail closed.

Unsupported inputs must be reported explicitly; a backend must not silently
substitute a different architecture, mapping, or timing engine.

## Benchmarks

The [RTL catalog](benchmarks/rtl_catalog.json) identifies upstream designs,
licenses, and pinned source revisions. The
[contest matrix](benchmarks/contest_validation_matrix.json) separately covers
communication-algorithm inputs. The VTR-only KOIOS medium run specification and
its unexecuted full-flow matrix are retired; their workload identities are not
reassigned to Xilinx runs.

See [benchmark usage](docs/RTL_BENCHMARKS.md). Raw logs, experiment histories,
machine-specific paths, and temporary results are not project documentation.

## Repository layout

```text
src/          orchestration, native providers, and validators
engines/      selected EDA engines and their source provenance
schemas/      versioned artifact contracts
platforms/    virtual platforms and board-model inputs
providers/    provider manifests and integration recipes
rtl/          transport and runtime-synchronization RTL
benchmarks/   workload catalogs, platform matrices, and run contracts
examples/     small RTL and artifact fixtures
scripts/      build, import, and provider utilities
tests/        unit and integration regressions
docs/         architecture, usage, and validation documentation
```

## Documentation and contributing

- [User guide](docs/USER_GUIDE.md)
- [Architecture and phase contracts](docs/FLOW_PLAN.md)
- [Algorithms and providers](docs/ALGORITHMS.md)
- [Hardware platform and inter-FPGA wiring](docs/HARDWARE_PLATFORM.md)
- [Xilinx physical flow](docs/XILINX_PHYSICAL_FLOW.md)
- [Global OpenSTA timing scope](docs/global-opensta-qualification.md)
- [Static Exact semantics](docs/STATIC_EXACT_COMBINATIONAL_CUT.md)
- [Contest/Phase 6 interface contract](docs/CONTEST_PHASE6_INTEGRATION_CONTRACT.md)
- [Validation requirements](docs/VALIDATION.md)
- [Contributing](CONTRIBUTING.md)

## License and source provenance

EmuFlow's first-party code is licensed under [Apache-2.0](LICENSE).
Included upstream components retain their own licenses and notices.

The source inventory includes [Yosys](https://github.com/YosysHQ/yosys),
[OpenROAD/TritonPart](https://github.com/The-OpenROAD-Project/OpenROAD),
[OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA), and
[OpenPARF](https://github.com/PKU-IDEA/OpenPARF).
See [the complete component and license inventory](OPEN_SOURCE_COMPONENTS.md),
[its machine-readable form](OPEN_SOURCE_COMPONENTS.json), and
[the implementation source manifest](SOURCE_MANIFEST.json).
