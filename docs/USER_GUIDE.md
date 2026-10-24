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

## Native compile control

Add `--compile-implementation native-control-v1` to the normal
`emuflow multi-fpga compile` invocation to select the C++ one-shot controller.
The CLI still resolves benchmark/BoardDB inputs, then sends bounded configuration
to `emuflow-native compile REQUEST_JSON`. The controller chooses the phase order;
it never calls the Python compile function. Individual shared phase bodies run
in isolated Python adapters while their remaining data planes are migrated.
The default remains `python-control-v1`, with the same provider, algorithm,
seed and physical-policy defaults in both modes.

The native controller supports the ordinary baseline split path, optional physical
implementation and global timing. Frame-length search,
Chimew comparison and serial-BSP continuation currently reject before synthesis;
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
Configured route-hop limits also require `emuflow_hop_partition_refiner`;
versioned runtime deployment builds and probes it instead of relying on an
older base installation to contain that helper.

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

Select another BoardDB with `--platform`. The alternative VTR flow must be
requested consistently rather than mixed with the Xilinx default:

```bash
emuflow arch fetch-default-vtr \
  --output build/architectures/vtr-flagship.xml

emuflow multi-fpga compile examples/rtl/counter.v \
  --top counter --clock clk --clock-period clk=10 \
  --mapping-profile vtr-hard-blocks \
  --physical-backend open \
  --platform platforms/virtual/academic_vtr_2fpga_p2p.json \
  --out build/counter-vtr-flow
```

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
older PATRON version: v14 rejects that combination. Explicit reference reruns
must name both their algorithm version and noncurrent boundary policy. The
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

Append `--physical` to compilation when a complete physical run is required.
Backend-specific architecture, timing, and constraints must be supplied
consistently.

The CLI and Python API default to RapidWright with
`xilinx-ultrascaleplus-open-v1` mapping, including the standalone physical and
frontend-stage entry points. Backend selection never silently falls back to VTR.

- `--physical-backend rapidwright`: the default; Xilinx device facts and RWRoute
  with the native OpenPARF placer and `xilinx-ultrascaleplus-open-v1` mapping.
  Supply the compatible provider/device, native
  constraints, architecture/timing data, and runtime paths listed by
  `multi-fpga compile --help`. This path has external RapidWright API/device
  licensing conditions; it is not the fully open VTR route.
- `--physical-backend open`: alternative VTR/VPR packing and routing with
  OpenPARF placement and `--mapping-profile vtr-hard-blocks`. Fetch the public
  architecture using `arch fetch-default-vtr`, bind it with
  `--physical-architecture`, and configure the compiled OpenPARF
  installation/interpreter using `--physical-openparf-install` and
  `--physical-openparf-python`.
- `--physical-backend vivado`: optional proprietary device implementation.
  It requires the matching Vivado installation, device support, and licenses.

Standalone `multi-fpga physical` uses `--backend` for the same selection and
also defaults to RapidWright. The integrated Chimew lookahead path currently
requires explicit `--physical-backend open --mapping-profile vtr-hard-blocks`.

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

Build Chimew's grouping, position-refinement, RUDY and bank/channel kernels
with `EMUFLOW_BUILD_CHIMEW=ON` (the default). The old
`EMUFLOW_BUILD_PIN_PLANNER` cache entry must be removed; configuring with that
retired option fails explicitly. The logical pin-plan schema retains its v1
identifier for current Chimew records; this does not retain the old planner.

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
`emuflow-native phase5 build-schedule` operation. Production Phase 5 accepts only `deterministic-dependency-earliest-slot-v1`
and requires a current typed RouteStore plus its Phase 3 Static Exact binding.
The Python schedule oracle is test-only and bounded to 4096 routes. Explicit
legacy artifact legality checks do not restore a legacy production scheduler.

The independent ratio/KKT, timing-DAG and slot optimizers, their tuning flags,
`ratio_plan.json`, and the cross-stage/partition-feedback commands are retired.
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
