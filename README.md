# EmuFlow

EmuFlow is a research-oriented multi-FPGA emulation flow. It compiles
synchronous RTL into per-FPGA netlists and a statically scheduled communication
fabric, with explicit interfaces between synthesis, partitioning, system
routing, TDM assignment, physical implementation, and global timing analysis.

The default research route uses RapidWright device resources, native OpenPARF
placement, and RWRoute routing for supported Xilinx FPGAs. VTR/VPR remains an
alternative academic backend, and Vivado is an optional proprietary backend.
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

Phases 3–7 exchange compact selector manifests and content-addressed SQLite
sidecars for assignments, routes, schedules, transport, and physical handoffs.
Production readers stream typed indexed relations; whole-graph materialization is
an explicit compatibility operation. A continued run uses the current BoardDB
and does not inherit an older interconnect configuration from the saved input.

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

The Xilinx Phase 7 path combines fanout-aware placement, RUDY/pin-density area
adjustment, and a reserved post-adjustment convergence window before exact
legalization and negotiated routing. The final route still requires zero
overused nodes and complete source-to-sink connectivity.

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
and Boost. The complete build also needs the development dependencies for
OpenROAD, OpenSTA, OpenPARF, and VPR: Bison, Flex, Tcl, SWIG, Eigen3, zlib,
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
  not require Vivado.
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
