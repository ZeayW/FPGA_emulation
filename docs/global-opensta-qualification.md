# Global timing model and qualification

Physical flows default to `--global-timing-engine opensta`. OpenSTA computes
the numeric results from bound physical logic/interface/link arcs and explicit
transport-event constraints. It does not implicitly run the separate Python
timing composer. Select that engine explicitly with
`--global-timing-engine python`.

## Fixed events and original logical paths

Transport registers split ordinary STA paths. Register-to-register setup slack
alone is not the original DUT launch-to-capture macro-cycle latency.

The timing abstraction therefore includes both event readiness checks and a
terminal observation per original TimingPathDB member. A frozen TX edge has an
explicit launch/readiness deadline relative to the frame epoch. A missed event
cannot wrap into a later frame to appear legal. TDM wait is not an ordinary
combinational Liberty arc.

Physical logic/interface delays and BoardLinkTimingDB supply the arc model.
Without characterized link timing, BoardDB cycle latency remains a model-only
input. Timing abstraction files are analysis inputs, not new synthesizable
transport hardware.

## Current event-stream contract

Checks and imported OpenSTA measurements must contain one contiguous bundle
per original path, including exactly one target and one runtime observation.
A path cannot reappear after its bundle ends. Interleaved historical records
are rejected; the current flow does not regroup them into whole-design maps.
Export, representative compression, independent validation and result stitching
all use this contract. Complete numerical bundles remain the unit of
compression, and expansion preserves every original member.

Timing reconstruction must supply each scheduled hop's concrete
`fixed_slot_tx_time_ns` and its bound TDM ratio where the link model requires
one. Global binding consumes those fields directly. It rejects an incomplete
reconstruction instead of rebuilding a second schedule-entry dictionary.
Historical artifacts requiring the retired reconstruction or interleaved order
must be examined with their matching Git version.

## Metrics and scope

Global WNS is the minimum slack over the declared original-path population.
Original-path TNS sums negative slack once per member; it is not conventional
worst-slack-per-register-endpoint TNS. Separate TX/relay/commit checks are
correctness requirements and are not added to that TNS.

Coverage means the source TimingPathDB population. It does not prove that
source extraction enumerated every possible sensitizable path. A reached
extraction limit must be reported.

Physical bounds remain bounds. Model-only board timing does not become measured
hardware data. Hold/min-delay, metastability, clock-tree skew, unconstrained CDC,
and silicon PVT signoff require additional inputs and validation.

## Qualification

Use hand-computed and adversarial cases for local paths, slot zero, multi-hop
relays, consecutive combinational cuts, uncertainty, late TX with legal final
commit, missing measurements, and different clock periods. Compare arrival,
required time, and slack per original member rather than only WNS/TNS.
Quantify numerical tolerance without using a long frame period to hide a
short-path mismatch.

A complete real-design gate also needs physical implementation, original-member
coverage, and delay qualifications. Terminal validation reconstructs raw binding
and checks saved engine scalars; it does not need to run STA or the alternate
Python engine again.

`scripts/opensta/opentimer_event_check.cpp` is an optional offline OpenTimer
driver for independent model checks. It is not a production dependency or a
second implicit large-design analysis pass. See
[validation requirements](VALIDATION.md) for the complete claim boundary.
