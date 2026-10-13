# EmuFlow

EmuFlow is a research-oriented multi-FPGA emulation flow. It compiles
synchronous RTL into per-FPGA netlists and a statically scheduled communication
fabric, with explicit interfaces between synthesis, partitioning, system
routing, TDM assignment, physical implementation, and global timing analysis.

The default research route uses **RapidWright device resources, native OpenPARF
placement, and RWRoute routing** for supported Xilinx FPGAs. VTR/VPR remains an
alternative academic backend, and Vivado is an optional proprietary backend.
Device resources and the inter-FPGA platform model are separate: a virtual
BoardDB is an academic model, not a deployable board or evidence of hardware
signoff. See [backend selection](#configuration-and-limits) for the explicit CLI
options required to select the research route.

## Flow

| Phase | Responsibility | Algorithms and tools |
| --- | --- | --- |
| 1 | Synthesize and map RTL; preserve connectivity, primitive parameters, and resource demand in EmuIR | Yosys and ABC; mapping profiles for VTR hard blocks, generic soft logic, and Xilinx UltraScale+ |
| 2 | Extract timing paths and derive optimization weights | OpenSTA; optional Vivado timing provider |
| 3 | Cluster and partition against resource and topology constraints | PATRON refinement with a TritonPart initializer; explicit TritonPart, RePart, MFSPart, and greedy alternatives |
| 4 | Route cross-FPGA multicast communication | Native C++ system router with topology, congestion, and timing costs |
| 5 | Assign TDM ratios, lanes, and slots to routed communication | Lagrangian ratio optimization, discrete legalization, and slot refinement; concrete event binding for supported combinational cuts |
| 6 | Plan pins/lanes, split the netlist, and generate transport RTL | Baseline pin planning; optional Chimew provider; static transport, shadow registers, and frame-barrier synchronization |
| 7 | Implement each FPGA and analyze whole-design timing | RapidWright/OpenPARF/RWRoute research route; alternative VPR/OpenPARF or Vivado backends; OpenSTA global timing |

Each stage has versioned artifacts and independent structural, legality,
coverage, or equivalence checks. Passing an intermediate stage does not
establish physical timing closure.

See [architecture and phase contracts](docs/FLOW_PLAN.md) and
[algorithm/provider overview](docs/ALGORITHMS.md).

## Algorithms and research foundations

EmuFlow integrates upstream EDA engines and implements the multi-FPGA glue and
system optimizers around them. The descriptions below distinguish direct tool
integration, implementations based on published methods, and EmuFlow-specific
extensions. A paper citation identifies the method's origin; it does not imply
that EmuFlow reproduces that paper's complete experimental setup or results.

### Phase 1: RTL synthesis and technology mapping

[Yosys](https://github.com/YosysHQ/yosys) elaborates RTL, lowers processes and
memories, and maps the design to the selected primitive library.
[ABC](https://github.com/YosysHQ/abc) supplies logic optimization and technology
mapping through the Yosys flow. The mapping profile determines whether RAM,
DSP, carry, and LUT structures are represented by architecture-specific hard
blocks or soft logic. These choices must agree with the physical backend.

EmuFlow's contribution is the frontend integration and EmuIR representation:
stable cell/net identities, connectivity, primitive parameters, and resource
demand shared by the later stages. The VTR and Xilinx profiles describe
different target models, not interchangeable LUT-capacity units.

### Phase 2: timing extraction

[OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) analyzes the mapped
netlist under the declared timing model and clock constraints. EmuFlow exports
the resulting path population, endpoint identities, and slack into
TimingPathDB, then derives criticality information for partitioning, routing,
and TDM optimization. A Vivado adapter is available for supported vendor flows.

This is pre-physical timing: inter-FPGA route, TDM events, and routed FPGA delays
are resolved later. Phase 7 performs the final global analysis.

### Phase 3: multi-resource hypergraph partitioning

[TritonPart in OpenROAD](https://openroad.readthedocs.io/en/latest/main/src/par/README.html)
provides multilevel hypergraph partitioning: coarsening, initial partitioning,
and uncoarsening with refinement. Multiple vertex weights represent resources
such as LUTs, registers, memories, and DSPs. Its timing-aware formulation also
accounts for critical paths and repeated partition crossings; see the
[TritonPart project and publication](https://github.com/ABKGroup/TritonPart).

The default **PATRON** provider is an EmuFlow extension initialized by
TritonPart on the same cluster graph. Its native direct K-way refinement
evaluates predicted path slack, directed communication load/TDM pressure,
partition transitions, and cut cost while preserving resource, fixed-placement,
reachability, and hop constraints. Incremental updates and bounded paired moves
extend the single-cluster move neighborhood. PATRON is our algorithm name,
not an upstream partitioner or a claim to reproduce one particular paper.

Explicit alternatives include upstream **RePart**, with an optional
logic-replication route ([RePart paper](#selected-research-references)), and
EmuFlow's separately implemented **MFSPart-based** multilevel provider
([MFSPart paper](#selected-research-references)). Greedy partitioning remains a
baseline. Boundary policy is a separate choice: sequential-only and generalized
Static Exact admit different cut locations. Exact transport-event legality is
established after routing and TDM assignment, not by a Phase 3 timing proxy.

### Phase 4: system-level multicast routing

EmuFlow's native C++ router connects each cross-FPGA net's source to its sinks
on the BoardDB graph. It supports shortest-path and delay-demand-balanced tree
construction, additional Steiner-style candidates, and iterative rerouting.
Candidate costs account for delay, congestion, shared link capacity, and TDM
pressure, with separate treatment of intra-device SLL and inter-FPGA links
when the platform provides that distinction.

The research basis includes Wang et al.'s **Synergistic Die-Level Router**
(DAC 2025) and Chen et al.'s **Timing-Aware Optimization of Die-Level Routing
and TDM Assignment** (ASP-DAC 2026); links are in the
[reference list](#selected-research-references). The in-tree implementation
extends these ideas with EmuFlow's route contracts and candidate selection.
System routing selects FPGA/die paths; it does not route wires inside an FPGA.

### Phase 5: TDM ratio, lane, and slot assignment

The native ratio optimizer uses **Lagrangian relaxation/KKT updates**, followed
by discrete ratio legalization and local refinement. Its foundations are
Pui and Young's TODAES 2020 TDM method and the ASP-DAC 2026 timing-aware method.
The implementation includes a timing-DAG continuous optimizer and a separate
concrete slot optimizer; a continuous ratio alone is not an executable
communication assignment.

EmuFlow then materializes legal lanes and slots under the platform's ratio,
direction, compatibility, and shared-capacity rules. Slot refinement evaluates
concrete transmission events. For supported combinational cuts, the binding
also records when upstream values become available and when downstream
transmissions consume them. Those runtime bindings are EmuFlow integration
work, distinct from the cited ratio-optimization algorithms.

### Phase 6: pin planning, netlist splitting, and transport

The **baseline** consumes the Phase 5 lane/slot assignment directly. The
optional **placement-aware** provider adds compatible signal grouping,
placement-proximity refinement, and minimum-cost group-to-virtual-pin matching.
Its normalized region model is an EmuFlow baseline.

The optional **Chimew** provider implements methods from *TDM Signal Grouping
and Package Pin Assignment for 2.5D Multi-FPGA Systems with Lookahead Placement*
(FPGA 2026): crossing-encoding grouping, position refinement, and two-stage
bank/channel assignment using min-cost flow. OpenPARF lookahead placement and
RUDY congestion estimates supply physical guidance. The implementation keeps
paper-derived kernels separate from EmuFlow's electrical, slot, and transport
adapters. Academic region estimates must be distinguished from actual SLR/SLL
data, and package binding requires a declared pin/electrical model.

All providers feed the EmuFlow netlist splitter and transport generator.
Static TDM communication, shadow state, and frame synchronization implement
the declared virtual-cycle semantics. These runtime mechanisms and their
equivalence checks are project implementations, not part of Chimew.

### Phase 7: FPGA physical implementation and global timing

The physical backends share a result contract but use different device models:

| Backend | Device model and physical algorithms | Upstream foundation |
| --- | --- | --- |
| RapidWright (default research route) | Supported Xilinx device resources and site/BEL connectivity; EmuFlow device/netlist adapters; native OpenPARF placement and validated site/BEL binding; RWRoute detailed routing | [RapidWright](https://github.com/Xilinx/RapidWright), [FPGA Interchange](https://github.com/chipsalliance/fpga-interchange-schema), [RWRoute](https://www.rapidwright.io/docs/RWRoute_timing_driven_routing.html) |
| VTR (alternative academic route) | VTR architecture XML; VPR architecture-aware packing, routing-resource graph and detailed routing; OpenPARF analytical placement, legalization and detailed placement, with VPR placement available as a baseline | [VTR/VPR](https://docs.verilogtorouting.org/en/latest/vpr/), [OpenPARF](https://github.com/PKU-IDEA/OpenPARF) |
| Vivado | Vendor synthesis/implementation integration and physical reports for supported inputs | Optional proprietary backend |

OpenPARF's heterogeneous placement framework and RapidWright/RWRoute's device
and routing work are cited below. RapidWright device support does not itself
supply a multi-FPGA board topology, cable specification, or board-link latency;
those belong to the separate platform model.

Finally, EmuFlow binds physical logic/interface delays, board-link delays, and
the selected TDM events to an abstract global timing model. **OpenSTA is the
default global STA engine**. The internal timing composer is a separate engine
option for explicit comparisons, not an implicit prerequisite for OpenSTA.
The binding/export layer is EmuFlow code; the STA engine computes timing on the
exported model. See [timing scope and metric definitions](docs/global-opensta-qualification.md).

### Selected research references

These references explain implemented methods and upstream foundations. Exact
source revisions and licenses are maintained in
[the component inventory](OPEN_SOURCE_COMPONENTS.md); implementation entry
points are mapped in [Algorithms and providers](docs/ALGORITHMS.md#implementation-and-reference-map).

- **TritonPart** — multilevel, multi-constraint partitioning, ICCAD 2023.
  [Author project and paper](https://github.com/ABKGroup/TritonPart).
- **RePart: Efficient Hypergraph Partitioning with Logic Replication Optimization
  for Multi-FPGA System** — 2026 preprint.
  [Paper](https://arxiv.org/abs/2604.00780), [source](https://github.com/Welement-zyf/RePart).
- **MFSPart: A Generalized Partitioning Framework for Multi-FPGA Systems and Its
  Ensemble-Based Extension** — TCAD 2026.
  [Author manuscript](https://zhiyaoxie.com/files/TCAD26_MFSPart.pdf).
- **Synergistic Die-Level Router for Multi-FPGA System with Time-Division
  Multiplexing Optimization** — Wang et al., DAC 2025.
  [Author manuscript](https://yibolin.com/publications/papers/ROUTE_DAC2025_Wang.pdf).
- **Lagrangian Relaxation-Based Time-Division Multiplexing Optimization for
  Multi-FPGA Systems** — Pui and Young, TODAES 2020.
  [Paper](https://cwpui.com/doc/j4.pdf), [DOI](https://doi.org/10.1145/3377551).
- **Timing-Aware Optimization of Die-Level Routing and TDM Assignment for
  Multi-FPGA Systems** — Chen et al., ASP-DAC 2026.
  [Author manuscript](https://numbda.cs.tsinghua.edu.cn/papers/aspdac262.pdf).
- **Chimew: TDM Signal Grouping and Package Pin Assignment for 2.5D Multi-FPGA
  Systems with Lookahead Placement** — Wang et al., FPGA 2026.
  [Paper](https://yibolin.com/publications/papers/FPGA_ISFPGA2026_Wang.pdf),
  [DOI](https://doi.org/10.1145/3748173.3779199).
- **OpenPARF: An Open-Source Placement and Routing Framework for Large-Scale
  Heterogeneous FPGAs with Deep Learning Toolkit** — 2023.
  [Paper](https://arxiv.org/abs/2306.16665), [source](https://github.com/PKU-IDEA/OpenPARF).
- **RapidWright: Enabling Custom Crafted Implementations for FPGAs** — FCCM 2018.
  [Official publication page](https://www.rapidwright.io/docs/Papers.html).
- **RWRoute: An Open-source Timing-driven Router for Commercial FPGAs** —
  [Author manuscript](https://www.rapidwright.io/docs/_downloads/3858e01e500602e00f7d5dda065b19ec/RWRoute_final_submitted.pdf).

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

Use the CLI produced by the root build. This small frontend/transport example
explicitly selects the alternative VTR profile; it is not a RapidWright
physical-flow example:

```bash
export PATH="$PWD/build/native/install/bin:$PATH"
emuflow --help

emuflow arch fetch-default-vtr \
  --output build/architectures/vtr-flagship.xml

emuflow multi-fpga compile examples/rtl/counter.v \
  --top counter \
  --clock clk \
  --clock-period clk=10 \
  --mapping-profile vtr-hard-blocks \
  --physical-backend open \
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

The default research route is RapidWright. Select
`--physical-backend rapidwright --mapping-profile xilinx-ultrascaleplus-open-v1`
and supply the matching device/provider, architecture, timing, and runtime
inputs described in the [user guide](docs/USER_GUIDE.md#physical-implementation).
Add `--physical` to execute placement and routing.

**CLI selection caveat:** the current command-line parser still selects `open`
and `vtr-hard-blocks` when these two options are omitted. Set both explicitly
for RapidWright research runs; a bare invocation does not select that route.

Other CLI defaults are timing-driven PATRON partitioning, `sequential-only`
cuts, baseline Phase 6, and OpenSTA global physical timing. Generalized Static
Exact combinational cuts and Chimew are explicit options.

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
