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

For the indexed Vivado provider, vendor timing summary WNS/TNS are partition
endpoint diagnostics. They cannot substitute for original-member global
metrics. Every imported same-FPGA member and cross-FPGA segment must have
source-bound routed evidence. A through-point query returning an enclosing
path must not be labeled an exact subsegment merely because it traverses both
requested pins; its delay must be restricted to the declared segment or carry
an explicitly supported conservative qualification.
