# Behaviorally Calibrated Academic Platform

## Objective

Build an open, editable multi-FPGA platform model whose externally visible
resource, communication, TDM, transport, and timing trends agree with an
authorized reference flow closely enough for academic algorithm comparison.
The result is not a physical replica, a bitstream target, or a reconstruction
of the reference tool's private algorithms.

This is analogous to a calibrated architecture simulator: controlled tests
identify the model parameters, and workloads not used during fitting test
whether the model preserves capacity decisions, performance trends, and
algorithm rankings.

## Non-negotiable boundaries

- Raw reference reports, licensed databases, file paths, binaries, credentials,
  and proprietary implementation details remain outside Git.
- Repository artifacts use an opaque reference alias and authorization ID.
- Public output contains only parameters and aggregates permitted by the
  recorded publication scope.
- Fixed FPGA assignment and a topology with a unique source-to-sink path
  isolate hardware behavior during fitting. The observed assignment and route
  must match those controls. A free run combines hardware behavior with the
  reference tool's optimization decisions and therefore cannot identify
  hardware parameters.
- The platform offers only explicitly supported configurations. Asking for
  eight FPGAs cannot synthesize a new topology unless an eight-FPGA
  configuration was independently observed and declared.

## Model equation

The link-delay model is:

```text
delay_ns = endpoint_ns
         + hop_count * per_hop_ns
         + interpolate(tdm_penalty_curve_ns, max_tdm_ratio)
```

`max_tdm_ratio` is an observed aggregate from the reference run. It already
reflects the provider's serialization, multiplexing, and contention decisions,
so the model must not add separate payload-width or flow-count penalties and
double-count them. Providers may implement discrete TDM tiers: controlled PPro
measurements showed a large ratio-2 to ratio-8 transition and a much smaller
ratio-8 to ratio-16 transition, invalidating a per-ratio linear cost. The
non-negative endpoint and hop terms plus one penalty per observed TDM tier are
fitted only when the controlled matrix has full rank. The tier curve must be
monotone; interpolation is reserved for ratios not directly characterized.

Capacity is represented as a raw-device interval. Each controlled demand is
normalized by the utilization limit used for that run, so a small probe at a
1% limit identifies the same raw-capacity boundary without elaborating millions
of synthetic cells merely to reach a 75% boundary. The greatest normalized
passing demand is the lower bound and the smallest normalized capacity failure
is the exclusive upper bound. Conservative, nominal, and aggressive profiles
select raw capacities only inside this interval; BoardDB applies the model's
separate utilization limit when deriving effective capacity.

Reference-flow and open-flow resource counters are not assumed to share a
universal unit.  The fitter converts each capacity axis with two or more
isolated, same-RTL mapping probes.  LUT/FF probes use banked sequential
feedback networks so neither mapper can collapse a large nominal structure to
a small Boolean function; BRAM/DSP probes retain their inferred hard blocks.
This conversion is used to express a measured capacity boundary in the open
mapper's units.  It is not a promise that arbitrary application resource
reports differ by the same fixed ratio: technology mapping is
structure-dependent.

Logical TDM service, physical serializer throughput, and offered-load tolerance
are separate quantities. BoardDB exposes one independently schedulable logical
bit per characterized directional channel. PHY width, line rate, and fabric
clock describe the serializer behind those channels; multiplying channel count
by PHY width would incorrectly turn serial line bits into independent TDM
lanes. Offered load is checked separately against the provider-declared maximum
ratio. A passing TDM-expanded workload is therefore never mislabeled as
evidence that the physical link has that many parallel logical bits per slot.

## Artifact flow

```text
authorized reference flow
        |
        | controlled aggregate observations (outside repo)
        v
platform-calibration-observations/v1  +  calibrated-platform-template/v1
        |
        | calibrated-fit
        v
calibrated-academic-platform/v1
        |                         \
        | blind micro holdout       \ named configuration + profile
        v                           v
validation/v1                  BoardDB v1 + BoardLinkTimingDB v1
        |
        | free-partition workload excluded from fitting
        v
application-validation/v1
```

The template owns topology. The observation set owns measurements. The fitted
model owns parameter intervals and provenance. BoardDB remains the normal
consumer interface, so Phase 3--7 do not parse calibration diagnostics.
Materialization can also emit a direction-complete BoardLinkTimingDB carrying
the characterized sub-cycle link bound. BoardDB retains the integer cycle
latency needed by transport RTL, while timing-driven routing and final global
timing consume the non-rounded bound.

## Staged implementation and validation

### Stage 0 — authorization and release boundary

Status: structurally implemented. The artifacts require an authorization ID,
source class, and publication scope. Legal documents themselves are not stored
in the repository.

### Stage 1 — calibration contracts and deterministic fitter

Status: implemented on this branch.

- strict Python validators and six JSON schemas;
- controlled capacity/link boundary inference;
- identifiable non-negative delay fitting;
- disjoint blind-holdout validation;
- named-configuration BoardDB materialization;
- synthetic unit tests with known ground truth.

### Stage 2 — reference microbenchmark generator and collector

Status: implemented and exercised by the authorized internal campaign; no raw
report or reference-derived numeric parameter is committed to the repository.

`calibrated-campaign-plan` materializes isolated LUT-, FF-, BRAM-, DSP-,
same-RTL resource-unit-mapping, link-width-, hop-, TDM-, and contention probes
plus fixed-assignment constraints
using hard instance-to-FPGA bindings, plus topology-unique controlled routes.
It deliberately does not add `-exclusive`: in PPro that option reserves the
entire target FPGA for the named instance and would invalidate multi-instance
capacity and contention probes. Every task includes a cold-start Tcl
runner that stops after `run_system_route`; the resource utilization limits are
explicit campaign inputs. `calibrated-campaign-collect` accepts a small,
versioned result contract. It excludes provider, license, transport, and
execution failures instead of misclassifying them as capacity failures. The
planner/collector and mock reports have unit-test coverage. The external PPro
adapter must report the observed assignment and route; the collector compares
both against the planned controls before accepting a measurement.

Every generated case contains `run_ppro.sh`, which loads the external PPro
environment and invokes the documented
`rtlpart_linux -script_file run_ppro.tcl` interface. The wrapper sets `TMPDIR`
below the isolated case directory and does not copy the reference installation,
license, or reports into repository artifacts. Its manifest identity is
`ppro_rtlpart_script_file_v1`.
Fresh cold-start pre-partition intentionally omits the provider's optional
`-res_result` input. The private adapter consumes the provider-generated
`*_InstResourceResult.csv` report after the run; fitting uses that realized
resource demand, never the nominal RTL generator count. The generated
LUT/FF/BRAM/DSP structures carry preservation/inference attributes and avoid
algebraically cancellable reductions or inferred shift registers. A contention
probe creates one independently preserved and fixed endpoint pair per flow;
concatenating all flows into a single wider bus is expressly not treated as a
contention experiment. A valid fit campaign pairs equal-total-width cases with
different flow counts so payload serialization and endpoint contention are
separately identifiable.
The launcher writes one atomic `runner.exit-code` file; it neither duplicates
stdout nor converts raw diagnostics into repository artifacts.
Each case manifest carries the complete logical-target map needed to translate
the observed reference assignment and every intermediate route hop back into
the provider-neutral campaign namespace.

Campaign inputs and raw reports remain under the approved external experiment
root. Only compact aggregate observations enter the fitter; raw reports and
licensed topology files are never copied into Git.

### Stage 3 — parameter campaign

Status: authorized internal controlled campaign complete for the current named
three-FPGA configuration; publication of fitted numeric parameters remains out
of scope for this branch.

The completed campaign identifies all four modeled resource-capacity intervals,
logical TDM service, physical serializer characteristics, endpoint/per-hop
delay, and a monotone observed-TDM-tier penalty curve. Fit and holdout datasets
are disjoint. Provider, license, SSH, and infrastructure failures remain
separate from capacity outcomes. Additional platform sizes require their own
controlled campaigns; topology is never extrapolated from a requested count.

### Stage 4 — blind application validation

Status: complete for one authorized Koios DLA medium holdout excluded from
fitting.

`calibrated-application-holdout-validate` predicts the minimum active FPGA count
from fitted raw capacities at the observation's declared utilization limit,
checks that the reference and open mappings make the same per-resource FPGA
count decisions, rounds aggregate directional cut load to a characterized TDM
tier, and predicts the worst cross-FPGA delay from the fitted timing model. It
checks exact active FPGA count, exact resource-capacity decisions, bounded
TDM-tier error, and bounded delay-relative error. Raw cross-mapper application
count error is retained as a diagnostic rather than misrepresented as a
hardware-behavior gate. The completed blind DLA run passes all four behavioral
gates: both mappings require two FPGAs (DSP is the binding resource), TDM ratio
is predicted exactly, and worst-cross-FPGA delay error is 1.33%. Its raw
resource-count diagnostic reaches 34.96%, confirming that a universal
application-count scale would be invalid. It is a free-partition
application check and is never reused to fit hardware parameters. GEMM/NVDLA
remain useful future holdouts, not prerequisites for the current model contract.

```sh
emuflow platform calibrated-application-holdout-validate \
  --model calibrated-model.json \
  --observation blind-application-observation.json \
  --output application-validation.json
```

### Stage 5 — full EmuFlow qualification

Status: complete for one fresh Koios DLA medium Phase 1--7 qualification on the
named three-FPGA nominal configuration, using an explicit 10% utilization
stress override, baseline Phase 6, and physical seed 1.

Register qualified workload/platform combinations in the canonical validation
matrix, run a fresh complete Phase 1--7 flow with one physical seed, and retain
only compact terminal evidence. Final claims use system-global WNS/TNS. The
calibrated model is promoted only if holdout trends and algorithm ordering agree
with the reference within declared error bounds.

The completed one-shot qualification did not reuse a stage checkpoint. It
synthesized 379,357 instances, used all three FPGAs, cut 1,404 nets, scheduled
1,867 board hops, and added 4,989 transport cells. All three VTR physical
implementations passed; the aggregate physical result has zero DRC violations,
zero unrouted nets, and a worst local backend slack of +1.61078 ns. Phase 7C
passed schedule legality and macro-cycle equivalence with no mismatches.

The independent OpenSTA route covered all 195,532 original TimingPathDB paths
(188,418 local and 7,114 cross-FPGA). At the realized 256-slot, 200 MHz fabric
schedule, the virtual period is 1,280 ns and the runtime result is WNS
+607.021434 ns / TNS 0 ns. The separate 10 ns source-clock target comparison is
WNS -662.978550 ns / TNS -158,313.972324 ns; it is a target-frequency QoR
diagnostic, not the actual emulation runtime frequency. The OpenSTA executable
identity is sealed by SHA-256 when its build banner lacks a source revision.
Independent terminal validation with physical evidence required also passed.

This result qualifies the implementation and the calibrated academic-platform
contract; it does not prove equivalence to proprietary hardware. The blind
application gate predicted two FPGAs at its declared utilization, whereas this
deliberately stricter 10% stress qualification used three. The VTR backend is
an academic physical model, and the characterized board-link timing is not a
measured hardware sign-off (`final_board_link_timing_signoff=false`).

For a deliberately capacity-stressed qualification, `calibrated-materialize`
accepts an explicit lower `--utilization-limit`. It may never exceed the
model's declared default, and the generated BoardDB name records the override
in basis points. This keeps a 10% communication stress run distinguishable
from the normal 75% research platform.

## Current limitations

- The current model does not yet fit transport LUT/FF/BRAM overhead; the full
  qualification reports the realized open-flow transport cost, but fitting that
  cost against the reference requires a separate controlled delta-resource
  observation family.
- It models one link class per academic platform. Heterogeneous link classes
  require a schema revision rather than implicit special cases.
- The model does not claim cycle-accurate equivalence, package-pin closure, or
  bitstream deployability.
- No real reference-derived values are committed yet. Public tests prove the
  fitter and contracts against synthetic ground truth; the completed internal
  pilot proves that the private adapter can execute and verify real controlled
  cases without moving vendor reports into Git.
