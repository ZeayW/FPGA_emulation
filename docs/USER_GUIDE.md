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

## Paired Phase 4 validation

`multi-fpga compile --stop-after phase3` stops after partitioning and timing
projection; `--stop-after phase5` includes routing and schedule validation.
Both publish `multi-fpga-stage-report.json` with `status: partial` and an explicit
validated boundary. They do not imply Phase 6/7 completion. Add
`--sta-require-complete` to reject an exhausted or unknown STA export limit
before partitioning. For fresh Xilinx RTL synthesis, `--synthesis-profile-passes`
streams pass progress to `frontend/yosys.log` without changing mapping options.
Physical execution and full-flow archival cannot be
combined with these intermediate boundaries.

For a paired full NV_nvdla routing study, prepare the pinned design using
`scripts/benchmarks/prepare_nvdla_holdout.py` with the explicit
`physically-implementable-shared-memory-model-v2` policy. SRAM preparation
preserves wrapper enable, retention and sleep controls, including opposite-edge
write capture where required by the original macro. Functional differential
checks and actual BRAM inference are separate preparation gates. The portable
`scripts/phase4_nvdla_prefix.py` command captures provenance and produces one
PATRON v14 Static Exact prefix on the four-XCVU19P mesh, preserving both 10 ns
clocks and the fixed 1024-slot frame. An explicit `--frame-slots` overrides the
route-constraints file for both Phase 3 partition pressure and Phase 4 routing;
other route constraints are preserved. The paired seal checks the retained
PATRON native input header as well as the requested configuration. Inspect its
`--help` for tool/input paths. After an audited Phase 3 frame-propagation repair,
`--reuse-prefix <stopped-prefix> --reuse-producer-commit <original-full-sha>`
can bind its completed Phase 1 and original STA publications read-only. The
runner checks unchanged RTL, tools, timing constraints and producer code,
recovers completeness from the original uncapped OpenSTA export log, and seals
the reused payloads once. It records the original producer separately from the
new prefix producer; missing evidence or upstream computation changes reject
reuse. This continuation rebuilds Phase 3 and does not reuse an assignment.
Before sealing a completed prefix, replay its independent qualification:

```bash
python scripts/phase4_nvdla_qualification.py \
  --common-run COMMON --provenance PROVENANCE \
  --architecture-timing-db ARCHITECTURE_TIMING_DB --output QUALIFICATION.json
python scripts/phase4_nvdla_compare.py \
  --common-run COMMON --platform PLATFORM --provenance PROVENANCE \
  --qualification QUALIFICATION.json --seal-contract CONTRACT.json
```

Use new output paths and the architecture timing database sealed by the producer.
The replay checks every original representative's local/crossing membership,
output copies, clock, required time and ownership, then independently rebuilds
the full-cut structural contract. `uncovered_cut_nets` remains reported: it
counts cuts absent from the projected representative population, not omitted
original members. This is not an assertion that STA enumerated every possible
path; transport can change which path is worst at an endpoint. Both arms still
require complete routing and schedule validation. Raw and Phase1-normalized
forms of the repository platform are accepted only when their parsed hardware
contracts agree; both file identities are sealed.

Replay leaves producer reports unchanged and seals its evaluator identity
separately. The historical source exception permits only the comparison gate
script to change; changes to computation sources or the prefix runner reject
reuse. Qualification does not turn an explicitly budgeted, non-converged
partition into a formal complete-search result.

`scripts/phase4_nvdla_compare.py --contract ... --common-run ... --platform ...
--output ...` runs the explicit old and author routers against this same prefix
and identical Phase 5 settings; its `--help` lists the required native tools.
Reports distinguish projected inter-FPGA Phase 5 timing from whole-design
physical timing. Phase 5 includes per-clock WNS/TNS and path counts, preserving
original clock identities and counting each original path after output-copy
aggregation. New compressed inputs retain each member’s logic delay; reports
mark these observations `original-member-timing/v1`. Older inputs without that
information remain explicitly `legacy-diagnostic-only`, with
`original_member_tns_available: false`. Candidate selection retains its existing
compressed scoring policy, separate from the corrected final timing report.
This report covers projected paths and registered output copies, not all local
paths. For whole-original-population estimates, use
`scripts/phase4_nvdla_timing.py --producer-root ... --contract ...
--comparison-root ... --output ...`. It restores each original path’s own logic
delay, checks population coverage, and counts the worst output copy once.
Its population is the fixed original STA representatives, including local paths;
it is not all-path STA. The evaluator accepts ordinary and managed route/schedule artifacts while
sealing the original stored files. These estimates do not establish physical
timing closure. Successful preparation alone is not qualification evidence.

The contest comparison tool separately reuses a baseline only when its Python
producer/evaluator, runtime, native tools, settings, inputs and sealed outputs
match. Legacy summaries without these seals require a fresh control run;
reusable imported inputs do not establish reusable metrics.

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

### Completed native partition results

`phase4_nvdla_native_result.py` seals a successful native PATRON result and its
original input, tool and ordered cluster mapping. Pass the resulting manifest
and recovered clusters to `phase4_nvdla_prefix.py --completed-native-result
<manifest> --recovered-clusters <clusters>` with the original prefix options.
The consumer requires byte-identical regenerated native input and retains
separate native and continuation producer identities. It still executes normal
Phase3 validation, Static Exact construction and original timing-path projection.
Stopped or partial native results cannot be continued; native completion alone
is not a Phase3 pass. Use each script’s `--help` for required sealing inputs.

### Explicit PATRON search budget

The native PATRON v14 option `--sweep-prefix-clusters N` selects a deterministic
research variant: visit at most N clusters in critical order, finish each
candidate transaction, then reconstruct and validate the full final assignment.
It does not execute subsequent ejection, comove or flow refinement. This limits
search work, not design or timing-path coverage. Output records the budget and
termination reason and makes no convergence claim. Omitting the option retains
the full search. Seal the exact command alongside input and output when using
a budgeted result; all normal Phase3 and downstream validation still applies.
