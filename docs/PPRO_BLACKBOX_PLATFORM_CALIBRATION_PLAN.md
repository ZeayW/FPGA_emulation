# PPro black-box platform calibration plan

## Objective

Build an open, behavior-equivalent academic multi-FPGA platform model from two
lawful inputs:

1. public S2C and AMD specifications, which define the platform family,
   devices, published resource budgets, supported scale, connector classes,
   and physical upper bounds; and
2. repeatable observations from normal PPro runs over controlled RTL and
   documented user constraints.

PPro is treated as an executable black box.  The calibration path must not
read, copy, parse, or publish PPro's internal BoardDB, private STF/board
implementation database, pin map, timing tables, encrypted data, or
undocumented configuration.  It may use the same documented project setup,
legal FPGA identifiers, user constraints, commands, and ordinary reports that
an authorized user receives when running the tool.

The deliverable is an **EmuFlow calibrated academic platform**, not an S2C
hardware clone.  It should reproduce the capacity, routing, TDM, timing, and
algorithm-ranking behavior that PPro exposes closely enough for open CAD
research.  It does not claim schematic equivalence, physical pin equivalence,
hardware BER, or measured board timing.

## Non-goals

- Reverse engineering PPro algorithms or proprietary file formats.
- Reconstructing package-pin, GTY channel/quad, reference-clock, reset, or PCB
  wiring details that are not public observations.
- Treating a configured maximum line rate as payload bandwidth.
- Claiming cycle-accurate hardware behavior without physical measurements.
- Fitting against a single application and calling the result general.
- Using contest communication graphs or replicated-core harnesses as evidence
  for application-level calibration.

## Evidence and provenance classes

Every emitted parameter must have exactly one provenance class:

| Class | Meaning | Publication policy |
| --- | --- | --- |
| `public_spec` | Directly stated in a public S2C or AMD document | Publish the value and citation |
| `black_box_observation` | Appears in an ordinary authorized PPro result | Store a compact normalized observation; keep raw vendor reports outside the repository |
| `black_box_fitted` | Estimated from multiple controlled observations | Publish estimate, confidence interval, fit set, and holdout error |
| `research_assumption` | Required by EmuFlow but not identifiable | Publish the assumption and sensitivity range |
| `not_identifiable` | Cannot be separated from available observations | Do not invent a value or silently substitute a guess |

The open model must be rebuildable from public specifications plus normalized
observations.  Raw PPro projects, reports, licenses, installations, credentials,
and server paths never enter the repository.

## Resulting platform contracts

### Calibrated BoardDB

The model should describe:

- supported complete platform configurations, initially the public 2/4/6/8
  XCVU19P family rather than arbitrary FPGA subsets;
- per-FPGA effective LUT, FF, BRAM, URAM, DSP, I/O, and transceiver budgets;
- effective directed FPGA-pair reachability;
- inferred hop relationships and shared-capacity groups;
- per-direction payload-capacity intervals;
- allowed communication classes and maximum supported TDM ratio; and
- explicit headroom for DUT, transport, clocking, and routability.

### Calibrated BoardLinkTimingDB

For every identifiable link class, fit a bounded model such as:

\[
D = D_{endpoint} + H D_{hop}
  + \left\lceil W/B \right\rceil D_{serialization}
  + D_{tdm}(r) + D_{contention}(q)
\]

where `H` is effective hop count, `W` is transferred width, `B` is effective
payload width, `r` is the TDM ratio, and `q` summarizes competing traffic.
The fitted terms are aggregate behavior.  They must not be relabelled as
specific SerDes, CDC, cable-propagation, or elastic-buffer delays unless PPro
exposes an independently identifiable observation.

### TransportCostDB

Fit incremental LUT/FF/BRAM costs for endpoint count, lane count, mux ratio,
buffering class, direction, and multicast fanout when PPro reports sufficient
resource data.  If the tool reports only whole-design utilization, estimate
these costs through paired difference experiments and retain uncertainty.

Each database is published in `nominal`, `conservative`, and `aggressive`
profiles.  The nominal profile is the fitted median or constrained optimum;
the other profiles are derived from confidence bounds, not hand-picked QoR.

PPro's ordinary pre-partition resource report was experimentally shown not to
observe the inserted proprietary transport shell.  Transport cost therefore
uses a deliberately separate, source-backed evidence path: generate the exact
production EmuFlow transport and runtime-controller RTL, map it with the
audited open Xilinx UltraScale+ Route-A profile, and fit only the resulting
primitive resource totals.  The model follows the generated hardware
structure: fixed shell, physical TX output lanes, RX shadow bits, RX
arrival-slot decode groups, deep TX mux lanes, and categorical frame-slot
terms.  Multicast and TDM affect cost through the physical lanes and mux depth
they actually create instead of an unrelated logical-net count.  Fit and
holdout cases are disjoint.  BRAM/DSP/URAM may be
declared structural zero only when every mapped primitive audit independently
contains no such hard block; absence from a PPro report is never sufficient.
This hybrid provenance is intentional: PPro black-box observations calibrate
capacity/topology/payload/TDM/latency, while the open shell that EmuFlow itself
implements calibrates its own resource cost.

## Calibration architecture

```text
public specifications
        |
        v
public platform prior --------------+
                                     |
controlled RTL + documented          v
user constraints -> PPro black box -> normalized observations
                                     |
                                     v
                         constrained model fitting
                                     |
                 +-------------------+-------------------+
                 v                   v                   v
              BoardDB       BoardLinkTimingDB      TransportCostDB
                 \                   |                   /
                  +------------------+------------------+
                                     v
                        blind application validation
                                     |
                                     v
                          EmuFlow Phase 1--7 validation
```

The external adapter accepts only explicitly named normal PPro reports and
emits a compact, vendor-neutral observation.  It must fail closed when an
expected report or metric is absent; it must not search the installation tree
for hidden alternatives.

## Experiment design

### C0: reproducibility and observation contract

- Pin the PPro release, target public platform family, RTL generator revision,
  documented command sequence, and random seed when exposed.
- Run one tiny case repeatedly to measure deterministic fields and run noise.
- Define the normalized observation schema and a redaction gate.
- Distinguish tool/infrastructure/license failures from evaluated experiments.
- Validate the harness with mock reports before any calibration campaign.

### C1: effective resource capacity

Generate naturally connected, parameterized designs dominated separately by:

- LUT;
- FF;
- BRAM;
- URAM;
- DSP; and
- mixed LUT+FF, BRAM+DSP, and control/datapath resources.

Increase one resource axis until the result changes from one FPGA to more
FPGAs or becomes infeasible.  Use interval search near the boundary.  These
experiments fit effective capacity and headroom; public device totals remain
the hard physical upper bounds.

### C2: effective topology and reachability

- Place one producer and one consumer on every legal ordered FPGA pair using
  documented user constraints.
- Record success, selected route, reported path identity, hop evidence, and
  timing.
- Repeat with reverse direction to expose asymmetric behavior.
- Add disjoint and overlapping pair tests to identify shared bottlenecks.
- Infer only the minimum effective graph needed to explain observations;
  non-identifiable physical connectors remain unspecified.

### C3: payload capacity and TDM

For each identifiable link class, sweep:

- payload width;
- unidirectional versus bidirectional traffic;
- one flow versus multiple parallel flows;
- fanout and multicast shape;
- TDM-forced and non-TDM user settings when documented; and
- competing flows on inferred shared resources.

Observe maximum accepted width, TDM transition points, maximum TDM ratio,
TDM/untimed net counts, busiest FPGA pair, and failure type.  Fit effective
payload capacity rather than multiplying advertised lane count by line rate.

### C4: latency decomposition

Use paired experiments so each sweep changes one variable:

- narrow single-hop traffic estimates the aggregate fixed intercept;
- width sweeps identify payload/TDM transition points; a separate continuous
  serialization slope is retained only if the ordinary reports make it
  independently identifiable;
- pair/hop sweeps estimate per-hop increments;
- TDM-ratio sweeps estimate slot-related increments;
- concurrent-flow sweeps estimate contention growth; and
- fanout sweeps estimate multicast effects.

The primary PPro timing signal is the worst cross-FPGA delay reported by the
normal system-timing output.  Record path count, cross-FPGA path count, maximum
TDM ratio, and pair load so a single worst scalar cannot hide a changed test.

### C5: transport resource cost

Hold DUT logic constant while sweeping endpoint count, aggregate cut width,
lane count, TDM ratio, multicast fanout, and buffering class.  Fit incremental
resource use from paired runs.  Reject a fit when unrelated repartitioning or
optimization changed the DUT placement; this stage requires fixed assignment.

### C6: joint fit and identifiability audit

- Fit parameters under public physical upper bounds and non-negative costs.
- Use grouped cross-validation so repeated variants of one generator cannot
  appear in both fitting and validation folds.
- Report parameter correlations and mark terms that cannot be separated.
- Produce nominal/conservative/aggressive profiles plus confidence intervals.
- Re-run the observation-to-model pipeline deterministically from normalized
  observations.

## Application-level blind validation

Microbenchmarks are used only for fitting.  Naturally connected upstream RTL
is used for holdout validation.  The existing catalog provides the following
progression:

| Tier | Workload | Calibration role |
| --- | --- | --- |
| smoke | SERV, PicoRV32 | Harness and frontend sanity only |
| medium | secworks AES | First unseen connected holdout |
| diversity | Ibex, VeeR EH1 | CPU hierarchy, memory, and SystemVerilog holdout |
| large | Koios GEMM, attention, convolution | Independent wide-datapath and hard-block holdouts |
| large primary | Koios DLA medium, then DLA large or TPU-like large | Multi-FPGA capacity, routing, TDM, and timing validation |
| very large final | NVDLA `NV_nvdla` | Million-cell hierarchy, capacity, and runtime stress |

Koios and NVDLA must not participate in fitting the microbenchmark
coefficients.  They test whether those coefficients generalize.  The existing
canonical Koios DLA medium case remains a useful EmuFlow Phase 1--7 regression,
but the calibration campaign must add at least one structurally different
large holdout before promoting the model.

For every holdout, compare PPro and calibrated EmuFlow on:

- minimum required FPGA configuration;
- per-resource utilization distribution;
- assignment distribution and busiest FPGA-pair ordering;
- routed cross-FPGA path and cut-net counts;
- maximum TDM ratio and TDM transition points;
- worst cross-FPGA delay and its scaling trend;
- success versus capacity/link infeasibility; and
- algorithm/configuration ordering, followed by complete EmuFlow Phase 1--7
  global WNS/TNS on the calibrated platform.

PPro and EmuFlow need not choose identical partitions for a free-optimization
holdout.  The primary research requirement is that feasibility boundaries,
pressure locations, scaling trends, and algorithm ranking agree.  Constrained
experiments, not free optimization, are used to fit hardware parameters.

## Promotion gates

The first calibrated platform may become an EmuFlow research default only when
an unseen application set meets all mandatory gates:

- exact agreement on the selected complete FPGA-count configuration;
- no false feasible result beyond public resource or I/O upper bounds;
- per-resource utilization error at most 10% on the declared comparable
  quantities;
- maximum TDM ratio exact or differing by at most one discrete ratio level;
- worst cross-FPGA delay error at most 15%;
- busiest-pair and major congestion ordering agreement;
- consistent relative ranking for at least two partition/routing/TDM choices;
- successful complete Phase 1--7 execution with zero unrouted nets, zero DRC
  violations, full original-path coverage, and independently validated global
  WNS/TNS; and
- a published residual/error report identifying every failed or
  non-identifiable metric.

Thresholds are initial engineering gates, not facts about PPro.  They may be
tightened after the first campaign but must not be relaxed after observing a
holdout merely to obtain promotion.

## Implementation stages

### Stage 1: public prior and observation schema

Status: **implemented on the calibration branch**. The implementation includes
strict Python validators, matching versioned JSON schemas, an official-source
LX2 public prior, synthetic mock observations, and corruption/redaction tests.
The prior intentionally leaves effective topology, payload capacity, TDM,
timing, and transport cost as `not_identifiable`; no public connector count is
promoted into a BoardDB edge.

Deliver:

- versioned public-platform prior schema;
- normalized black-box observation schema;
- provenance/redaction validator;
- mock PPro report fixtures containing no vendor-confidential data; and
- README documentation of the claim boundary.

Gate: deterministic schema round trip, corruption rejection, and proof that no
raw vendor path or private configuration is serialized.

### Stage 2: black-box runner and normal-report adapter

Status: **runner plus real ordinary-report adapter implemented; fresh C0 v2
smoke passed**. At source commit
`5b3c31294de797a9db727385674d6353f376f555`, an authorized ordinary run
completed in 35.68 seconds and produced all four allowlisted report classes.
The normalized observation recorded 68 cross-FPGA signals, four directed
one-hop route aggregates, maximum TDM ratio 1, and worst normalized
cross-FPGA delay 14.1 ns. These values qualify the runner/report boundary only;
they are not fitted platform parameters. The current
runner keeps commands, installation/license state, concrete target names, and
report paths in a non-serializable runtime binding. It executes isolated cases
through an explicitly bounded queue, distinguishes license/tool/infrastructure,
missing-report, and parse failures, and deletes raw reports after producing a
compact validated observation. A deterministic connected C0 workload generator
emits only provider-neutral RTL, hashes, and public experiment metadata. The
mock report profile is intentionally not accepted as real PPro evidence. The
separate `ppro-2026-ordinary-reports-v1` adapter has been checked against the
format of an existing successful normal PPro result. It reads only `pa0.rpt`,
`sr0.rpt`, and `sr0_time.rpt`, replaces physical FPGA names through a
runtime-only alias map, and stores directed route load as a compact aggregate
with `signal_count`. It does not read project XML, STF, BoardDB, pin maps, or
timing tables. This format check is not a substitute for the required fresh
runner smoke. A runtime-only renderer now emits the documented ordinary PPro
project/compile/pre-partition/partition/system-route sequence. It treats the
installed platform reference as an opaque path argument: only existence is
checked, and its contents are not read, copied, hashed, or serialized. The
provider-neutral constraint JSON is translated only into the `assign_inst`
form shown by the installed PPro user example; legal physical target names are
runtime bindings. Route, forced-TDM, and random-seed directives fail closed
until an equally documented user syntax is available. Generated Tcl, launcher,
absolute filelist, translated constraints, active project, and raw reports are
scrubbed after the compact observation is validated. Its command is covered by
a disposable fake-provider test. The production renderer also rejects case directories outside the
mandatory `/research/d4/gds/ziyiwang21` boundary and pins all standard temporary
environment variables below the case. This does not replace the fresh
authorized PPro smoke for any future incompatible runner or report-profile
revision.
The default pre-partition headroom is uniform across LUT, FF, BRAM, URAM, and
DSP at 75%; one explicit runtime option changes all five together so a capacity
comparison cannot silently use inconsistent resource limits.

Deliver:

- runtime renderer for documented PPro projects and explicitly supplied user
  constraints;
- queued runner with explicit concurrency and failure classification;
- allowlisted normal-report parser; and
- compact terminal observation output.

The CLI now generates capacity matrices, complete directed-pair topology
matrices with named holdouts, and individual communication probes. Real normal
reports are the default; mock output must be requested explicitly. Generation
is an active-run operation rather than a persistent checkpoint/campaign cache.
A one-shot campaign runner discovers only a bounded number of generated cases,
uses isolated result directories, defaults licensed concurrency to one, and
retains one compact observation per case after raw-project cleanup. Once every
case has a terminal observation, it deletes the strictly allowlisted generated
input bundles and prunes only empty matrix directories; unknown entries fail
closed rather than being removed.
Stage 4 matrices explicitly separate fit and holdout widths and repeat every
point. Transport-cost matrices generate matched local/cross placements with
the same RTL parameters and seed so the paired fitter can cancel DUT logic.

Gate: mock/dry-run first, followed by one real smoke experiment. The real C0
smoke fixes a generated `P0` producer and `P1` consumer to different logical
FPGAs using the documented `assign_inst` form so all ordinary report classes
are materialized; its holdout role still excludes it from fitting. Report PPro
runtime scale and artifact availability; never infer missing values.

### Stage 3: capacity and topology calibration

Status: **experiment/fitting framework implemented; BRAM/URAM/DSP boundaries
and the four-FPGA effective topology fitted, remaining capacity axes pending**. Capacity probes cover LUT-, FF-, BRAM-, URAM-, DSP-, and two mixed
resource axes using compact parameterized RTL and public inference attributes.
Requested units are controls, never treated as mapped resource counts. The
ordered-pair topology matrix covers both directions, requires repeated fit
trials for every pair, and adds independent holdout repeats for selected pairs.
Capacity fitting emits only a measured
pass/infeasible interval; topology fitting emits only observed directed
reachability and stable effective hop counts. Single-flow data explicitly
leaves shared-capacity groups `not_identifiable` instead of guessing them. The
topology matrix must use one consistent positive probe width, which is retained
in the fitted artifact; mixed-width matrices are rejected. Since ordinary route
reports expose per-hop aggregates, effective hop count is reconstructed on the
directed route graph using only edges whose signal count covers the complete
probe width; narrower clock/control traffic is not topology evidence. The
v4 capacity RTL places its swept payload below a real `P0` hierarchy before the
documented `assign_inst` constraint is emitted. Topology fixes only producer
and consumer instances and observes the tool-selected route; it does not claim
an unavailable route constraint or applied tool seed.
The capacity producer/fitter contract is version-locked at v4; large LUT
sweeps use hierarchy with at most 4,096 generated units per elaboration loop.
BRAM, URAM, and mixed-hard probes use one preserved helper-module hierarchy per
requested unit. This replaces v3 memory sweeps that PPro legally collapsed to
constant 2-BRAM/1-URAM footprints and that were therefore rejected as
non-identifying diagnostics rather than fitted evidence.
The topology producer/fitter remains version-locked at v2. Capacity infeasibility is
eligible evidence only when a bounded ordinary PPro console diagnostic names a
specific exhausted resource in the canonical “cannot be placed on any FPGA”
failure; generic partition failures, missing reports, license failures, and
infrastructure failures are not reclassified as capacity. Boundary records
preserve only their controlled design coordinate and never synthesize report
metrics that were not produced.
The run-spec evidence set is keyed to experiment semantics rather than forcing
all four reports on every case: resource/partition for capacity, plus route for
topology and payload, plus timing for latency and application-level gates.
This prevents a valid single-FPGA capacity observation from becoming a false
`missing_report` while preserving fail-closed requirements for each metric a
fitter actually consumes.
An authorized current-runner LUT pilot requested 150,000 v3 control units and
PPro reported 55,886 mapped LUTs at 2% utilization in 191.05 seconds. This
qualifies generation, execution, compact report parsing, and cleanup, but does
not satisfy the repeated pass/infeasible interval gate by itself.
An authorized v4 BRAM pilot requested 68 preserved 32-Kib memories and the
ordinary PPro report returned BRAM demand 68 at 4% utilization.  The
one-for-one demand and the public XCVU19P memory bound identify this report
column as a 36-Kib-class block count.  The observation schema records it as
`bram36k`; the final platform projection performs the explicit
`1 bram36k = 2 bram18k` conversion required by BoardDB.  This pilot validates
the measurement unit only; repeated pass/infeasible points remain mandatory
for the capacity interval.
Two authorized repeated v4 campaigns at source commit
`075a2b759f349e892d32830bb92c86d429d2a56f` produced 36 evaluated
hard-resource observations.  The fitted pass/infeasible intervals are
1580/1640 `bram36k`, 2850/2950 `dsp48`, and 238/245 `uram288`; no observation
was excluded and all six repeated withheld points passed as predicted.  This
qualifies those three axes only.  It does not substitute for the pending LUT,
FF, communication, application-holdout, or full-flow gates.

The authorized four-FPGA topology matrix at source commit
`ff55af4cef1024f1a1285660e35bcd643dd21953` completed all 28 generated cases:
two fit repeats for every ordered pair plus two holdout repeats for each of two
named pairs.  All cases passed, no observation was excluded, and both holdout
pairs reproduced the fitted state and hop count.  The effective graph is a
bidirectional `K2,2`: `F0/F1` each connect directly to `F2/F3`, while the two
same-side pairs are stable two-hop routes.  This result is deliberately stated
only as effective black-box reachability; connector identity and shared
capacity remain unidentified.

Deliver resource-boundary and ordered-pair experiment matrices, fitted
effective capacities, effective reachability, and shared-resource hypotheses.

Gate: repeated measurements and at least one withheld pair/resource boundary.

### Stage 4: bandwidth, TDM, latency, and transport calibration

The communication producer and payload/latency/transport fitters share the
version-locked `ppro-blackbox-communication-probe-v3` contract. Older v1/v2
observations are rejected rather than mixed into a current fit.

Status: **controlled probe and fitting framework implemented; effective
payload/TDM classes and aggregate latency fitted; PPro transport observations
completed but proved non-identifiable from ordinary reports**. One compact communication generator sweeps width, parallel flows,
direction, fanout, and requested TDM level while keeping logical endpoints
explicit. Payload fitting reports the repeated ratio-one/TDM transition and
observed TDM levels; a final link-infeasible upper bound is retained when
observed but is not confused with per-cycle payload width. Aggregate latency
fits non-negative endpoint, hop, contention, and multicast terms plus a
categorical delay for every observed TDM ratio.  This matches the discrete
black-box timing states without pretending that line rate or a per-bit
serialization slope was identified.  Every fitted ratio, including ratio one,
requires an independent holdout.  Deterministic residual bootstrap preserves
the complete controlled design matrix in every confidence replicate, so a rare
but identified TDM state cannot disappear from its own interval. Transport
cost uses same-RTL local/cross placement pairs so DUT logic cancels before
fitting incremental resource cost; negative paired deltas fail closed as
evidence that unrelated mapping changed. An opaque pairing token binds each
local/cross pair without exposing a vendor artifact, and local baselines retain
the cross case's fanout. Latency reconstructs full-width end-to-end paths from
ordinary per-hop route records.  Its capacity-aware reconstruction can combine
parallel striped paths, still requires their aggregate capacity to cover the
full logical transported width, and does not let narrower control-only routes
masquerade as payload.  TDM remains an independent scheduling/delay feature;
it does not divide the logical route-path count. Both regression fitters
require full column rank plus independent holdouts; they do not silently
publish unidentifiable zero coefficients. The real adapter currently observes
natural TDM transitions under width/flow pressure. Nonzero forced-TDM probes
fail closed until their user-facing PPro constraint syntax is documented.

At source commit `e0b1f74a6042d52c6b65e45a93d763e67ff459b0`, the authorized
four-FPGA payload campaign completed 96 real runs without a failed case.  The
initial sweep found that four directions were still ratio one at 128 bits.
Those 256-bit discovery holdouts were excluded from the final fit and replaced
by 256-bit fit trials plus new 512-bit holdouts, preventing adaptive holdout
leakage.  The final selected set contains 88 observations, eight directed-link
signatures, zero excluded observations, and 16/16 matching holdout checks.
Four directions have a conservative ratio-one lower bound of 64 bits and four
have 128 bits; every observed transition enters ratio 8.  The asymmetric
classes are retained rather than collapsed into one convenient link width.

The latency campaign completed 56/56 real PPro runs: 34 fit observations and
22 independent holdouts over direct, multicast, and two-hop shapes.  The
original matrix lacked a ratio-16 holdout, so a distinct 96-bit/four-flow
holdout shape was run twice; both repeats reproduced ratio 16 and 55.2 ns.
The final v2 model has zero excluded observations, approximately `3.17e-10 ns`
fit RMSE, and approximately `7.85e-11` maximum holdout relative error.  It
identifies a 0.2 ns endpoint intercept, 7 ns per-hop increment, 0.1 ns per
additional multicast sink, no observable concurrent-flow increment after TDM
state is included, and categorical increments of 43 ns and 48 ns for ratios 8
and 16.  These are aggregate behavior terms, not reverse-engineered internal
implementation details.

The paired transport campaign completed 64/64 PPro runs, giving 16 fit pairs
and 16 holdout pairs.  Ordinary `pa0.rpt` exposed zero delta for FF, BRAM, DSP,
and URAM in every pair.  LUT deltas were limited to 0/1/2 cells and failed the
holdout gate at 75% maximum relative error.  The report therefore does not
identify the cost of PPro's inserted transport shell.  The fitter records this
as negative evidence: all-zero response or holdout error above 15% sets every
affected coefficient to `identifiable=false`, so Stage 5 cannot promote the
result.  EmuFlow's TransportCostDB will instead be characterized from its own
open transport RTL and labelled with separate source-backed provenance; the
PPro black-box observations remain a guard against falsely claiming that the
ordinary report measured proprietary transport internals.

Deliver parameter sweeps, constrained fits, confidence intervals, and an
identifiability report.

Gate: withheld widths, ratios, and contention levels meet the declared error
bounds. Platform materialization requires zero excluded observations, resolved
capacity/payload holdouts, exact topology holdout agreement, full-rank latency
and transport fits, and at most 15% nonzero holdout relative error.

### Stage 5: platform generator and EmuFlow integration

Status: **artifact generator and independent validators implemented; real
calibrated instance pending**. The generator converts only already-fitted,
immutable inputs into aggressive/nominal/conservative BoardDB,
BoardLinkTimingDB, and TransportCostDB profiles. Public VU19P totals remain the
hard capacity ceiling; BoardDB v1's single utilization limit is the minimum of
the fitted per-resource effective/public ratios. Only observed one-hop edges
become BoardDB links, and every observed multi-hop pair must be reproduced by
the resulting shortest paths. Ratio-one payload evidence is mandatory for
each direct edge. Timing is marked `characterized-upper-bound`, never measured
signoff; fabric clock remains an explicit sensitivity assumption. The writer
stores only final artifacts and a canonical hash manifest, then re-reads and
validates all three contracts independently.

Deliver generated BoardDB, BoardLinkTimingDB, TransportCostDB, a parameter
provenance manifest, and independent validators.  Generated profiles are
immutable inputs to a run; model fitting is never performed in the production
Phase 1--7 hot path.

Gate: deterministic generation, schema validation, and small complete-flow
acceptance.

### Stage 6: blind large-design validation

Status: **promotion contract and evaluator implemented; real blind runs
pending**. A scratch-only application bundle generator now binds an existing
checked benchmark contract and natural RTL source tree to a free-partition
PPro `application_holdout`; source paths and the EmuFlow platform choice never
enter the compact observation. Each case joins one passing PPro observation
to a complete EmuFlow Phase 1--7 summary produced with physical seed 1 and
authoritative OpenSTA global timing. Promotion requires the same complete
configuration, comparable resource utilization within 10 percentage points,
TDM ratio within one level, cross-FPGA delay within 15%, major busiest-pair
ordering agreement, macro-cycle/schedule legality, zero unrouted nets and DRC,
and complete original-path coverage. It additionally requires all AES/CPU,
large, DLA, and NVDLA tiers plus matching ranking for at least two algorithm
variants on one workload. Global WNS/TNS are mandatory evidence, not replaced
by an intermediate Phase 3--6 metric.

Run secworks AES and one CPU holdout, then Koios GEMM/attention, Koios DLA
medium/large, and finally NVDLA if the smaller gates pass.  Keep one physical
seed unless a variance study is explicitly requested.

Gate: the promotion criteria above, including a complete Phase 1--7 run and
global WNS/TNS.  A Phase 3/4/5 or PPro-only comparison is not completion.

## Execution and storage policy

- Deploy only exact pushed Git commits and versioned checkouts.
- Keep PPro raw results and active scratch outside the repository and within
  the authorized experiment storage boundary.
- Run each experiment in one isolated directory and retain only its compact
  normalized observation or terminal failure summary after validation.
- Do not create a persistent Phase 1--7 checkpoint cache.  Independent
  calibration points can run concurrently because they are complete,
  self-contained experiments, subject to the licensed PPro concurrency limit.
- Never count license, SSH, provider, parser, or missing-report failures as
  measured platform behavior.
- Do not write server paths, credentials, licenses, or raw vendor report
  contents into source control.

## Completion definition

The black-box platform effort is complete only when:

1. calibration can be repeated from public priors, generated experiments, and
   normalized observations without accessing internal PPro configuration;
2. all model parameters have provenance, bounds, and an identifiability status;
3. independent application holdouts satisfy the promotion gates;
4. EmuFlow completes Phase 1--7 on the generated platform with authoritative
   global timing; and
5. the documentation states exactly which claims are public-spec,
   PPro-behavior-equivalent, assumed, or unavailable.
