# Validation requirements

Validation establishes a specific claim on a declared input population.
A structural check, functional fixture, optimizer objective, and physical
timing result answer different questions.

## Functional and structural gates

- Preserve circuit connectivity, primitive parameters, clock/reset semantics,
  resources, and stable identities across representation changes.
- Check capacity, fixed/group ownership, SCC/stateful/cascade atomicity,
  topology, and route-hop constraints independently of the optimizer.
- Check complete route and schedule-entry coverage, direction, ratio/lane/slot
  legality, and transport boundary identity.
- Check macro-cycle equivalence and startup/shadow semantics for the selected
  transport. A finite trace is not general formal proof.
- Admit each physical primitive and constraint explicitly; account for every
  cell and check site/BEL/pin ownership, macro/cascade legality, DRC, and
  unrouted nets.

A zero-combinational-cut flow is a compatibility test, not Static Exact
feature evidence. Real-design qualification must use naturally selected cuts,
not assignments forced to make a feature appear exercised.

## Timing acceptance

A complete physical flow requires explicit clock periods, routed logic and
interface timing, concrete board routes/TDM events, and original-path binding.
Missing data makes the result incomplete; it must not be replaced by guessed
delays or silently dropped paths.

Global WNS is the minimum slack over the declared original-path population.
Global original-path TNS sums each negative member slack once. Same-FPGA and
cross-FPGA paths must form a disjoint union of that population. Report:

- source/original-member coverage and any extraction limit;
- target-clock and virtual-runtime-clock WNS/TNS;
- negative-path counts and the critical path;
- per-FPGA physical diagnostics, separately labelled;
- exact-delay, analytical-model, and conservative-bound qualifications;
- transport readiness/relay/commit failures.

An optimizer representative may establish a worst path, but it must be expanded
to its original members before original-path TNS is accumulated. The metric is
not conventional unique-register-endpoint TNS. See
[global timing scope](global-opensta-qualification.md).

## Fair algorithm comparisons

Use real RTL and an explicitly identified platform. Raw contest communication
graphs cannot substitute for synthesis and FPGA-internal physical implementation.

Freeze the common inputs, target periods, mapping/device models, tool versions,
and physical seed. Only the intended variable may change. For a Phase 6
comparison, assignment, routes, and TDM schedule are common inputs. For a
partitioning comparison, downstream assignment/routes/schedules may differ and
must not be mistaken for controlled inputs.

Default acceptance uses physical seed 1; multiple seeds require an explicitly
declared variance study. Small exact references qualify correctness. Provider
or default-selection changes additionally need a materially sized real-design
comparison through complete physical Phase 7 and global timing. Intermediate
metrics do not establish end-to-end QoR.

Report absolute slack changes and negative-deficit reduction. If the baseline
already has nonnegative WNS or zero TNS, percentage deficit improvement is not
defined. Keep regressions in secondary metrics visible.

## Workload and platform identity

[The full-flow registry](../benchmarks/end_to_end_validation_matrix.json)
identifies real workloads and platforms separately.
[The contest registry](../benchmarks/contest_validation_matrix.json) covers
communication inputs only. Raw contest topology combined with a VTR device
template is a derived academic platform, not the original contest's physical
hardware.

A successful run does not prove untested workload/platform combinations.
Bitstream generation, measured-link timing, CDC/hold/PVT signoff, and actual
board operation require their own evidence.

## Execution and retention

Keep experiment configuration, machine paths, raw reports, timing populations,
placement/routing directories, and iteration histories outside the source
repository. Use isolated active-run scratch; retain compact final summaries
outside the repository after terminal validation.

Do not restart computation merely because an observer disconnected. Check its
recorded identity and process tree. Remove scratch only when its producers and
consumers have stopped. Do not publish a partial stage as a completed flow.
