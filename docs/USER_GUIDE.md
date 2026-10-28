# User guide

## Continue from completed indexed inputs

Use `multi-fpga compile --prepared-frontend PHASE1_DIRECTORY
--prepared-timing PHASE2_DIRECTORY` with the original mapping profile and an
explicit `--clock-period NAME=NS` for every original clock. The saved Phase 1
must contain the indexed design, passing primitive audit, and normalized
BoardDB. Phase 2 must contain the indexed path and net-weight manifests and
their SQLite payloads. The continuation validates design, mapping, device,
clock periods, publication counts, and SQLite versions without rerunning
synthesis or pre-partition STA.

The saved BoardDB is read only for its device part identity. Its historical
interconnect fields are not interpreted or migrated. The explicitly selected
current BoardDB is validated in full and supplies all Phase 3–7 capacities,
topology, and link models. Missing or changed saved device identities still
reject the continuation. Input databases remain read-only. Terminal validation
accepts the EmuIR symlink only when it resolves to the input declared in the
passing Phase 1 continuation report, and hashes those original bytes as part
of the flow artifact seal. Other flow artifacts still reject symlinks.

## Retired contest topology experiments

The EDA2025 topology-mutation, rerouting and scoring flow is retired, including
its CLI commands, fetch catalog, interchange schemas and standalone optimizer.
The retained system-routing provider uses the fixed BoardDB topology. Public
EDA2023 and EDA2024 format checks retain sealed import and evaluation support.
Historical EDA2025 experiments use their matching Git revision.

## Optional research and dataset tools

Install the main runtime with `python3 -m pip install .`. The optional tools
are a separate distribution: `python3 -m pip install ./tools`. They depend on
the installed runtime; the runtime does not import or install them.

Use `emuflow-tools contest ...`, `emuflow-tools benchmark ...`, and
`emuflow-tools platform arm-mps4-materialize ...` for dataset preparation and
independent BoardDB materialization. PPro calibration uses the separately
installed `emuflow-ppro-calibration` entry point. Direct imports use
`emuflow_tools`, while current frontend input and transport-cost contracts
remain in the runtime. From a source checkout, include `tools` in `PYTHONPATH`.

The hardware-BSP, serial-wrapper, PHY-provider, runtime-sync materialization,
Phase 6B/6C and board-integrated Vivado chains are retired, including their
CLIs, schemas and provider assets. The maintained Phase 6 baseline/Chimew,
fixed wiring and electrical checks, and both per-FPGA physical backends remain.

## Installed runtime data

The root CMake `emuflow_python` target installs a complete current Python
package, replacing its prior module inventory so deleted adapters do not remain
importable. It bundles the default Xilinx primitive library together with
BoardDB, I/O inventory and SQL schemas; resource changes invalidate the install
stamp. The `emuflow_installed_python_runtime` CTest imports only the installed
package and verifies its module/data inventory and default profile resources.
It requires the Python install target to have been built, as in the normal
root build. This check does not execute synthesis or physical implementation.

## Native PATRON model and ownership

PATRON v14 consumes the compact source, primary cluster columns and indexed
TimingPathDB directly. Its Python graph reconstruction, packed pressure trees,
text/pipe model writer, legacy output parser and full-trace replay flow are
retired. Current typed model requests and read-only ownership borrowing remain
in the narrow native host. Small native-model fact fixtures cover endpoint
identity, multicast, duplex capacity, timing objectives and resource bounds;
source-owned assignment validation separately checks membership and legality.

## Native partition hop contracts

Hop legality and repair consume compact EmuIR, the primary cluster store and
source-bound ownership through the native stage. PATRON v14 and TritonPart
retain independent original-endpoint, fixed-owner and resource audits. The
Python numeric/text refiner, packed graph writer, result parser and standalone
`phase3 refine-hops` command are retired. Board shortest routes and distances
are bounded configuration helpers; they do not construct a circuit model.
The retained C++ hop kernel has direct API tests for its large-instance repair
guard, minimum-used-part repair and invalid sink rejection.

## Native system routing

Phase 4 runs as `emuflow-native phase4 run ASSIGNMENT BOARD POLICY OUTPUT_DIR
[TIMING_PATHS]`. The current assignment index, BoardDB and optional timing-path
file are bound by generation in a small policy document. The C++ stage reads
cut records, normalizes or consumes compressed timing paths, invokes the
retained global candidate router, independently checks routes, candidates and
timing, and atomically publishes RouteStore v3 with its selector and report.
The Python API only prepares bounded configuration and reads the stage report.
Its assignment argument names the explicit `assignment.sqlite` artifact; the
JSON assignment summary is no longer a routing input.

The native compile controller invokes this stage directly. The old Python
routing model, kernel-result parser, projected route/selection classes,
publication adapter and diagnostic candidate-pool JSON option are retired.
Registered round-barrier routing is retired; Static Exact transport rounds
remain dependency diagnostics. Neither migration changes the routing provider
or its search algorithm.

The `phase4 seal-routes STORE REQUEST SUMMARY` API remains an independent
ordered-record seal checker. It does not provide another routing producer.
The mapping-to-store writer is used to construct explicit test/import records;
it cannot accept private kernel-output callbacks or retry a failed stage.

## Native compile control

`emuflow multi-fpga compile` always uses the C++ one-shot controller.
The CLI resolves benchmark/BoardDB inputs and sends a bounded request to
`emuflow-native compile REQUEST_JSON`. The public Python `run_multi_fpga_flow`
API submits the same request. There is no control-implementation selector,
alternate Python controller or fallback after failure. The controller chooses
the stage order and owns cancellation, subprocess cleanup and handoffs.
Ordinary RTL preparation, pre-placement timing, partitioning, system routing,
exact scheduling and baseline splitting run in native phase processes.
Supplied/prepared input binding, configuration preparation, Chimew and
physical/signoff/final reporting remain explicit isolated adapters. Existing
provider, algorithm, seed and physical-policy defaults are unchanged.

The native controller supports baseline splitting and retained Chimew comparison,
with either OpenPARF/RWRoute or Vivado physical implementation and global timing.
Chimew retains its baseline physical/global timing prepass, source-qualified
package geometry, fixed wiring, typed pipeline and independent final comparison.
The device pin catalog crosses process boundaries as bounded, source-generation
bound control data; no placement or endpoint population is embedded in it.
Baseline reports have one owner within the comparison and are restored as
references within each phase, rather than serialized twice. This control state
is private to the live invocation and cannot be used as an old-artifact reader.
Frame-length search is not integrated into the production controller; requesting
`optimize_frame_slots=True` rejects before synthesis. Serial-BSP continuation is
retired. No rejected or failed run retries through Python or changes providers.
The result continues to report `native_data_plane_complete=false`; this entry
change does not establish NVDLA terminal qualification or completion of the C++
data-plane migration.

The [request envelope](../schemas/native-compile-request-v1.schema.json) contains
the interpreter invocation, Python source directory, invocation working directory
and current `run_multi_fpga_flow` keyword options. Direct requests require absolute
paths for all Path-valued options; the CLI bridge resolves them. Relative executable
strings retain their original invocation-directory meaning. Options bind to the
current API signature; unknown options reject. Use the exact deployed source and
tool revisions for reproducible runs. File-generation checks detect local changes;
they do not replace portable source and tool hashes in terminal evidence.

Outputs require an empty directory. Phase failure stops the sequence immediately.
Cancellation unwinds adapters and reaps nested native tool owners before removing
control scratch. Control state carries configuration, artifact paths and projected
stage summaries; canonical phase stores and reports remain their owners. The
ephemeral state is neither a checkpoint nor a restart interface.
Each phase process receives its own `TMPDIR`, `TMP`, `TEMP`, `SQLITE_TMPDIR`
and `XDG_CACHE_HOME` below
the controller's scratch directory. After adapter execution, group cleanup and
state validation, the controller removes that phase's temporary workspace before
starting the next phase. Absolute paths in control state that resolve into the
workspace, including output symlinks to temporary files, reject the handoff.
Publish required downstream artifacts in the flow directory; a temporary engine
cache is not a phase publication.

`multi-fpga-flow-report.json` remains the usual flow result. The separate
[native-compile-result.json](../schemas/native-compile-result-v2.schema.json)
uses result v2 and records each stage implementation explicitly. TDM uses
`native-exact-phase5-v1`; system routing uses `native-phase4-v1`; partitioning
uses `native-phase3-v1`; pre-placement timing uses `native-phase2-v1`;
the ordinary RTL frontend uses `native-rtl-frontend-v1`; baseline Phase 6 uses
`native-baseline-phase6-v1`.
Supplied JSON/prepared frontend modes, retained Chimew and remaining phases use
`isolated-python-adapter-v1`. Native RTL preparation retains the current mapping
script and defaults. The stage requires successful Yosys exit and resource/clock
checks before atomically exposing Phase 1; it never retries a rejected import
through a Python frontend. Only a sealed bounded frontend summary crosses the
next control boundary.
Native TDM RSS is the native process high-water value from `wait4`. Baseline
Phase 6 reports the native owner high-water measurement; small-design oracle
children are reaped by that owner. These are kernel high-water measurements,
not sampled simultaneous process-tree peaks. The retired result v1 is read
with its matching historical Git revision. This result
retains requested and resolved configuration, common operation status and stage
wall/CPU/RSS metrics. Its `native_data_plane_complete` is explicitly false.

Both physical backends use the native lowering stage for current typed split
inputs. Its PlacementIR, boundary identity selector/store and report publish
together under `physical/<fpga>/lowering/`. The report identifies
`native-placement-lowering-v1`; this is a component of the physical stage,
whose backend preparation and orchestration are still transitional. Missing
native lowering support fails preflight before transport synthesis. Failures
never retry the former Python indexed lowering path.
Other adapters may report sampled process-tree RSS in `stage-metrics/`; native
TDM uses the explicit `wait4-single-native-process-high-water` measurement label.
Preserve each measurement's scope. Retain the compact
controller result alongside normal terminal validation evidence, and apply the
same experiment cleanup and full Phase 7/7C acceptance requirements.

## Indexed OpenSTA timing inputs and weights

`sta run-opensta` and the Python extraction API require compact EmuIR v2 and
produce the same indexed TimingPathDB as compilation. A standalone command can
consume the Phase 1 `design.emuir.sqlite` directly:

```sh
python3 -m emuflow sta run-opensta --ir build/run/frontend/phase1/design.emuir.sqlite \
  --clock-period clk=10 --output build/timing/path-database.json
python3 -m emuflow sta derive-partition-net-weights \
  --ir build/run/frontend/phase1/design.emuir.sqlite \
  --database build/timing/path-database.json --output build/timing/net-weights.json
```

Each JSON file is a bounded selector for its canonical SQLite payload. Native
weight generation and independent validation preserve maximum path criticality,
the configured power law and exact membership counts. The old inline result
producer and private output-mode/inline-return switches are retired. Legacy
documents remain usable only by their supported explicit readers or the matching
historical tools; they are not current weight-producer inputs. Version/format
errors fail before expensive engine execution when the source format is known.

## Complete build

Run `emuflow-native check-runtime` in the environment that will execute the flow
before launching expensive work. The probe checks native core loading, SQLite
3.22+ and JSON features and creates no design data. Use
`emuflow-native --report runtime-check.json check-runtime` for a compact result.
External synthesis, placement, routing and timing engines require their
separate dependency and device-data checks.

Compact production candidate analysis uses the shared operation
`phase3 build-candidate-ids EMUIR POLICY OUTPUT`. A bounded policy request binds
the source generation; `OUTPUT` contains sealed membership bits, a streamed UTF-8
name pool and integer offsets. Python borrows those columns and sends no graph
or cell-class arrays to the operation. `candidate_implementation_id` reports
`native-candidate-ids-v1`; `native_candidate_metrics` uses the same native metric
scopes as clustering below. Native semantic analysis uses the same source-bound
producer. The former Python graph-characterization command is retired.

The public Phase 3 API executes `phase3 run REQUEST OUTPUT` in the shared core.
It accepts compact indexed EmuIR and the current Static Exact contract. Native
clustering, TritonPart initialization, PATRON v14, candidate selection, exact
semantic construction and artifact publication belong to that one stage owner.
Python prepares bounded board and tool configuration and checks original output
references; it does not coordinate a second graph-level implementation.

`stage_implementation_id` reports `native-phase3-v1`. Native operation metrics
and the sampled whole-stage process-tree measurements have separate scopes.
The stage retains atomic publication and rejects changed source generations.
Independent `validate_phase3_indexed` checks source-to-result legality and
terminal artifact ownership without rebuilding a Python graph. The public CLI
consumes the current assignment index and its phase report:

```bash
emuflow partition validate OUTPUT/assignment.sqlite \
  --phase3-report OUTPUT/phase3_report.json \
  --ir INPUT.emuir.sqlite --platform BOARD.json
```

The former clusters/assignment JSON validator is retired.

The previous Python Phase 3 coordinator, TritonPart exporter/executor and frozen
assignment promotion route are retired. Full diagnostic snapshots, retained
baseline assignments and alternative refiner executables cannot select an old
production chain. Historical tools and source remain available through Git.
Tests retain bounded explicit record fixtures and independent kernel assertions;
they do not retain a second full Python cluster/assignment/semantic producer.
Complete NVDLA physical and global timing qualification is separate from a
Phase 3 stage result.

`emuflow-native [--report OPERATION_JSON] phase7 export-bookshelf MAPPED ATOMS NETS SUMMARY SPOOL`
streams the finalized mapped v2 and private lexical atom relations to OpenPARF
net text. Inputs may be file paths or read-only SQLite file URIs. `SPOOL` must
not exist; the operation removes its owned relation after export or failure.
`NETS` and the primitive-signature `SUMMARY` are staged and published in that
order, with the summary last. All destinations must be distinct from the inputs
and each other. The normal Python adapter selects this shared operation.
The redundant standalone Bookshelf CLI is no longer built or deployed; use
`emuflow-native phase7 export-bookshelf` directly.

Native stage resolution requires `emuflow-native` and never falls back to an
older standalone executable. When `EMUFLOW_NATIVE_ROOT` is set, its installation
is authoritative: missing or nonexecutable tools reject rather than being
borrowed from another checkout or installation. Explicit external-engine paths
remain intentional comparison inputs, not recovery from an incomplete install.

Installed Yosys streaming and OpenSTA extraction use the shared native external
tool supervisor. It keeps diagnostics bounded and preserves the synthesis stdout
pipe into the indexed importer. Cancellation waits for engine process-group
cleanup. The internal `run-tool REQUEST RESULT_JSON` boundary accepts a bounded
versioned binary request; it is not a replacement for the full compile command.
Explicit comparison tools without a unified installation retain their reference
launcher. A selected native supervisor failure is fatal and never retries a tool.

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
OpenSTA qualification checks both its engine version and Tcl 8.6+ `try/finally`
support before an expensive frontend run. Rebuild an executable linked against
an older Tcl; changing only its reported engine version is insufficient.

For native binaries requiring a newer host ABI, the source tree provides
optional container launchers under `scripts/yosys`, `scripts/opensta`,
`scripts/openroad`, and `scripts/patron`. Select each launcher through the
corresponding executable option, configure the shared
`EMUFLOW_YOSYS_CONTAINER_{EXECUTABLE,IMAGE,BIND,LIBRARY_PATH}` variables, and
set each tool's `EMUFLOW_<TOOL>_CONTAINER_BINARY` independently (`YOSYS`,
`OPENSTA`, `OPENROAD`, or `PATRON`). `TMPDIR` must point into the active
experiment. These launchers require an existing Singularity-compatible runtime
and image; they do not install tools or change the host ABI.
If an image lacks Tcl's matching script library, explicitly set
`EMUFLOW_OPENSTA_CONTAINER_TCL_LIBRARY` or
`EMUFLOW_OPENROAD_CONTAINER_TCL_LIBRARY` to that tool's versioned directory
containing `init.tcl`.
PATRON's runtime binary binding survives the native environment filter;
retired implicit algorithm-tuning variables remain excluded.

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
  -DEMUFLOW_BUILD_OPENROAD=OFF -DEMUFLOW_BUILD_OPENSTA=OFF \
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
  --out build/counter-flow
emuflow multi-fpga validate --flow build/counter-flow
```

This command intentionally exercises the current defaults: the bundled
four-XCVU19P research BoardDB and `xilinx-ultrascaleplus-open-v1` mapping. It
runs through Phase 6 and emits the fixed virtual-board inventory, scheduled
pin binding, per-FPGA XDC and differential-I/O adapters, and transport RTL.
The small counter is a smoke input; it is not a representative multi-FPGA QoR
workload. A successful `multi-fpga-flow-report.json` without physical results
does not establish routed timing closure.

Select another BoardDB with `--platform`. The only supported compile mapping
profile is `xilinx-ultrascaleplus-open-v1`; `generic-soft`, `vtr-hard-blocks`,
and the standalone VTR synthesis API/command are retired. The private streaming
LUT6/FF helper is still used for transport synthesis inside physical adapters;
it is not another upstream flow.

Do not change mapping profiles silently to make a capacity failure disappear.
Match resource units, device geometry, and physical architecture to the selected
platform.

The indexed hierarchical frontend propagates child-module constant outputs
through parent aliases, including previously visited consumers. Values `0`,
`1`, `x`, and `z` retain their source meaning; conflicting constant bindings
are rejected. This also preserves synthesis-model control constants across
module boundaries. A frontend repair changes the design contract: regenerate
Phase 1, timing, partitioning, and Phase 6 rather than patching an old split.

## Providers and boundary semantics

CLI, Python phase APIs and experiment configurations default to timing-driven
PATRON v14 with a TritonPart initializer, `static-exact-combinational` boundary
semantics and baseline Phase 6. These defaults also apply to the native compile
bridge. Phase 4 exposes only `timing-aware-global-candidate-v1` at compile,
phase and experiment entry points. Its shortest-path and balanced trees remain
internal candidates. `--no-timing-driven` keeps this engine, disables timing
optimization, and preserves the complete timing population for analysis. Direct
Python Phase 4 calls may explicitly use `timing_driven=False` for a structural
qualification without STA input; those results do not claim timing coverage.
The old `compare-routing-tdm` and `compare-routing-tdm-scale` commands and
standalone router are removed. Select other supported phase options explicitly.
Use the bundled OpenROAD build: Phase 3 requires its `-resource_bounds_file`
interface to enforce independent resource limits inside TritonPart. Illegal
provider output fails validation instead of being repaired after partitioning.

```bash
emuflow multi-fpga compile --help
emuflow partition --help
emuflow multi-fpga physical --help
emuflow phase7c --help
```

`--partition-provider` selects the optimizer. `--cut-mode` separately selects
boundary semantics; `static-exact-combinational` requires the supported
single-virtual-clock contract. Selecting `sequential-only` does not select an
older PATRON version: v14 rejects that combination. Only v14 is supported;
`--patron-algorithm-version` may explicitly seal 14 but cannot select a retired
search. The `partition-pressure-reference` runner is removed. Source-bound native
assignment audits and small fixed model/objective fixtures remain; the retired
full Python model replay is not a production proof of timing values. Old
experiments use their matching Git revision. The
redundant `--patron-flow-refinement` flag, Python argument and experiment-config
key are retired; an explicit algorithm version determines its own refinement
behavior. Published reports retain the derived field for validation and must
record the algorithm version. See [Static Exact](STATIC_EXACT_COMBINATIONAL_CUT.md).

`--no-timing-driven` changes optimization, not the requirement to carry
original timing identities into a physical flow.

`--phase6-provider chimew` requires compatible placement/physical-region and
grouping inputs. Selecting it is not permission to invent package pins, routing
capacity, or electrical data. The baseline provider does not require a Chimew
physical-lookahead run.

## Physical implementation

Both physical backends use the complete native original-path query owner.
`physical prepare-local-paths` partitions the original TimingPathDB against the
compact source, assignment v4 and current route store; its shared scratch index
contains only path ordinals, local/cross scope and FPGA ownership.
`physical query-local-paths` joins those original records directly to the merged
physical source and publishes `query.tsv`, the current typed local identity and
a bounded report as one immutable generation. Callers consume the returned
query/identity references, without copying large payloads to old basenames.
The Python preparation, path-record spool and both query-producing loops are
retired. Primary-port paths use selected-net drivers for ownership, so unrelated
remote fanout cannot turn a local path into a crossing. Missing owners, unknown
selected nets, unresolved endpoints and unreported crossings are rejected.
Current local queries preserve endpoint-longest-path fallback qualification;
they do not claim an explicit routed pin chain. Measurement importers require
current typed identities and produce
current typed timing selectors directly through `emuflow-native`. Private
`vivado-local-identity.sqlite` JSON-row inputs, boundary-JSON import and the logic
importer's legacy-identity switch are retired; historical reruns use their matching Git revision. Native import
failure never selects a Python importer. These representation changes preserve
the selected physical backend and measurement qualification.

Mapped physical inputs are generated by `physical build-mapped-index` in the
shared core. Route certificate validation uses `physical validate-route` over
source-bound requests and reads the original sealed net/PIP streams; the public
Python adapter accepts artifact references rather than a complete route object.

Standalone RWRoute input export accepts only the current mapped SQLite index and
one typed physical handoff:

```bash
emuflow arch export-xilinx-rwroute-input --mapped-index MAPPED.sqlite \
  --physical-handoff PHYSICAL.sqlite --output INPUT.tsv
```

The OpenPARF bridge likewise requires one `physical_handoff` output argument and
a typed placement certificate. Its former pair of packing/placement JSON outputs
and `physical_handoff_path=None` mode are removed. Input aliases and source changes
reject publication; independent packing, placement and macro checks remain.

Separate packing/placement JSON export and Python mapping-object export are
retired. Historical JSON qualification uses the noninstalled test oracle; the
production entry never falls back after a native failure.

Append `--physical` to compilation when a complete physical run is required.
Backend-specific architecture, timing, and constraints must be supplied
consistently.

The CLI and Python API default to RapidWright with
`xilinx-ultrascaleplus-open-v1` mapping, including the standalone physical and
frontend-stage entry points. Only `rapidwright` and `vivado` physical backends
are supported. VTR/open, its standalone architecture/packing/routing commands,
VTR TimingDB translation and its CMake build targets are retired. Historical
VTR artifacts require the corresponding historical Git revision; current
commands reject the retired backend and options.
RapidWright always uses native OpenPARF placement; the old placer-selection
options and packed-cluster pack/guide/place commands are removed. Independent
`arch validate-xilinx-physical --mapped-index MAPPED.sqlite --physical-handoff HANDOFF.sqlite --architecture ARCH.json` audits the typed owner through the shared core. The former separate JSON packing and placement validators are retired.

- `--physical-backend rapidwright`: the default; Xilinx device facts and RWRoute
  with the native OpenPARF placer and `xilinx-ultrascaleplus-open-v1` mapping.
  Supply the compatible provider/device, native
  constraints, architecture/timing data, and runtime paths listed by
  `multi-fpga compile --help`. This path has external RapidWright API/device
  licensing conditions.
- `--physical-backend vivado`: optional proprietary device implementation.
  It requires the matching Vivado installation, device support, and licenses.

To consume an existing current Phase 6 output with Vivado, use the standalone
entry point and the matching original source/assignment/routing/timing inputs:

```bash
emuflow multi-fpga physical --split RUN/flow/split --platform PLATFORM.json \
  --schedule SCHEDULE.json --out RUN/vivado-physical --backend vivado \
  --vivado vivado \
  --original-ir ORIGINAL.emuir.sqlite --assignment ASSIGNMENT.json \
  --routes ROUTES.json --path-database PATHS.json
```

CMake and Python package installations include the Vivado Tcl adapters, native
primitive contracts, and fixed virtual-board wiring tables; the
indexed provider resolves the installed data directory when outside a source
checkout.

On hosts whose system C library is older than the versioned Yosys build,
`scripts/yosys/container_runtime.sh` is an optional `--yosys` launcher. Set its
`EMUFLOW_YOSYS_CONTAINER_*` variables to the executable, immutable image, binary,
bind root, and runtime library paths listed at the top of that script, plus the
active experiment's `TMPDIR`. This isolates the synthesis runtime from host
Vivado and keeps writable container work under that scratch directory.
For the same host ABI issue with OpenSTA, set `EMUFLOW_OPENSTA_CONTAINER_BINARY`
and pass `scripts/opensta/container_runtime.sh` to `--global-sta-executable`.
It shares the container configuration and selects OpenSTA independently of
Yosys, so both launchers can be used in one compile invocation.
Native install aliases resolve to their actual executable, so launchers find
their sibling runtime files. When the repository OpenSTA container launcher is
selected, timing evidence seals both its delegated engine and the launcher.

Supply the exact paths from the source run, including `--logic-path-database`
when required by its timing contract. `--fpga fpgaN` selects a representative
partition diagnostic and does not constitute a complete global run. The
indexed adapter uses complete timing queries; `--vivado-max-timing-paths` is a
retired sampled-export option, as is `--physical-vivado-max-timing-paths`.
The independent `--timing-backend`/`--timing-vivado` selectors are removed;
the upstream Phase 2 always uses OpenSTA, including with physical Vivado.
The old standalone Vivado `sta` map/import commands and provider policy are
also retired; post-route physical measurement uses the indexed Phase 7 owners.
Vivado export and implementation require current indexed EmuIR; a whole-graph
JSON input is rejected before tool execution. See the [provider contract](XILINX_PHYSICAL_FLOW.md#vivado-indexed-provider)
for current qualification and optimization limits.

The old `phase2` placement spike, generic `placement` commands and single-FPGA
`vpr fpga-open`/`full-open` flow are retired together with their whole-graph
Bookshelf exporter, greedy placement and generic placement schema.
The dependent `phase7d`/`phase8a` release/BSP-requirements chain is also retired;
its reports do not qualify either current physical backend. Current multi-FPGA
fixed-pin/electrical checks and terminal flow validation remain. Hardware-BSP
materialization and its serial-provider execution chain are retired. OpenSTA Phase 2 is unaffected. Use the multi-FPGA compile/physical
interface for the maintained implementations. Chimew reads indexed Xilinx
placement; the VTR placement/atom-order adapter is removed. Historical artifacts
require their matching Git revision.

Standalone `multi-fpga physical` uses `--backend` for the same selection and
also defaults to RapidWright. Integrated Chimew uses
`--phase6-provider chimew --physical` with the Xilinx mapping and either
`--physical-backend rapidwright` or `--physical-backend vivado`; both require
`--physical-architecture` with source-qualified native package-pin geometry.
There is no implicit `auto` selection.

Global physical STA supports only OpenSTA. `--global-sta-executable` overrides
the executable; the optional `--global-timing-engine opensta` records the same
engine explicitly. The retired Python engine is rejected before frontend work.
A missing engine or unsupported physical model is an error. Terminal validation
requires standalone OpenSTA evidence and checks saved measurements without
relaunching STA or reconstructing a different engine's numerical results.

Production `global-opensta/model.json` selects a generated model directory
containing `global_timing.lib`, `global_timing.v`, `global_timing.sdc` and
`analyze.tcl`. Use the manifest rather than assuming these files are directly
beside `opensta.log`. `global-opensta/measurements.json` selects the sealed
representative scalar SQLite owner used for explicit saved-result validation.
Original identities are restored by joining the reconstructed event namespace;
expanded original-check TSV is not a production handoff. The engine log remains
in `global-opensta`. Historical TSV production results require their matching Git
version. The event store is invocation-owned scratch and is not a replay checkpoint.
The internal commands are `emuflow-native global-sta index-events PRIMARY INDEX`
and `emuflow-native global-sta export-events PRIMARY INDEX REQUEST SUMMARY`;
the installed phase adapter creates the versioned request and binds sources.
Numerical stages use `global-sta import-measurements PRIMARY INDEX TSV REQUEST STORE`,
`global-sta audit-measurements PRIMARY INDEX STORE REQUEST SUMMARY`,
`global-sta publish-qor PRIMARY INDEX STORE REQUEST QOR` and
`global-sta validate-qor STORE REQUEST SUMMARY` through the same entry.
See the [event-stream contract](global-opensta-qualification.md#current-event-stream-contract).

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

## Phase 6 providers

The maintained providers are `baseline` and `chimew`. Baseline consumes the
concrete Phase 5 lane/slot assignment. Chimew adds source-bound grouping and
bank/channel assignment, followed by independent electrical, direction-bank,
physical-lane/slot and transport validation. Its current integrated lookahead
requirements remain those described under physical execution.

The standalone placement-aware planner, `pin-plan build`, the old contest
planner-versus-Chimew comparison, and `--pin-planner` are removed. Old planner
providers are rejected rather than reinterpreted as Chimew. Qualified Chimew
adapters remain available through `pin-plan chimew-run` and `chimew-build`;
`pin-plan validate` checks the current pin contract.
For the fixed HP-bank LVDS BoardDB, these adapters require v3 electrical maps
and publish v3 fixed-wire bindings resolving both P/N pins from the board owner. The Python helper
`build_fixed_wiring_chimew_electrical_map` derives pin identities from the
existing board inventory; the caller supplies only eligible link/lane/direction
selections and source-qualified lookahead geometry. Single-ended maps are
rejected for this BoardDB. The lookahead reads the selected backend's typed
physical owners and preserves the Phase 5 whole-bank direction allocation.
It never invents package pins or pairs opposite directions on one fixed bank.

The ArchitectureDB package catalog must include `native_tile` on each used
`physical_regions.io_banks[].pins[]` record: the coordinate system
`native-device-tile-column-row-v1` and non-negative integer `column`/`row`.
Regenerate a geometry-free region sidecar using
`scripts/rapidwright/export_physical_regions.py` and merge it through the
normal physical-region interface. Missing native geometry is rejected before
frontend synthesis; site names and lane numbers are not coordinate substitutes.
Baseline execution still accepts existing geometry-free sidecars.

Lookahead region encodings remain a labelled heuristic, not physical SLR/SLL
closure. A Phase 6 QoR claim still requires complete baseline/candidate Phase 7
and original-path global OpenSTA acceptance with identical backend settings.
The current A/B comparison uses schema v3 and checks matching backend, device
and execution settings. Its common physical diagnostics use measured timing
and closure fields; obsolete VPR-only wirelength is not reported as zero or
substituted with an unrelated vendor metric.
See the [fixed wiring contract](HARDWARE_PLATFORM.md#fixed-virtual-board-wiring).

Chimew's grouping, position-refinement, RUDY and bank/channel kernels are always
linked into `libemuflow_core` and run through the unified `emuflow-native phase6`
commands. Static Exact analysis uses `emuflow-native phase3 analyze`. Their five
duplicate standalone executables are no longer built or deployed. Remove the
retired `EMUFLOW_BUILD_CHIMEW` and `EMUFLOW_BUILD_STATIC_EXACT_ANALYZER` cache
options; explicitly enabling them fails. The older `EMUFLOW_BUILD_PIN_PLANNER`
cache entry must also be removed. These changes do not remove the Chimew provider.
Grouping, refinement and bank assignment reject `--legacy-text`; use the typed
input protocol. RUDY retains its current single input stream protocol. Chimew
file results publish atomically after input generation checks. Stdout results remain
streamed and are accepted only after their record grammar and process success
are validated. The logical pin-plan schema retains its v1 identifier for current
Chimew records; this does not retain the old planner.

Canonical provider comparisons now require baseline and Chimew under identical
upstream, backend, seed and clock contracts. Both arms must reach complete
Phase 7/7C and full original-path timing coverage before a QoR claim. Removing
an abandoned arm does not qualify either retained arm or a backend.

## Output interpretation

Current indexed Static Exact Phase 5 runs publish a typed `schedule.*.sqlite`
store, a compact `schedule.json` selector, cross-layer timing and the final
report. Independent indexed validation completes before the report is committed.
The producer executes model construction, scheduling, readiness certificates
and typed publication in one native call. Python retains only its bounded
request, then consumes the staged store; numeric intermediate input/result
files and Python result import are absent. The report records the producer
implementation separately from its unchanged algorithm provider and labels
the storage timer's scope, which includes calculation on this path.
This compact production behavior does not require a managed-storage flag.
Selection follows the current v3 indexed route/Static Exact contract at every
route count and requires the unified `emuflow-native` scheduler; a missing unified
entry does not silently select the reference producer. The old numeric
model/result scheduling and separate publication commands are retired, along
with the standalone Exact scheduler and its build option. Use the complete
`emuflow-native phase5 run` stage. The stage owns schedule construction,
independent validation, timing projection and report-last publication; the
former Python ScheduleStore publication lifecycle has been removed.
Production Phase 5 accepts only `deterministic-dependency-earliest-slot-v1`
and requires a current typed RouteStore plus its Phase 3 Static Exact binding.
The Python schedule oracle is test-only and bounded to 4096 routes.

The current `cross_layer_timing.json` uses v3: `path_binding` records a source-bound
projection hash and counts, while path/member/cut facts remain in RouteStore. It
has no duplicate `path_bindings` population. The native timing projector and its
preflight schema are required by the current Phase 5 implementation; use the
matching Git version to interpret old inline generations. See the
[indexed timing contract](FLOW_PLAN.md#phase-5-indexed-timing).

ScheduleStore supports only `emuflow.tdm-schedule-storage/v4`; storage v1--v3
readers, writers and native registry aliases are retired. Historical stores
require their matching Git revision. The logical `emuflow.tdm-schedule/v1`
schema and the typed child relation v1 schemas are still current. The installed
writer accepts only a native Exact generation request, never a Python schedule
dictionary. Tests may publish bounded current-v4 snapshots through a noninstalled
fixture helper; that helper is not a production fallback. Explicit logical JSON
inputs remain available for qualification and do not restore a legacy scheduler.
Current-v4 stores without the optional route-net lookup index remain readable:
the deadline reader builds a temporary SQLite lookup from typed route columns,
without reconstructing Python route or hop records. New producers write that
index directly. Independent completion validation compares all typed scalar and
sink-arrival rows without duplicating each completion as JSON.

The independent ratio/KKT, timing-DAG and slot optimizers, their tuning flags,
`ratio_plan.json`, and the cross-stage/partition-feedback commands are retired.
Phase 4 also removes `--tdm-feedback*` and `--physical-feedback*`, their Python API
arguments and external native price records. Use the single upstream timing and
routing contract; historical feedback studies require their original Git revision.
The sequential-versus-Static-Exact comparison compiler is also removed because
its registered-boundary arm no longer has a production scheduler. Full compile
requires `static-exact-combinational`. Minimum-frame search uses the same
scheduler and emits `emuflow.frame-search/v5`. Platform-owned ratio quantization,
bank direction and physical delay constraints remain authoritative.

Native production does not emit `schedule.tsv`, `transport_manifest.json`,
testbench or feedback JSON, or run reference signal simulation implicitly.
These remain explicit qualification/export operations when needed. Compact
production outputs are independent of a managed-storage flag. Existing physical
backends and optional Phase 6 Chimew have not been removed by this change.

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

### PATRON source-owned input

PATRON runs through `emuflow-native phase3 refine-patron-from-primary EMUIR TIMING PRIMARY INITIAL REQUEST OUTPUT`.
The compact source, indexed timing and primary cluster ownership are consumed
directly. The complete-model `refine-patron` and `refine-patron-from-store`
commands, text/pipe model protocols and `EMUFLOW_PATRON_INPUT_COPY` diagnostic
route are retired. Model-build and search summaries remain bounded; current
output ownership is typed and source-bound.

### Retired partition providers

Greedy, RePart, RePart replication and MFSPart, including MFSPart post-refinement,
are removed from the CLI, Python phase APIs and root build. Their tool paths,
post-refinement options and dedicated qualification compiler are unsupported.
Use the matching historical Git version to interpret or reproduce those routes.
TritonPart remains the PATRON initializer and a direct comparison capability;
PATRON's internal communication costs and hop validation remain in use.
Phase 6 baseline remains the default, and explicit Chimew support is retained.

The greedy initializer oracle lives only under `tests/` and is bounded to small
contract fixtures. It cannot initialize a production or NVDLA acceptance run.
Explicit `--tritonpart-solution` inputs use the shared imported-solution validator
and do not require OpenROAD when it will not execute. A single active FPGA uses
direct packed ownership and the common legality/resource validators; it does not
construct a greedy adjacency graph.
