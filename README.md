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

Supported installer platform: Linux x86-64. On Windows, use a Linux WSL
environment.

Before running the installer, install these prerequisites:

- Git, Python 3.9+ with `venv`, CMake 3.20+, GNU Make and a C++17 compiler;
- a JDK providing both `java` and `javac`;
- Bison 3.4+, Flex 2.6+, SWIG 4.0+ and Tcl 8.6+ development files;
- Boost 1.78+, SQLite 3.22+, zlib, Eigen3, spdlog, LEMON, OR-Tools and OpenMP
  development packages.

The installer prepares NumPy, PyYAML, CPU PyTorch and RapidWright. The Vivado
backend requires an existing Vivado installation.

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

The installer builds and configures EmuFlow. The [step-by-step guide](docs/RUNNING.md)
explains each command and argument.

Results are written to `runs/top` by default. Use `--out` to choose another
output directory.

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
