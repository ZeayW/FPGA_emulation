# Running a design

The preparation and execution commands accept user RTL; they do not require a
benchmark catalog or a design-specific script. The maintained target is Xilinx
UltraScale+ with the registered device data and BoardDB, using either
OpenPARF/RWRoute or Vivado. Machine registration is reusable across designs.

## Register the machine once

Build and install the [complete runtime](USER_GUIDE.md#complete-build). Make its
`bin` directory available in the shell used for setup and execution. A Python-only
installation does not supply Yosys, OpenSTA, OpenROAD, OpenPARF or the native core.

For the default source-checkout installation, select the built launcher once
in the setup shell (or register its absolute location in your shell profile):

```bash
export PATH="$PWD/build/native/install/bin:$PATH"
```

Copy [the environment template](../examples/environments/xcvu19p.example.json)
to a machine-local file and replace its example paths with real inputs:

- `native_root`: the complete root-build installation, with `emuflow-native`,
  `yosys`, `sta` and `openroad` under `bin`.
- `timing_data_dir`: the pinned RapidWright delay-model data used by the current
  pre-placement model. `intrasite_delay_terms.txt` and
  `intersite_delay_terms.txt` must match the source hashes in the timing contract.
- `architecture`, `native_constraints`, `provider_manifest`: the matching,
  source-qualified XCVU19P device exports. Follow the existing
  [device-data preparation](XILINX_PHYSICAL_FLOW.md); arbitrary files with these
  names are insufficient.
- `backends.rapidwright`: the actual RapidWright standalone JAR, JDK `java`
  (with sibling `javac`), device-data directory, compiled OpenPARF installation
  and its matching Python/PyTorch interpreter. The root build installs the
  maintained Java adapter source; `java_source` can explicitly select a qualified
  alternative snapshot.
- `backends.vivado.executable`: the actual external Vivado executable. An
  installed Vivado and a valid license are the user's responsibility.

Register either or both backends. Remove a backend section when it is not
installed. Device data, external executables and vendor licenses are not supplied
by an example profile. License variables and required loader paths may be
inherited from the launching shell, or explicitly registered under `environment`
(`LM_LICENSE_FILE`, `XILINXD_LICENSE_FILE`, `LD_LIBRARY_PATH`,
`EMUFLOW_OPENPARF_RUNTIME_LIBRARY_PATH`). Keep profiles with machine or license
information local.

```bash
emuflow environment prepare machine.json \
  --out environment.json

emuflow environment check environment.json
```

Preparation reports missing inputs before starting EDA work, loads the native
runtime and selected OpenPARF host, checks OpenSTA and vendor/JDK identities, and builds the
internal source-sealed pre-placement TimingDB once. Subsequent runs consume its
checked reference. Relative paths in profiles resolve from the profile directory.
Changing the source timing revision requires preparing a new environment.
Vendor license availability is enforced by the vendor tool during execution.

## Prepare a design

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
emuflow design prepare design.json --environment environment.json --out design.run.json
```

Preparation validates the design description and platform and records the
source/include bytes and compile options. It produces a small run configuration
and a sibling benchmark descriptor used internally by the existing frontend;
no manual hashes or internal TimingDB conversion are needed. Keep both files.
Use a new output path outside the source tree. Intentional source or constraint
changes require preparing a new run configuration. Tool availability is checked
at environment registration and again before execution.

The optional [NVDLA preset](RUNNING_NVDLA.md) handles that qualification design's
pinned archive and physical memory models. It is not required for user RTL.

## Run and inspect

```bash
emuflow run design.run.json --backend rapidwright --out runs/design-openparf
emuflow run design.run.json --backend vivado --out runs/design-vivado
```

Choose a registered backend and a new output directory for each invocation.
Both commands submit to the sole `emuflow-native compile` controller for
Phase 1–7/7C. Add `--check` for a source/configuration/runtime inventory check
without executing the design; it does not establish physical completion.

The controller prints stage progress. Inspect `stage-metrics/` and
`native-compile-result.json` for execution state. Successful terminal execution
publishes `multi-fpga-flow-report.json`, `runtime/phase7c_report.json` and
`runtime/qor_report.json`. Independently validate the completed result:

```bash
emuflow multi-fpga validate --flow runs/design-openparf --require-physical
```

Missing coverage, unresolved paths and failed implementation remain failures.
Both backends still require full NVDLA terminal qualification; these preparation
commands and configuration checks do not claim timing closure or W7 completion.
