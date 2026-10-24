# EmuFlow

EmuFlow is a research-oriented multi-FPGA emulation flow. It compiles
synchronous RTL into per-FPGA netlists and a statically scheduled communication
fabric, with explicit interfaces between synthesis, partitioning, system
routing, TDM assignment, physical implementation, and global timing analysis.

The default research route uses RapidWright device resources, native OpenPARF
placement, and RWRoute routing for supported Xilinx FPGAs. VTR/VPR remains an
alternative academic backend, and Vivado is an optional proprietary backend.
Optional container launchers support native-tool ABI and Tcl script bindings,
with runtime selection separate from algorithm settings. See the
[build instructions](docs/USER_GUIDE.md#complete-build).
OpenSTA container evidence identifies the native engine and launcher separately.
The indexed Vivado adapter consumes current Phase 6 SQLite contracts with
canonical net identities, fixed LVDS I/O, native sequential control pins, and
the common physical-result contract. Real-device qualification is in progress. Its OOC implementation retains abstract external clock sources and
does not establish board clock/skew signoff. Used undriven source nets are
rejected before mapping or vendor implementation. Physical capacity accounting
handles native macro expansion and shared LUT halves.
Indexed mapped primitives carry provenance tags without global KEEP/DONT_TOUCH
constraints; source-bound coverage is checked after optimization. The timing
contract requires an explicit measured or proven optimized-away disposition for
every original path; WNS/TNS use surviving implemented paths. Local paths,
cross-FPGA logic segments, and shadow interfaces can retain eliminated records
with null delays and checked source-proof references. Selected vendor constant
choices additionally require complete frontier checks and checkpoint binding. Complete physical coverage
and global timing remain required qualification gates; the physical evidence
schemas follow this disposition contract. Explicit checkpoint I/O and selected source-bound constant-state
diagnostics are documented with the provider. See the
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
an explicit compatibility operation. Phase 7 explicitly enables immutable
SQLite input URIs across supported runtimes. A continued run uses the current
BoardDB and does not inherit an older interconnect configuration from the saved input.
Mapped physical pin indexes are built in bulk before their atomic publication.
Indexed OpenPARF Bookshelf export streams typed pins through C++ without
reconstructing the cell and pin population in Python.
Physical macro derivation is shared within one immutable mapped reader;
independent artifact readers still derive and validate their own contracts.
The native SLL weight update follows LEAPS' fixed-step Adam equation with
`t_psi=0.002` and the paper's current-delta EMA weight `rho=0.9`. Absolute
SLL-count changes feed the EMA and moments once; they are not exponentiated a
second time, so the update remains stable as the design grows.
When driving the reusable stage commands directly, pass
`experiment-stage cut-timing-run --prepared-timing-boundary` (and the same flag
plus the expected `--clock-period` values to `cut-timing-validate`) to consume a
saved indexed Phase 2 publication. This mode validates the native SQLite
version, clocks, row counts, and design identity without rerunning OpenSTA or
converting the database to whole-graph JSON.

The native migration keeps the large Phase 3–7 relations in typed, indexed
storage and streams them through unified native entry points. Phase 6 publishes
virtual wiring, binding, differential-I/O, and XDC contracts in the sealed
transport store; Phase 7 reads those contracts lazily. See the
[Phase 3 native migration](docs/PHASE3_NATIVE_MIGRATION.md) for the storage
and migration contracts.

## Flow

| Phase | Responsibility | Algorithms and tools |
| --- | --- | --- |
| 1 | Synthesize and map RTL; preserve connectivity, primitive parameters, and resource demand in EmuIR | [Yosys/ABC](https://github.com/YosysHQ/yosys); EmuFlow mapping profiles for VTR hard blocks, generic soft logic, and Xilinx UltraScale+ |
| 2 | Extract timing paths and derive optimization weights | [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA); optional Vivado timing provider; EmuFlow TimingPathDB export |
| 3 | Cluster and partition against resource and topology constraints | [TritonPart](https://github.com/ABKGroup/TritonPart) initialization → [PATRON](docs/ALGORITHMS.md#phase-3-initial-partitioning-and-post-processing) post-processing; [RePart](https://github.com/Welement-zyf/RePart), [MFSPart](https://zhiyaoxie.com/files/TCAD26_MFSPart.pdf), and greedy alternatives |
| 4 | Route cross-FPGA multicast communication | EmuFlow native C++ router, informed by the [DAC 2025 synergistic router](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf) and [ASP-DAC 2026 timing-aware routing](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf) |
| 5 | Assign TDM ratios, lanes, and slots to routed communication | Lagrangian/KKT optimization from [Pui and Young, TODAES 2020](https://doi.org/10.1145/3377551) and the [ASP-DAC 2026 timing-DAG method](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf), followed by EmuFlow legalization and event binding |
| 6 | Plan pins/lanes, split the netlist, and generate transport RTL | EmuFlow baseline pin/transport flow; optional [Chimew, FPGA 2026](https://doi.org/10.1145/3748173.3779199) provider; static transport, shadow registers, and frame-barrier synchronization |
| 7 | Implement each FPGA and analyze whole-design timing | [RapidWright](https://github.com/Xilinx/RapidWright), [OpenPARF](https://github.com/PKU-IDEA/OpenPARF), and [RWRoute](https://www.rapidwright.io/docs/RWRoute_timing_driven_routing.html); alternative [VTR/VPR](https://github.com/verilog-to-routing/vtr-verilog-to-routing) or Vivado backends; [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) global timing |

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
Boost, and the SQLite 3.22+ development library. The complete build also needs the
development dependencies for OpenROAD, OpenSTA, OpenPARF, and VPR: Bison, Flex,
Tcl 8.6+ (including the OpenSTA runtime), SWIG, Eigen3, zlib,
spdlog, LEMON, OR-Tools, OpenMP, and the appropriate Python/PyTorch packages.
CUDA is optional and disabled by default. The OpenPARF Python/PyTorch ABI must
match the interpreter selected when compiling its native operators.

```bash
cmake --preset release
cmake --build --preset release --parallel 2
ctest --preset release
```

Build products are installed under `build/native/install/`.
For dependency-prefix configuration and partial development builds, see the
[user guide](docs/USER_GUIDE.md).

## Quick start

Use the CLI produced by the root build. The following smoke run uses the
current defaults: the `xilinx-ultrascaleplus-open-v1` mapping profile, the
bundled four-XCVU19P BoardDB, timing-driven PATRON, native system routing and
TDM assignment, and baseline Phase 6 generation:

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

To use the alternative VTR route, explicitly select
`--physical-backend open --mapping-profile vtr-hard-blocks`. Standalone
`multi-fpga physical` also defaults to RapidWright; use `--backend open` for VTR.
Missing RapidWright inputs do not trigger an automatic VTR fallback.

Other CLI defaults are TritonPart initial partitioning followed by
timing-driven PATRON post-processing, `sequential-only` cuts, baseline Phase 6,
and OpenSTA global physical timing. Generalized Static Exact combinational
cuts and Chimew are explicit options. The integrated Chimew
lookahead path currently requires the explicit VTR backend/profile combination.
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
reached. Small qualification models and older PATRON versions retain the
configured exhaustive behavior; all capacity and lexicographic acceptance
checks remain unchanged.

Thin versioned runtime deployments rebuild the native Static Exact analyzer
and exact scheduler alongside the partition, routing, TDM ratio, timing-DAG,
concrete-slot, and balance tools. Large-design Phase 3
therefore uses native semantic validation from the same exact source revision
as the refiner. Deployment selects the native compiler only after compiling and
running a C++17 `<filesystem>` probe with the same statically linked C++ runtime
used by the deployed tools; an explicit `CXX` override fails closed when it
cannot satisfy that probe. This prevents validation-farm login defaults such as
GCC 7, or an older compute-node `libstdc++`, from producing a partial runtime.
The compact bridge also derives dense native endpoint coordinates from stored
endpoint order, so filtered legacy artifacts with sparse endpoint ordinals keep
the same logical EmuIR semantics without rewriting the SQLite database.

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
  Vivado is proprietary and is not bundled. The alternative VTR backend does
  not require Vivado. Native installations include the primitive contracts,
  fixed wiring tables, and Tcl adapters required by the indexed Vivado route.
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
[full-flow matrix](benchmarks/end_to_end_validation_matrix.json) pairs real RTL
workloads with platform configurations; the
[contest matrix](benchmarks/contest_validation_matrix.json) separately covers
communication-algorithm inputs.

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
[OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA),
[RePart](https://github.com/Welement-zyf/RePart),
[OpenPARF](https://github.com/PKU-IDEA/OpenPARF), and
[VTR/VPR](https://github.com/verilog-to-routing/vtr-verilog-to-routing).
See [the complete component and license inventory](OPEN_SOURCE_COMPONENTS.md),
[its machine-readable form](OPEN_SOURCE_COMPONENTS.json), and
[the implementation source manifest](SOURCE_MANIFEST.json).
