# Install and run EmuFlow

This page explains the ordinary user workflow. You do not need to create an
environment file or register the machine manually.

## What you need

The installer is currently tested on Linux x86-64. Before starting, install:

- Git and Python 3.9 or newer, including Python `venv` support;
- CMake 3.20 or newer, GNU Make and a C++17 compiler;
- a JDK that provides both `java` and `javac`;
- Bison, Flex, SWIG and Tcl development files;
- Boost, SQLite, zlib, Eigen3, spdlog, LEMON, OR-Tools and OpenMP development
  packages.

Package names differ between Linux distributions. The installer handles the
Python packages and RapidWright download itself. Vivado is optional and is only
needed when you choose the Vivado backend.

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
emuflow run rtl/top.sv \
  --top top \
  --clock clk=10 \
  --backend rapidwright
```

The arguments mean:

- `rtl/top.sv`: the RTL source file;
- `--top top`: the top-level module is named `top`;
- `--clock clk=10`: the clock port is named `clk` and its period is 10 ns;
- `--backend rapidwright`: use OpenPARF for placement and RapidWright/RWRoute
  for routing.

The output is written to `runs/top`. Use `--out` to choose another directory:

```bash
emuflow run rtl/top.sv --top top --clock clk=10 \
  --backend rapidwright --out runs/my-first-run
```

Always use a new output directory for a new run.

## Choose a physical backend

The backend is selected separately for each run.

OpenPARF plus RapidWright/RWRoute:

```bash
emuflow run rtl/top.sv --top top --clock clk=10 --backend rapidwright
```

Vivado:

```bash
emuflow run rtl/top.sv --top top --clock clk=10 --backend vivado
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
emuflow run rtl/top.sv --top top --clock clk=10 \
  --backend rapidwright --check
```

## Check a completed result

After a run finishes, validate its published result:

```bash
emuflow multi-fpga validate --flow runs/top --require-physical
```

A successful command launch does not guarantee that placement, routing or timing
will pass. The validation command checks the completed physical result.

## Advanced design configuration

The direct `emuflow run` command is enough for ordinary RTL. Designs that need
explicit input/output timing groups or additional compile options can use
[the design configuration template](../examples/designs/design.example.json):

```bash
emuflow design prepare design.json --out design.run.json
emuflow run design.run.json --backend rapidwright --out runs/design
```

The optional [NVDLA instructions](RUNNING_NVDLA.md) are only for the project's
full-scale validation design. They are not required for normal user RTL.
