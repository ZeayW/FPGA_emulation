# User guide

## Complete build

Build from the repository root:

```bash
cmake --preset release
cmake --build --preset release --parallel 2
ctest --preset release
export PATH="$PWD/build/native/install/bin:$PATH"
```

The native installation includes the EmuFlow CLI and selected in-tree engines.
Use `emuflow --help` and the relevant subcommand's `--help` for the complete
option list.

For dependencies outside system directories:

```bash
cmake --preset release \
  -DEMUFLOW_CMAKE_PREFIX_PATH=/absolute/path/to/dependencies \
  -DEMUFLOW_OPENPARF_PYTHON=/absolute/path/to/python
```

Multiple dependency prefixes form a semicolon-separated CMake list. The
OpenPARF interpreter, PyTorch ABI, compiler, and optional CUDA toolkit must be
compatible. CUDA is disabled by default; enable it only for a compatible build.

A partial developer build can disable imported engines:

```bash
cmake -S . -B build/core -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DEMUFLOW_BUILD_YOSYS=OFF -DEMUFLOW_BUILD_CUDD=OFF \
  -DEMUFLOW_BUILD_REPART=OFF -DEMUFLOW_BUILD_OPENROAD=OFF \
  -DEMUFLOW_BUILD_OPENSTA=OFF -DEMUFLOW_BUILD_VPR=OFF \
  -DEMUFLOW_BUILD_OPENPARF=OFF
cmake --build build/core --parallel 2
ctest --test-dir build/core --output-on-failure
```

This is not a complete emulation installation. GitHub Codespaces development
helpers are in `scripts/codespaces/`; they also provide partial profiles rather
than a substitute for the complete root build.

## Input configuration

A compile needs RTL sources, a top module, clock identities and periods, and
a BoardDB platform. BoardDB supplies FPGA resource budgets and inter-FPGA link
properties; the mapping/device inputs supply single-FPGA primitive and physical
models.

```bash
emuflow multi-fpga compile examples/rtl/counter.v \
  --top counter --clock clk --clock-period clk=10 \
  --platform platforms/virtual/academic_vtr_2fpga_p2p.json \
  --out build/counter-flow
emuflow multi-fpga validate --flow build/counter-flow
```

This is board-independent generation through Phase 6. A successful
`multi-fpga-flow-report.json` without physical results does not establish
routed timing closure.

Mapping profiles are explicit:

| Profile | Purpose |
| --- | --- |
| `vtr-hard-blocks` | Public VTR LUT/FF, RAM, and multiplier mapping |
| `generic-soft` | Architecture-neutral LUT6/FF studies; memory/arithmetic expansion can be expensive |
| `xilinx-ultrascaleplus-open-v1` | Supported Xilinx UltraScale+ primitive namespace for the corresponding device backend |

Do not change mapping profiles silently to make a capacity failure disappear.
Match resource units, device geometry, and physical architecture to the selected
platform.

## Providers and boundary semantics

The CLI defaults to timing-driven PATRON partitioning with a TritonPart
initializer, sequential-only cut semantics, and baseline Phase 6.
Select alternatives explicitly:

```bash
emuflow multi-fpga compile --help
emuflow partition --help
emuflow multi-fpga physical --help
emuflow phase7c --help
```

`--partition-provider` selects the optimizer. `--cut-mode` separately selects
boundary semantics; `static-exact-combinational` requires the supported
single-virtual-clock contract. See [Static Exact](STATIC_EXACT_COMBINATIONAL_CUT.md).

`--no-timing-driven` changes optimization, not the requirement to carry
original timing identities into a physical flow.

`--phase6-provider chimew` requires compatible placement/physical-region and
grouping inputs. Selecting it is not permission to invent package pins, routing
capacity, or electrical data. The baseline provider does not require a Chimew
physical-lookahead run.

## Physical implementation

Append `--physical` to compilation when a complete physical run is required.
Backend-specific architecture, timing, and constraints must be supplied
consistently.

- `--physical-backend open`: VTR/VPR packing and routing with OpenPARF
  placement. Fetch the public architecture using `arch fetch-default-vtr`,
  bind it with `--physical-architecture`, and configure the compiled OpenPARF
  installation/interpreter using `--physical-openparf-install` and
  `--physical-openparf-python`.
- `--physical-backend rapidwright`: Xilinx device facts and RWRoute with the
  native OpenPARF placer. Supply the compatible provider/device, native
  constraints, architecture/timing data, and runtime paths listed by
  `multi-fpga compile --help`. This path has external RapidWright API/device
  licensing conditions; it is not the fully open VTR route.
- `--physical-backend vivado`: optional proprietary device implementation.
  It requires the matching Vivado installation, device support, and licenses.

Global physical STA uses OpenSTA by default. `--global-sta-executable` overrides
the executable; `--global-timing-engine python` explicitly selects the separate
Python engine. The OpenSTA route does not implicitly run both engines.
A missing engine or unsupported physical model is an error, not a silent
fallback.

For independent terminal validation, require the physical endpoint:

```bash
emuflow multi-fpga validate --flow build/physical-flow --require-physical
```

BoardDB cycle latency is a model unless backed by characterized link data.
An explicit BoardLinkTimingDB provides directed link timing. Missing routed
logic/interface evidence cannot be replaced by a guessed delay.

Bitstreams and board deployment additionally require concrete package pins,
bank/electrical constraints, clock/reset interfaces, transport hardware, and a
board support package. A virtual platform does not supply those facts.

## Output interpretation

- Intermediate cut, congestion, TDM, pin-distance, and placement objectives
  are diagnostics.
- Per-FPGA WNS/TNS describe local physical timing.
- Global target-clock and virtual-runtime-clock results compose original
  logical paths with physical segments and concrete transport events.
- Original-path TNS counts negative slack once per original path, not once
  per unique endpoint.
- Coverage is relative to the declared source TimingPathDB; a capped extraction
  must not be described as all possible circuit paths.
- Exact measured delays, analytical models, and conservative bounds must retain
  their qualifications.

See [timing-model scope](global-opensta-qualification.md) and
[validation requirements](VALIDATION.md).

## Benchmarks and local outputs

Use the [RTL catalog instructions](RTL_BENCHMARKS.md) and checked-in workload
contracts. Contest communication graphs are not synthesizable RTL.

Store run directories, temporary tool products, local assistant configuration,
logs, and experiment records outside the public source tree. Consult CLI help
for specialized diagnostics; do not treat an old run directory or a partial
stage as a newly validated full flow.
