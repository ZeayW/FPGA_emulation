# Running NVDLA

The user entry is `emuflow run RUN_CONFIG --backend BACKEND --out OUTPUT`.
It always submits to `emuflow-native compile` and starts Phase 1 through physical
implementation and Phase 7C global OpenSTA. It does not run another orchestrator
or replace NVDLA with a small example. Completing these setup steps establishes
inputs and a runnable invocation; successful routed/timing closure still requires
the resulting full NVDLA evidence.

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
  --out "$HOME/.config/emuflow/xcvu19p.json"

emuflow environment check "$HOME/.config/emuflow/xcvu19p.json"
```

Preparation reports missing inputs before starting EDA work, loads the native
runtime and selected OpenPARF host, checks OpenSTA and vendor/JDK identities, and builds the
internal source-sealed pre-placement TimingDB once. Subsequent runs consume its
checked reference. Relative paths in profiles resolve from the profile directory.
Changing the source timing revision requires preparing a new environment.
Vendor license availability is enforced by the vendor tool during execution.

## Prepare the pinned NVDLA design once

From the source checkout, fetch the existing catalog revision and archive:

```bash
python3 scripts/benchmarks/fetch.py fetch nvdla --destination build/rtl

emuflow design prepare-nvdla \
  --source-root build/rtl/nvdla \
  --environment "$HOME/.config/emuflow/xcvu19p.json" \
  --out nvdla.run.json
```

The fetcher verifies the archive hash and writes the source stamp. The preparation
command uses the existing pinned nvdlav1 revision
`8e06b1b9d85aab65b40d43d08eec5ea4681ff715`, top `NV_nvdla`,
`dla_core_clk` and `dla_csb_clk` at 10 ns, and the current grouped I/O timing.
It generates the qualified `physically-implementable-shared-memory-model-v1`
SRAM models and compatibility sources. Black-box scale-screening contracts are
rejected by the physical NVDLA run entry.

The run configuration records the benchmark digest, original/include/generated
source-byte identity and the bundled four-XCVU19P platform. Generated source
files are reused without rewriting a live generation. Every launch checks those
identities; the native frontend binds all declared source/include files through
its tool execution. Prepare a new configuration after intentional source changes.

The prepared configuration explicitly records the established NVDLA choices:
PATRON v14 after TritonPart initialization, Static Exact combinational cuts,
depth 8, at least four used FPGAs, balance tolerance 2.1, 64 PATRON moves,
1024 frame slots, physical seed 1 and one physical worker. These are reproducible
run choices, not a new change to algorithm defaults or proof they are optimal.
Configuration options may explicitly select the retained Chimew provider;
fixed wiring, electrical checks and its comparison contract remain required.

## Run and inspect

```bash
emuflow run nvdla.run.json --backend rapidwright --out runs/nvdla-openparf
emuflow run nvdla.run.json --backend vivado --out runs/nvdla-vivado
```

Each invocation uses a new output directory. Tools, source lists, internal
TimingDB paths and physical handoffs do not need to be assembled again. For a
pre-execution inventory check use the same command with `--check`; its report
explicitly excludes terminal qualification.

The native controller prints completed stages. Inspect `stage-metrics/` and
`native-compile-result.json` for execution state. Successful terminal execution
publishes `multi-fpga-flow-report.json`, `runtime/phase7c_report.json` and
`runtime/qor_report.json`. Validate the complete physical and global timing
contract with:

```bash
emuflow multi-fpga validate --flow runs/nvdla-openparf --require-physical
```

Unresolved paths, missing physical coverage and failed implementation stages
remain failures. No backend or old Python production chain is selected as a
fallback. `native_data_plane_complete` remains false while remaining production
adapters and NVDLA terminal qualification are unfinished.
