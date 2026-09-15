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
- Fixed FPGA assignment and fixed route isolate hardware behavior during
  fitting. A free run combines hardware behavior with the reference tool's
  optimization decisions and therefore cannot identify hardware parameters.
- The platform offers only explicitly supported configurations. Asking for
  eight FPGAs cannot synthesize a new topology unless an eight-FPGA
  configuration was independently observed and declared.

## Model equation

The v1 link-delay model is:

```text
delay_ns = endpoint_ns
         + hop_count * per_hop_ns
         + (serialization_cycles + tdm_wait_slots) * slot_ns
         + contention_units * contention_ns
```

`serialization_cycles` is derived from payload width and the fitted effective
payload bits per cycle. The non-negative endpoint, hop, and contention terms
are fitted only when the controlled experiment matrix has full rank. EmuFlow
rejects an experiment that cannot distinguish these terms.

Capacity is represented as an interval. The greatest controlled passing demand
is its lower bound and the smallest controlled capacity failure is its exclusive
upper bound. Conservative, nominal, and aggressive profiles select values only
inside this interval. Raw BoardDB capacity is derived from the selected
effective capacity and the declared utilization limit.

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
        | blind holdout            \ named configuration + profile
        v                           v
validation/v1                  BoardDB v1
```

The template owns topology. The observation set owns measurements. The fitted
model owns parameter intervals and provenance. BoardDB remains the normal
consumer interface, so Phase 3--7 do not parse calibration diagnostics.

## Staged implementation and validation

### Stage 0 — authorization and release boundary

Status: structurally implemented. The artifacts require an authorization ID,
source class, and publication scope. Legal documents themselves are not stored
in the repository.

### Stage 1 — calibration contracts and deterministic fitter

Status: implemented on this branch.

- strict Python validators and four JSON schemas;
- controlled capacity/link boundary inference;
- identifiable non-negative delay fitting;
- disjoint blind-holdout validation;
- named-configuration BoardDB materialization;
- synthetic unit tests with known ground truth.

### Stage 2 — reference microbenchmark generator and collector

Status: pending.

Generate small LUT-, FF-, BRAM-, DSP-, link-width-, hop-, TDM-, contention-,
and transport-overhead sweeps. First validate manifests and parsers with mock
reports; only then run authorized PPro jobs. Collection output stays under the
approved external experiment root and produces a compact aggregate observation
file rather than copying reports into Git.

### Stage 3 — parameter campaign

Status: pending.

Run controlled fixed-assignment/fixed-route tests across the explicitly
supported 2/4/8-style configurations available in the reference platform.
Parallel jobs may use independent HPC nodes subject to the actual license
limit. Separate provider, license, SSH, and infrastructure failures from
capacity outcomes.

### Stage 4 — blind application validation

Status: pending.

Use workloads excluded from fitting: Koios DLA/GEMM and NVDLA where feasible.
Validate minimum FPGA count, resource-loading distribution, pair-load ordering,
maximum TDM ratio, worst cross-FPGA delay, and trend error. Initial gates are
10% mean and 15% maximum link-delay relative error plus exact controlled
capacity outcomes; thresholds remain versioned in the template.

### Stage 5 — full EmuFlow qualification

Status: pending.

Register qualified workload/platform combinations in the canonical validation
matrix, run a fresh complete Phase 1--7 flow with one physical seed, and retain
only compact terminal evidence. Final claims use system-global WNS/TNS. The
calibrated model is promoted only if holdout trends and algorithm ordering agree
with the reference within declared error bounds.

## Current limitations

- v1 does not yet fit transport LUT/FF/BRAM overhead; that requires a separate
  controlled delta-resource observation family.
- It models one link class per academic platform. Heterogeneous link classes
  require a schema revision rather than implicit special cases.
- The model does not claim cycle-accurate equivalence, package-pin closure, or
  bitstream deployability.
- No real reference-derived values are committed yet. Current tests prove only
  the calibration machinery against synthetic ground truth.
