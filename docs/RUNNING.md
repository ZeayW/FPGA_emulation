# Install and run EmuFlow

Follow these steps to install EmuFlow and run an RTL design.

## What you need

Supported installer platform: Linux x86-64. Before starting, install:

- Git and Python 3.9 or newer, including Python `venv` support;
- CMake 3.20 or newer, GNU Make and a C++17 compiler;
- a JDK that provides both `java` and `javac`;
- Bison, Flex, SWIG and Tcl development files;
- Boost, SQLite, zlib, Eigen3, spdlog, LEMON, OR-Tools and OpenMP development
  packages.

Package names differ between Linux distributions. The installer prepares the
Python packages and RapidWright. The Vivado backend requires an existing Vivado
installation.

## Install once

From the repository root, run:

```bash
./install.sh
```

This builds EmuFlow and prepares the device files it needs. It can take a while
on the first run. Verified downloads and generated files are reused when the
installer is run again.

The installer places the commands in `~/.local/bin`. If your shell cannot find
`emuflow` after installation, add that directory to `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Run your RTL

For a design with one RTL file, run:

```bash
emuflow run examples/rtl/counter.v \
  --top counter \
  --clock clk=10 \
  --backend rapidwright
```

The arguments mean:

- `examples/rtl/counter.v`: the included RTL example;
- `--top counter`: the top-level module is named `counter`;
- `--clock clk=10`: the clock port is named `clk` and its period is 10 ns;
- `--backend rapidwright`: use OpenPARF for placement and RapidWright/RWRoute
  for routing.

The output is written to `runs/counter`. Use `--out` to choose another directory:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 \
  --backend rapidwright --out runs/my-first-run
```

Always use a new output directory for a new run.

## Choose a physical backend

The backend is selected separately for each run.

OpenPARF plus RapidWright/RWRoute:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 --backend rapidwright
```

Vivado:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 --backend vivado
```

Vivado must already be installed and its `vivado` command must be available in
`PATH` when `./install.sh` is run. Run the installer again after adding Vivado.

## Use multiple RTL files

List source files in the order required by the HDL frontend:

```bash
emuflow run \
  rtl/packages.sv rtl/core.sv rtl/top.sv \
  --top top \
  --clock clk=10 \
  --include-dir rtl/include \
  --define FEATURE=1 \
  --backend rapidwright
```

Repeat `--clock NAME=PERIOD_NS` when the design has more than one clock.

## Check inputs without running the full flow

Add `--check` to validate the sources, selected backend and installation without
starting the expensive compile:

```bash
emuflow run examples/rtl/counter.v --top counter --clock clk=10 \
  --backend rapidwright --check
```

## Check a completed result

After a run finishes, validate its published result: Install the
[optional tools](../tools/README.md) for independent audits and input preparation.

```bash
emuflow-tools validate-flow --flow runs/counter --require-physical
```

This command checks placement, routing, timing and the completed physical result.

## Advanced design configuration

The direct `emuflow run` command is enough for ordinary RTL. Designs that need
explicit input/output timing groups or additional compile options can use
[the design configuration template](../examples/designs/design.example.json):

```bash
emuflow-tools design prepare design.json --out design.run.json
emuflow run design.run.json --backend rapidwright --out runs/design
```

The [NVDLA instructions](RUNNING_NVDLA.md) cover the project's full-scale
validation design.
