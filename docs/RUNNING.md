# Running a design

EmuFlow accepts ordinary RTL directly. It does not require a benchmark catalog,
a design-specific script or machine-specific configuration.

## Install once

The automatic installer and complete physical flow are currently qualified on
Linux x86-64. This is the maintained installation target rather than an
architectural assertion about every source component. RapidWright's
[standalone distribution](https://www.rapidwright.io/docs/Quick_Start.html)
has Linux and Windows x86-64 assets but no macOS asset. OpenROAD
[supports local macOS builds](https://github.com/The-OpenROAD-Project/OpenROAD/blob/master/docs/index.md),
but the combined EmuFlow, OpenPARF, pinned RapidWright and global timing path
has not been qualified there. Native Windows is unsupported; WSL uses the Linux
installation path.

Before installation on the qualified host, provide:

- Git, Python 3.9+ with `venv`, CMake 3.20+, GNU Make and a C++17 compiler;
- a JDK with `java` and `javac`;
- Bison 3.4+, Flex 2.6+, SWIG 4.0+ and Tcl 8.6+ development files;
- Boost 1.78+, SQLite 3.22+, zlib, Eigen3, spdlog, LEMON, OR-Tools and OpenMP
  development packages.

Package names vary by Linux distribution. These are build dependencies rather
than manually registered EmuFlow paths. With them installed, run:

```bash
./install.sh
```

The installer performs the remaining setup itself. It builds the native runtime
and bundled engines, downloads and verifies the pinned RapidWright standalone
release, obtains the required device data, generates the XCVU19P architecture,
fixed-resource and timing contracts, and checks the resulting installation. If
the selected Python lacks NumPy, PyYAML or PyTorch, the installer creates a
private build environment and installs them. The generated runtime manifest
lives under `build/native/install/share/emuflow`; users do not edit or register
it.

The commands are linked into `~/.local/bin`. If that directory is not already on
`PATH`, the installer prints the one path change needed by the shell. Re-running
`./install.sh` reuses verified downloads and generated device artifacts.

Vivado remains an optional external backend because it cannot be redistributed.
When `vivado` is on `PATH` during installation, it is included automatically;
its license is still managed by the vendor installation.

## Run a design

For ordinary RTL, no design or run configuration file is required:

```bash
# OpenPARF placement and RapidWright/RWRoute routing
emuflow run rtl/top.sv --top top --clock clk=10 --backend rapidwright

# Or the Vivado physical route
emuflow run rtl/top.sv --top top --clock clk=10 --backend vivado
```

The command reads the installation-owned runtime, validates the source and tool
inventory, and submits the complete Phase 1–7/7C request to the
native controller. Output defaults to `runs/top`. Multiple sources retain command
line order:

```bash
emuflow run rtl/packages.sv rtl/core.sv rtl/top.sv \
  --top top --clock clk=10 --include-dir rtl/include --define FEATURE=1
```

Repeat `--clock NAME=NS` for every clock. The direct entry applies no external
input/output delay groups; designs that need explicit I/O timing, custom compile
options, or a source-sealed reproducibility record can use the advanced
configuration below. The backend choice applies only to that run; one
installation can support both routes. RapidWright is always installed. Vivado
is available when its executable was on `PATH` during installation; rerun
`./install.sh` after adding Vivado to make that route available.

## Advanced reproducible configuration

Copy [the design template](../examples/designs/design.example.json) to
`design.json` and replace the example fields:

| Field | Meaning |
| --- | --- |
| `schema` | `emuflow.design/v1` |
| `name` | Your design identifier |
| `source_root` | Root containing RTL and included source files |
| `sources` | Ordered source paths or glob patterns relative to `source_root` |
| `include_dirs`, `defines` | Optional source-root-relative include directories and Yosys preprocessor definitions |
| `top` | RTL top module |
| `clock_periods_ns` | Each clock port and its positive period in nanoseconds |
| `timing_io` | Input/output groups, each with `clock`, `delay_ns`, and data `ports`; clocks are declared separately |
| `platform` | Optional BoardDB path; defaults to the bundled four-XCVU19P mesh |
| `options` | Compile options such as `cut_mode`, `frame_slots`, `phase6_provider` or `physical_workers` |

Configuration paths resolve relative to `design.json`; source/include paths
resolve under `source_root`. Source patterns must match files and cannot escape
that root. List sources in HDL dependency order where needed. The template's
port names and 10 ns clock are examples, not inferred design constraints.
Empty input/output group arrays are allowed when the design has no data I/O
requiring those constraints. Provide synthesizable implementations for memories
and other modules; preparation does not synthesize black boxes into missing IP.

```bash
emuflow design prepare design.json --out design.run.json
emuflow run design.run.json --backend rapidwright --out runs/design-openparf
```

Preparation validates the design description and platform and records the
source/include bytes and compile options. It produces a small run configuration
and a sibling benchmark descriptor used internally by the existing frontend;
no manual hashes or internal TimingDB conversion are needed. Keep both files.
Use a new output path outside the source tree. Intentional source or constraint
changes require preparing a new run configuration. Tool availability is checked
by the installer and again before execution.

The optional [NVDLA preset](RUNNING_NVDLA.md) handles that qualification design's
pinned archive and physical memory models. It is not required for user RTL.

## Inspect results

```bash
emuflow multi-fpga validate --flow runs/top --require-physical
```

Choose a new output directory for each invocation. Add `--check` to the same run
command for a source/configuration/runtime inventory check without executing the
design; it does not establish physical completion.

The controller prints stage progress. Inspect `stage-metrics/` and
`native-compile-result.json` for execution state. Successful terminal execution
publishes `multi-fpga-flow-report.json`, `runtime/phase7c_report.json` and
`runtime/qor_report.json`. Independently validate the completed result:

```bash
emuflow multi-fpga validate --flow runs/top --require-physical
```

Missing coverage, unresolved paths and failed implementation remain failures.
Both backends still require full NVDLA terminal qualification; these preparation
commands and configuration checks do not claim timing closure or W7 completion.
