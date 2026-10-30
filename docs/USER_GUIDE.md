# User guide

This guide covers the commands needed after installation. For a first run, start
with the [step-by-step installation guide](RUNNING.md).

## Run a design

Give EmuFlow one or more RTL files, the top module, every clock period, and a
physical backend:

```bash
emuflow run examples/rtl/counter.v \
  --top counter \
  --clock clk=10 \
  --backend rapidwright \
  --out runs/counter
```

Use `--backend vivado` to select Vivado instead. Each run needs a new output
directory.

For multiple files, include directories, definitions, or clocks:

```bash
emuflow run \
  rtl/package.sv rtl/core.sv rtl/top.sv \
  --top top \
  --include-dir rtl/include \
  --define FEATURE=1 \
  --clock core_clk=5 \
  --clock io_clk=10 \
  --backend rapidwright \
  --out runs/multi-clock
```

Before starting a long run, check the inputs and selected backend:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 \
  --backend rapidwright --check
```

## Input configuration

The command line is sufficient when a design only needs RTL sources, a top
module, clock periods, include directories, and preprocessor definitions.

Use a design configuration file when the design also needs explicit I/O timing
or other reproducible input settings. Start from
[`examples/designs/design.example.json`](../examples/designs/design.example.json):

```bash
emuflow design prepare design.json --out design.run.json
emuflow run design.run.json --backend rapidwright --out runs/design
```

The bundled platform is used by default. Select another BoardDB with
`--platform`:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 \
  --platform platforms/my-board.json \
  --backend rapidwright --out runs/my-board
```

Validate a BoardDB before using it:

```bash
emuflow platform validate platforms/my-board.json
```

## Physical implementation

Two physical backends are supported:

- `rapidwright` uses OpenPARF for placement and RapidWright/RWRoute for routing.
  It is the default installed backend.
- `vivado` uses an existing Vivado installation for synthesis, placement, and
  routing. The `vivado` command and required device license must be available.

Select the backend for each run:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 --backend rapidwright
emuflow run examples/rtl/counter.v --top counter --clock clk=10 --backend vivado
```

The selected backend applies to every FPGA in the run. Use
`emuflow run --help` for backend-specific overrides.

## Phase 6 providers

The default Phase 6 provider is `baseline`. Select Chimew explicitly:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 \
  --phase6-provider chimew \
  --backend rapidwright \
  --out runs/counter-chimew
```

Chimew requires inter-FPGA traffic and a physical architecture with package-pin
geometry. Its run includes a baseline physical/timing pass followed by the
candidate, using the same backend, platform, clocks and upstream inputs.

## Continue from completed indexed inputs

To reuse completed synthesis and pre-placement timing, call the advanced compile
command with both saved phase directories:

```bash
emuflow multi-fpga compile \
  --prepared-frontend PHASE1_DIRECTORY \
  --prepared-timing PHASE2_DIRECTORY \
  --clock-period clk=10 \
  --platform platforms/my-board.json \
  --physical \
  --physical-backend rapidwright \
  --out runs/continued
```

The saved inputs must come from the same design, mapping profile, device, and
clock periods. EmuFlow validates those identities before continuing. The
platform passed to the new command supplies the Phase 3–7 topology and resource
model.

## Native compile control

`emuflow run` and `emuflow multi-fpga compile` submit the flow to the installed
native controller. It executes the phases in order and stops on the first
failure. A failed operation does not switch algorithms or physical backends.
Use `emuflow multi-fpga compile --help` for the complete advanced option list.

## Validate a completed run

Validate all published artifacts after the flow completes:

```bash
emuflow multi-fpga validate --flow runs/counter --require-physical
```

Without `--require-physical`, validation accepts an upstream-only flow. Keep the
flag for results intended to include placement, routing, and global timing.

## Output interpretation

The run directory contains stage outputs and these top-level results:

- `multi-fpga-flow-report.json` records overall flow completion;
- `stage-metrics/` contains per-stage runtime and resource measurements;
- `runtime/phase7c_report.json` contains global timing-stage results;
- `runtime/qor_report.json` contains final quality-of-result evidence.

A complete run must pass `emuflow multi-fpga validate --require-physical`.
Per-FPGA timing reports describe local implementation. The Phase 7C report
combines physical measurements with scheduled cross-FPGA communication events.
See [validation requirements](VALIDATION.md) for the acceptance checks and
[global timing scope](global-opensta-qualification.md) for WNS/TNS definitions.

## Complete build

For ordinary installation, use:

```bash
./install.sh
```

For development, configure, build, and test the repository directly:

```bash
cmake --preset release
cmake --build --preset release --parallel 2
ctest --preset release
```

The commands and runtime data are installed below `build/native/install/`.
Add its binary directory to `PATH` when running that build without `install.sh`:

```bash
export PATH="$PWD/build/native/install/bin:$PATH"
```

When dependencies are installed outside system paths:

```bash
cmake --preset release \
  -DEMUFLOW_CMAKE_PREFIX_PATH=/absolute/path/to/dependencies \
  -DEMUFLOW_OPENPARF_PYTHON=/absolute/path/to/python
```

Use a semicolon-separated list for multiple CMake prefixes. The selected
OpenPARF Python, PyTorch, compiler, and optional CUDA toolkit must be compatible.
CUDA is disabled by default.

A core-only developer build can omit bundled engines:

```bash
cmake -S . -B build/core -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DEMUFLOW_BUILD_YOSYS=OFF \
  -DEMUFLOW_BUILD_CUDD=OFF \
  -DEMUFLOW_BUILD_OPENROAD=OFF \
  -DEMUFLOW_BUILD_OPENSTA=OFF \
  -DEMUFLOW_BUILD_OPENPARF=OFF
cmake --build build/core --parallel 2
ctest --test-dir build/core --output-on-failure
```

This build supports core development and tests; it cannot run the complete
physical flow.

## Troubleshooting

Check the installed tools and device data:

```bash
emuflow environment check --backend rapidwright
emuflow environment check --backend vivado
emuflow-native check-runtime
```

If the shell cannot find `emuflow` after `./install.sh`, add the installation
location to `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

For a failed run, inspect the first failing stage in the terminal output and its
report in the run directory. Re-run input checks after changing RTL, clocks,
platform data, backend tools, or installation paths. Start the next execution in
a new output directory.

## Benchmarks and local outputs

The [RTL catalog](RTL_BENCHMARKS.md) lists checked workloads and their source
revisions. The [NVDLA guide](RUNNING_NVDLA.md) covers the full-scale validation
design.

Keep run directories, temporary tool outputs, and local logs outside the source
tree. They are generated data and should not be committed.
