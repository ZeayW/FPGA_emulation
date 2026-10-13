# EmuFlow

EmuFlow is a research-oriented multi-FPGA emulation flow. It compiles
synchronous RTL into per-FPGA netlists and a statically scheduled communication
fabric, with explicit interfaces between synthesis, partitioning, system
routing, TDM assignment, physical implementation, and global timing analysis.

The default research backend uses a public VTR academic FPGA model. Optional
RapidWright and Vivado backends support Xilinx device integration. A virtual
platform is an academic model, not a deployable board or evidence of hardware
signoff.

## Flow

| Phase | Responsibility | Algorithms and tools |
| --- | --- | --- |
| 1 | Synthesize and map RTL; preserve connectivity, primitive parameters, and resource demand in EmuIR | Yosys and ABC; mapping profiles for VTR hard blocks, generic soft logic, and Xilinx UltraScale+ |
| 2 | Extract timing paths and derive optimization weights | OpenSTA; optional Vivado timing provider |
| 3 | Cluster and partition against resource and topology constraints | PATRON refinement with a TritonPart initializer; explicit TritonPart, RePart, MFSPart, and greedy alternatives |
| 4 | Route cross-FPGA multicast communication | Native C++ system router with topology, congestion, and timing costs |
| 5 | Assign TDM ratios, lanes, and slots to routed communication | Native ratio and slot optimization; dependency-aware timing-DAG assignment for combinational cuts |
| 6 | Plan pins/lanes, split the netlist, and generate transport RTL | Baseline pin planning; optional Chimew provider; static transport, shadow registers, and frame-barrier synchronization |
| 7 | Implement each FPGA and analyze whole-design timing | VPR/OpenPARF, RapidWright/OpenPARF, or Vivado physical backends; OpenSTA global timing |

Each stage has versioned artifacts and independent structural, legality,
coverage, or equivalence checks. Passing an intermediate stage does not
establish physical timing closure.

See [architecture and phase contracts](docs/FLOW_PLAN.md) and
[algorithm/provider overview](docs/ALGORITHMS.md).

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

Use the CLI produced by the root build:

```bash
export PATH="$PWD/build/native/install/bin:$PATH"
emuflow --help

emuflow arch fetch-default-vtr \
  --output build/architectures/vtr-flagship.xml

emuflow multi-fpga compile examples/rtl/counter.v \
  --top counter \
  --clock clk \
  --clock-period clk=10 \
  --platform platforms/virtual/academic_vtr_2fpga_p2p.json \
  --out build/counter-multi-fpga

emuflow multi-fpga validate --flow build/counter-multi-fpga
```

This example generates and validates the board-independent flow through
netlist splitting and transport. It does **not** request FPGA place-and-route
or demonstrate final physical WNS/TNS.

Physical execution is an explicit `--physical` continuation of compilation
and requires compatible architecture, timing, and backend inputs. Consult
`emuflow multi-fpga compile --help` and the
[physical-flow instructions](docs/USER_GUIDE.md#physical-implementation).

## Configuration and limits

The default CLI configuration uses `vtr-hard-blocks` mapping, timing-driven
PATRON partitioning, `sequential-only` cuts, baseline Phase 6, and the
`open` physical backend when physical execution is requested. Generalized
Static Exact combinational cuts and Chimew are explicit options. Global
physical timing defaults to OpenSTA.

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
  Vivado is proprietary and is not bundled or required by the default VTR
  research path.
- WNS/TNS are scoped to the declared source TimingPathDB population.
  Original-path TNS sums negative slack per original path, not once per
  unique register endpoint. Per-FPGA metrics are separate diagnostics.
  See [timing-model scope](docs/global-opensta-qualification.md).

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
- [Static Exact semantics](docs/STATIC_EXACT_COMBINATIONAL_CUT.md)
- [Contest/Phase 6 interface contract](docs/CONTEST_PHASE6_INTEGRATION_CONTRACT.md)
- [Validation requirements](docs/VALIDATION.md)
- [Contributing](CONTRIBUTING.md)

## License and source provenance

EmuFlow's first-party code is licensed under [Apache-2.0](LICENSE).
Included upstream components retain their own licenses and notices.

The source inventory includes [Yosys/ABC](https://github.com/YosysHQ/yosys),
[OpenROAD/TritonPart](https://github.com/The-OpenROAD-Project/OpenROAD),
[OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA),
[RePart](https://github.com/Welement-zyf/RePart),
[OpenPARF](https://github.com/PKU-IDEA/OpenPARF), and
[VTR/VPR](https://github.com/verilog-to-routing/vtr-verilog-to-routing).
See [the complete component and license inventory](OPEN_SOURCE_COMPONENTS.md),
[its machine-readable form](OPEN_SOURCE_COMPONENTS.json), and
[the implementation source manifest](SOURCE_MANIFEST.json).
