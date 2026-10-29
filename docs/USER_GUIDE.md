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

## Installed runtime data

The root CMake `emuflow_python` target installs a complete current Python
package, replacing its prior module inventory so deleted adapters do not remain
importable. It bundles the default Xilinx primitive library together with
BoardDB, I/O inventory and SQL schemas; resource changes invalidate the install
stamp. The `emuflow_installed_python_runtime` CTest imports only the installed
package and verifies its module/data inventory and default profile resources.
It requires the Python install target to have been built, as in the normal
root build. This check does not execute synthesis or physical implementation.

## Native route sealing

Production Phase 4 requires an updated `emuflow-native` with the
`system-route-seal-request-v1` protocol. The retained-row routing API checks
that capability before constructing its model. The internal
`phase4 seal-routes STORE REQUEST SUMMARY` command reads current RouteStore v3
rows and returns a bounded `emuflow.system-route-seal/v1` summary; it does not
export routes or replace the canonical store. Its request references the five
relation families rather than embedding their records. Producers use the shared
checked request writer; a stale native installation, corrupt source, mismatched
header/count, or changed input fails instead of invoking another publisher.

The public route selector, v3 typed schema, route ordering, logical SHA and
independent legality checks retain their meanings. Prior store generations
remain recoverable until the normal selector and typed-store validation pass.
The Python mapping writer is an explicit reference/import interface, not a
fallback for failed native sealing.

## Native compile control

Add `--compile-implementation native-control-v1` to the normal
`emuflow multi-fpga compile` invocation to select the C++ one-shot controller.
The CLI still resolves benchmark/BoardDB inputs, then sends bounded configuration
to `emuflow-native compile REQUEST_JSON`. The controller chooses the phase order;
it never calls the Python compile function. Individual shared phase bodies run
in isolated Python adapters while their remaining data planes are migrated.
The default remains `python-control-v1`, with the same provider, algorithm,
seed and physical-policy defaults in both modes.

The native controller supports baseline splitting and retained Chimew comparison,
with either OpenPARF/RWRoute or Vivado physical implementation and global timing.
Chimew retains its baseline physical/global timing prepass, source-qualified
package geometry, fixed wiring, typed pipeline and independent final comparison.
The device pin catalog crosses process boundaries as bounded, source-generation
bound control data; no placement or endpoint population is embedded in it.
Baseline reports have one owner within the comparison and are restored as
references within each phase, rather than serialized twice. This control state
is private to the live invocation and cannot be used as an old-artifact reader.
Frame-length search and serial-BSP continuation still reject before synthesis;
they remain available through the Python controller. No rejected or failed native
run retries through Python or changes providers. This entry does not establish
large-design qualification or completion of the C++ data-plane migration.

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
Each adapter receives its own `TMPDIR`, `TMP`, `TEMP` and `XDG_CACHE_HOME` below
the controller's scratch directory. After adapter execution, group cleanup and
state validation, the controller removes that phase's temporary workspace before
starting the next phase. Absolute paths in control state that resolve into the
workspace, including output symlinks to temporary files, reject the handoff.
Publish required downstream artifacts in the flow directory; a temporary engine
cache is not a phase publication.

`multi-fpga-flow-report.json` remains the usual flow result. The separate
[native-compile-result.json](../schemas/native-compile-result-v1.schema.json)
retains requested and resolved configuration, common operation status and stage
wall/CPU/RSS metrics. Its `native_data_plane_complete` is explicitly false.
`wait4` maximum-process high-water RSS is distinct from sampled process-tree RSS
in `stage-metrics/`; neither should be relabelled as the other. Retain the compact
controller result alongside normal terminal validation evidence, and apply the
same experiment cleanup and full Phase 7/7C acceptance requirements.

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
scopes as clustering below. Large compact native semantic analysis uses this
producer too; explicit reference and full characterization APIs stay available.

Normal compact Phase 3 runs use `phase3 build-clusters EMUIR REQUEST OUTPUT`
inside the shared core. Packed eligibility bits and integer group membership
bind to the EmuIR generation. The native producer performs connectivity union,
lexical cell/cluster ordering, CSR membership and resource reduction. Downstream
Python providers borrow sealed read-only columns; the mapping and its temporary
file live until the last borrower releases them. Final v1 cluster records,
group/fixed constraints and resource totals retain their reference semantics.
`cluster_implementation_id` reports `native-primary-clusters-v1`;
`native_cluster_metrics` retains common native wall/CPU/I/O measurements and the
process-lifetime RSS high-water snapshot. It is separate from sampled whole-phase
process-tree RSS. The ordinary
Python `build_clusters` API keeps its reference default for comparison. Retained
diagnostics and explicit PATRON comparison executables also keep that producer; a selected
production native failure stops the phase. Remaining policy/semantic publication
work is not claimed as native here.

Compact indexed PATRON v14 runs normally select the shared native pressure-model
producer. `algorithm_validation.model_implementation_id` identifies
`native-primary-store-model-v2` when consuming native primary columns directly.
Its input-transport counters describe packed initial/fixed ownership and the
bounded request; the primary membership/resource columns are referenced. The
private v2 primary store carries its EmuIR generation and resource totals, so
TritonPart summary queries do not scan the columns. The former snapshot command
and its input protocols are retired; missing primary ownership is an error. The native entry
`phase3 refine-patron-from-primary EMUIR TIMING PRIMARY INITIAL REQUEST OUTPUT`
also writes `OUTPUT.order` (sealed integer identity mapping) and
`OUTPUT.summary.json` (bounded model counts). These are invocation-owned intermediates.
Diagnostic/explicit comparison modes retain their named reference input path;
a native failure never switches implementations. This does not establish complete
native Phase 3 ownership or real-design physical/timing qualification.
Configured route-hop limits use `emuflow-native phase3 refine-hops INPUT OUTPUT`.
The unified operation publishes assignments and metric counters atomically and
accepts `--trace-output` for diagnostic move records. The Phase 3 API follows
`retain_diagnostics`; its normal report retains the total move count without
materializing a Python move list. There is no standalone helper or installation
fallback. Versioned runtimes build this kernel into `libemuflow_core`.

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
search. The `partition-pressure-reference` runner is removed; current independent
model/objective and assignment validation remain. Old experiments use their
matching Git revision. The
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
`arch validate-xilinx-packing` and `arch validate-xilinx-placement` remain.

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
The configured bind must expose the inputs, output directory, and `TMPDIR`.
The launcher uses standard runtime filesystem mounts to retain caller scratch
visibility, and checks that scratch is visible and writable inside the
container before starting the engine.
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
An explicit `--vivado-dut-frequency-mhz 10` sets a 10 MHz fastest-root
implementation target for standalone physical execution. The full compile
option is `--physical-vivado-dut-frequency-mhz 10`; original root period ratios
are preserved. See the [clock baseline](XILINX_PHYSICAL_FLOW.md#local-implementation-clock-baseline)
for source-target provenance and global timing scope.
The independent `--timing-backend`/`--timing-vivado` selectors are removed;
the upstream Phase 2 always uses OpenSTA, including with physical Vivado.
The old standalone Vivado `sta` map/import commands and provider policy are
also retired; post-route physical measurement uses the indexed Phase 7 owners.
Vivado export and implementation require current indexed EmuIR; a whole-graph
JSON input is rejected before tool execution. See the [provider contract](XILINX_PHYSICAL_FLOW.md#vivado-indexed-provider)
for current qualification and optimization limits.
The indexed provider also supports checked active-run synthesized continuation
and exports routed timing in one native session without mandatory routed checkpoints.

After independently finishing physical implementation, finalize its report with
`emuflow multi-fpga finalize-physical --flow <flow> --physical <physical-directory>`.
This reuses the checked Phase 1--6 artifacts and writes Phase 7C under
`runtime-final` by default. Use `--global-sta-executable` for a specific OpenSTA
installation, or `--global-sta-results-dir` to recheck matching saved measurements.
Saved-result replay retains the verified measurement file and engine log in
the new runtime directory. Bundle validation follows the sealed runtime and
physical artifact paths, including independently resumed directories.
A completed native analysis publishes its actual `pass`, `fail`, or `incomplete`
outcome; finalization returns exit code 2 for either non-passing outcome.
To continue after completed mapping, supply `--prepared-frontend` without
`--prepared-timing`; Phase 2 runs again using the selected timing inputs while
the checked Phase 1 database remains read-only. Supplying both boundaries also
reuses Phase 2. Fresh Phase 2 extraction requires the device FF control supplement
in its pre-placement TimingDB; missing control data is rejected before synthesis.
The NVDLA benchmark declares core-referenced arrivals for `dla_reset_rstn`,
`direct_reset_`, and `test_mode`, as well as its data and clock-enable inputs.
This keeps reset synchronizer data and recovery checks in the source search;
it does not tie off these inputs or establish asynchronous reset-release or
test-mode signoff. Source and physical execution use the same declared I/O model.

The old `phase2` placement spike, generic `placement` commands and single-FPGA
`vpr fpga-open`/`full-open` flow are retired together with their whole-graph
Bookshelf exporter, greedy placement and generic placement schema.
The dependent `phase7d`/`phase8a` release/BSP-requirements chain is also retired;
its reports do not qualify either current physical backend. Current multi-FPGA
BSP materialization, fixed-pin/electrical checks and terminal flow validation
remain. OpenSTA Phase 2 is unaffected. Use the multi-FPGA compile/physical
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

### Joining Vivado workers

For the indexed provider in the same active experiment,
`multi-fpga physical --backend vivado --fpga ID`
emits a `physical-partition-batch.json` and per-FPGA records. Repeat `--fpga`
to implement several partitions in one worker. After every BoardDB FPGA has
completed, join these batches without invoking synthesis, placement or routing:

```bash
PYTHONPATH=src python -m emuflow multi-fpga assemble-vivado-physical \
  --split RUN/split --platform PLATFORM.json --schedule RUN/tdm/schedule.json \
  --batch RUN/worker-a/physical-partition-batch.json \
  --batch RUN/worker-b/physical-partition-batch.json --out RUN/physical-joined
```

Supply the actual paths used by the workers and an empty output directory.
The join checks split/platform/schedule seals, record hashes, complete FPGA
coverage, matching seed and implementation directives, and each active
partition's runtime/resource-budget and exact Phase 6 fixed-I/O bindings.
The common physical validator
then checks resources, fixed differential I/O and complete boundary, local-path
and cross-FPGA logic timing. Missing, duplicate, failed or mixed-source records
are errors. Batches predating recorded implementation directives must be
rejected rather than assigned inferred defaults. The resulting physical
summary still requires whole-design Phase 7C analysis; joining workers does
not produce global WNS/TNS. These records are temporary active-run inputs,
not a persistent checkpoint library.

Word-mode Phase 7B workers use the Phase 6 `word_phy_plan.json` and
`virtual_board_wiring.json` directly. Their preflight and assembler validate
the plan's schedule/transport identities and native-bank physical evidence.
They may complete while Phase 7C integration is pending. Routed checkpoints
remain temporary inputs for that active timing investigation; a completed
Phase 7B batch does not certify global timing or an operating frequency.

Completed native word-mode analysis publishes a version 3
`multi-fpga-flow-report.json` even when its timing or coverage outcome is
`incomplete` or `fail`. `execution_status: pass` records successful execution;
`status` preserves the Phase 7C outcome, and the compile command still exits
with code 2 for a non-passing outcome. The summary includes the implementation
clocks, maximum sampling cost, required DUT period and inferred frequency.
Sampling cost includes the declared timing adjustments and is not relabeled
as raw datapath delay. Missing measurements stay null. The report binds the
native analysis, original-member indexes and independent STA evidence so a
terminal outcome does not require another physical implementation run.

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

For OpenSTA results, `runtime/qor_report.json` is a compact selector containing
the summary under `header.timing` and naming one sibling SQLite path store.
Keep both files when moving results. Python consumers use
`emuflow.io.read_json` to preserve the logical `timing` interface; `timing.paths`
is a lazy sequence with ordinal and `by_path(original_id)` access. A summary
read does not load the path store. See [indexed result ownership](global-opensta-qualification.md#indexed-result-ownership)
for explicit whole-population validation and all-eliminated results.

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
`emuflow-native phase5 build-schedule` operation. Production Phase 5 accepts only `deterministic-dependency-earliest-slot-v1`
and requires a current typed RouteStore plus its Phase 3 Static Exact binding.
The Python schedule oracle is test-only and bounded to 4096 routes.

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

Store run directories, temporary tool products, logs, and experiment records
outside the public source tree. Consult CLI help
for specialized diagnostics; do not treat an old run directory or a partial
stage as a newly validated full flow.

### PATRON input diagnostics

PATRON runs through `emuflow-native phase3 refine-patron --typed-input INPUT OUTPUT`
or the direct primary-store operation. The separate PATRON executable and text
input format are retired. `EMUFLOW_PATRON_INPUT_COPY` explicitly requests an
atomically published CRC-checked binary input copy for diagnosis; it is not an
alternative text protocol. Detailed output traces require `--trace-output`.

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
