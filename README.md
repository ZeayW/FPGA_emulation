# EmuFlow

EmuFlow is a research-oriented multi-FPGA emulation flow. It compiles synchronous
RTL into per-FPGA netlists and a statically scheduled communication fabric,
then implements each FPGA and analyzes whole-design timing.

The maintained flow has one common upstream pipeline, two physical backends,
and an optional Chimew provider for Phase 6:

- **Upstream:** Yosys → OpenSTA → TritonPart/PATRON v14 → system routing →
  Static Exact dependency scheduling → netlist splitting and transport generation.
- **OpenPARF/RWRoute backend:** native OpenPARF placement and legalization,
  followed by RapidWright/RWRoute routing.
- **Vivado backend:** synthesis, optimization, placement and routing through an
  external Vivado installation.
- **Global timing:** physical measurements and scheduled communication events
  feed OpenSTA, with coverage checked against the original timing-path population.

Both backends share the upstream contracts and independent validation. A
successful intermediate stage does not establish physical timing closure.

## Flow

| Phase | Responsibility | Maintained implementation and references |
| --- | --- | --- |
| 1 | Synthesize RTL, map Xilinx UltraScale+ primitives and import connectivity | [Yosys/ABC](https://github.com/YosysHQ/yosys); EmuFlow indexed EmuIR import |
| 2 | Extract pre-placement timing paths and optimization weights | [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA); indexed TimingPathDB |
| 3 | Cluster and partition under resource and topology constraints | [TritonPart](https://github.com/ABKGroup/TritonPart) initialization → [PATRON v14](docs/ALGORITHMS.md#phase-3-initial-partitioning-and-post-processing) |
| 4 | Route cross-FPGA multicast communication | EmuFlow [`timing-aware-global-candidate-v1`](docs/ALGORITHMS.md#phase-4-system-level-multicast-routing) C++ router; method references: [DAC 2025 synergistic routing](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf), [ASP-DAC 2026 timing-aware routing](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf) |
| 5 | Assign concrete TDM lanes, slots and dependency events | EmuFlow [Static Exact dependency scheduler](docs/ALGORITHMS.md#phase-5-tdm-ratio-lane-and-slot-assignment), `deterministic-dependency-earliest-slot-v1`; shared [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) path identities |
| 6 | Split netlists and generate transport, runtime and fixed-I/O contracts | EmuFlow native baseline; optional [Chimew, FPGA 2026](https://doi.org/10.1145/3748173.3779199) grouping and bank/channel assignment |
| 7 | Place and route each FPGA; collect physical and timing evidence | [OpenPARF](https://github.com/PKU-IDEA/OpenPARF) placement → [RapidWright](https://github.com/Xilinx/RapidWright)/[RWRoute](https://www.rapidwright.io/docs/RWRoute_timing_driven_routing.html) routing; alternative [Vivado backend](docs/XILINX_PHYSICAL_FLOW.md#vivado-indexed-provider) |
| 7C | Bind communication events to physical measurements and analyze global timing | [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) with independent [original-path coverage checks](docs/global-opensta-qualification.md) |

Phases exchange versioned artifacts, compact selectors and typed indexed stores.
Assignments, routes, schedules, transport and physical identities have explicit
owners and source seals. The phase contracts are described in
[the flow architecture](docs/FLOW_PLAN.md); algorithm definitions and method
references are in [the algorithm overview](docs/ALGORITHMS.md).

## Quick start

The installer is currently tested on Linux x86-64. The complete flow has not yet
been tested on macOS or native Windows; Windows users can use a Linux WSL
environment.

Before running the installer, install these prerequisites:

- Git, Python 3.9+ with `venv`, CMake 3.20+, GNU Make and a C++17 compiler;
- a JDK providing both `java` and `javac`;
- Bison 3.4+, Flex 2.6+, SWIG 4.0+ and Tcl 8.6+ development files;
- Boost 1.78+, SQLite 3.22+, zlib, Eigen3, spdlog, LEMON, OR-Tools and OpenMP
  development packages.

Python packages used by the flow, including NumPy, PyYAML and CPU PyTorch, are
prepared automatically when they are missing. Vivado is optional and is only
needed for the `vivado` backend.

Install once, then give EmuFlow the RTL, top module and clock period:

```bash
git clone https://github.com/ZeayW/FPGA_emulation.git
cd FPGA_emulation
./install.sh

# Choose the OpenPARF + RapidWright/RWRoute route
emuflow run rtl/top.sv --top top --clock clk=10 --backend rapidwright

# Or choose the Vivado route
emuflow run rtl/top.sv --top top --clock clk=10 --backend vivado
```

The installer builds the complete runtime, downloads the pinned RapidWright
release, prepares the supported XCVU19P device data and stores the resulting
runtime manifest inside the installation. There is no machine profile to edit or
register. See [installation and run details](docs/RUNNING.md).

The run command executes the complete flow and writes `runs/top`. The physical
route is selected per run: use `--backend rapidwright` for OpenPARF/RWRoute or
`--backend vivado` for Vivado. Add more RTL files as positional arguments, or
choose a different output with `--out`.
Stage progress is printed by the native controller. Each output directory contains
`stage-metrics/`, `multi-fpga-flow-report.json`, and terminal
`runtime/phase7c_report.json` and `runtime/qor_report.json` evidence.
Use a new output directory for each run. Both backends remain under NVDLA
qualification; launching a run does not establish successful physical closure.

## Developer build

Use the root CMake build for a complete runtime. Installing the Python package
alone does not build the native core or EDA engines.

Core prerequisites include CMake 3.20+, GNU Make, a C++17 compiler, Python 3.9+,
Boost, SQLite 3.22+ development files and zlib. The complete engine build also
requires the OpenROAD, OpenSTA and OpenPARF dependencies, including Tcl, Bison,
Flex, SWIG, Eigen3, spdlog, LEMON, OR-Tools, OpenMP and matching Python/PyTorch
packages. CUDA is optional and disabled by default. See
[complete build instructions](docs/USER_GUIDE.md#complete-build) for dependency
prefixes and development build options.

```bash
cmake --preset release
cmake --build --preset release --parallel 2
ctest --preset release
```

The build installs commands, libraries and runtime data under
`build/native/install/`. The installed commands are `emuflow` and
`emuflow-native`. `./install.sh` additionally prepares RapidWright and its
device contracts. An optional Vivado installation remains external; see
[physical implementation](docs/USER_GUIDE.md#physical-implementation).

## Configuration

| Setting | Current default or supported choices |
| --- | --- |
| Mapping profile | `xilinx-ultrascaleplus-open-v1` |
| Platform | Bundled four-XCVU19P virtual mesh; override with `--platform` |
| Partitioning | TritonPart initialization and timing-driven PATRON v14 |
| Cut semantics | `static-exact-combinational` |
| Phase 6 provider | `baseline`; select `chimew` explicitly |
| Physical backend | `rapidwright` for OpenPARF/RWRoute; alternative `vivado` |
| Global physical timing | `opensta` |
| Compile controller | Native one-shot controller, `emuflow-native compile` |

The CLI and Python compile API submit requests to the same native controller.
Algorithm and backend options retain their own explicit contracts. Missing
inputs or failed native operations do not select a different backend or fall
back to retired implementations.

Chimew is retained with its dependencies and independent checks. Its integrated
lookahead requires source-qualified package-pin geometry and the selected
physical backend. See [Phase 6 providers](docs/USER_GUIDE.md#phase-6-providers).

To reuse completed synthesis and pre-placement timing, add:

```bash
--prepared-frontend PHASE1_DIRECTORY --prepared-timing PHASE2_DIRECTORY
```

Continuation validates the saved design, mapping, clocks and indexed timing
contracts, then uses the currently selected BoardDB for Phases 3–7. It does not
inherit an older interconnect configuration. See
[continuation requirements](docs/USER_GUIDE.md#continue-from-completed-indexed-inputs).

## Implementation and validation status

The shared C++ library owns the ordinary RTL frontend, pre-placement timing,
Phase 3 partitioning, Phase 4 routing, Phase 5 scheduling and baseline Phase 6
when these stages run under native control. It also owns OpenPARF input
preparation, typed physical handoff production and audit, RWRoute input export
and connectivity/PIP validation, and the core global timing binding and reduction.
The replaced Python input generators are removed from the production package.

The native controller still uses transitional adapters for configuration,
supplied/prepared inputs, Chimew and physical/signoff stages. OpenPARF
placement-result processing and certificate production also retain Python
production logic; the required OpenPARF/PyTorch host remains. A single production
controller does not establish complete native data-plane ownership. See
[native compile control](docs/USER_GUIDE.md#native-compile-control) and
[migration boundaries](docs/PHASE3_NATIVE_MIGRATION.md#stage-ownership-boundaries).

Current-source NVDLA end-to-end physical and global-timing qualification remains
incomplete. Component regressions and intermediate results do not establish
full-design acceptance on either physical backend. The required correctness,
coverage, resource and timing evidence is defined in
[validation requirements](docs/VALIDATION.md) and
[the Xilinx physical-flow contract](docs/XILINX_PHYSICAL_FLOW.md).

## Scope and limits

- The maintained compile path targets Xilinx UltraScale+. Retired partition,
  scheduling and physical routes are not alternative production configurations;
  historical runs require their matching source revision.
- The bundled BoardDB is an academic platform model, not evidence of a
  manufactured board. Its default four-FPGA preset caps each resource at 25% of
  each device's raw capacity, including downstream implementation overhead.
  Partition balance and physical capacity are separate constraints. See
  [the hardware platform model](docs/HARDWARE_PLATFORM.md).
- State every analyzed clock period explicitly. Static Exact has a constrained
  synchronous, single-virtual-clock semantic model; it does not establish
  arbitrary CDC or board-level clock/skew signoff.
- Physical acceptance requires complete cell and endpoint coverage, legal
  site/BEL and dedicated-resource use, exclusive routing-resource ownership and
  source-to-sink connectivity. Analytical placement convergence alone is not
  a physical certificate.
- WNS/TNS refer to the declared original TimingPathDB population. Original-path
  TNS sums negative slack per original path, rather than once per unique register
  endpoint. Per-FPGA timing summaries are separate diagnostics. See
  [global timing scope](docs/global-opensta-qualification.md).
- Vivado is proprietary and is not bundled. RapidWright and other upstream
  components retain their own licensing conditions. Bitstream generation and
  hardware bring-up require board-specific clock, reset, electrical and transport
  qualification beyond this research-platform flow.

## Repository layout

```text
src/core/              shared C++ implementation, organized by domain
src/cli/               native executable entry points
src/python/emuflow/    Python package, remaining adapters and runtime data
tools/emuflow_tools/   separately installed research and dataset utilities
engines/               selected third-party EDA engine sources
schemas/               versioned artifact contracts
platforms/             virtual platforms and fixed wiring inputs
providers/             provider manifests and integration recipes
rtl/                   transport RTL
benchmarks/            workload catalogs and validation matrices
examples/              small RTL and artifact fixtures
scripts/               build, import and provider utilities
tests/                 independent checks and regression tests
docs/                  usage, architecture and validation documentation
```

Core and Python implementations use matching domains: frontend, partition,
routing, scheduling, transport, physical and timing, with shared flow, storage
and IR facilities. There are no forwarding packages for the retired flat source
paths. See [source layout](docs/PROJECT_LAYOUT.md).

The [RTL catalog](benchmarks/rtl_catalog.json) records designs, licenses and
pinned source revisions. [Contest inputs](benchmarks/contest_validation_matrix.json)
are communication graphs and topology constraints, not full RTL or physical
boards. Standalone contest and calibration utilities live in the
[optional tools package](tools/README.md), outside the production runtime.

## Documentation

- [User guide and build setup](docs/USER_GUIDE.md)
- [Architecture and phase contracts](docs/FLOW_PLAN.md)
- [Algorithms, providers and research references](docs/ALGORITHMS.md)
- [Hardware platform and inter-FPGA wiring](docs/HARDWARE_PLATFORM.md)
- [Xilinx placement, routing and fixed-I/O contracts](docs/XILINX_PHYSICAL_FLOW.md)
- [Global OpenSTA timing scope](docs/global-opensta-qualification.md)
- [Static Exact semantics](docs/STATIC_EXACT_COMBINATIONAL_CUT.md)
- [Native migration boundaries](docs/PHASE3_NATIVE_MIGRATION.md)
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
