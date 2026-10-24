# Global timing model and qualification

Physical flows default to `--global-timing-engine opensta`. OpenSTA computes
the numeric results from bound physical logic/interface/link arcs and explicit
transport-event constraints. It does not implicitly run the separate Python
timing composer. Select that engine explicitly with
`--global-timing-engine python`.

With the repository OpenSTA container launcher, `executable_sha256` identifies
the delegated native OpenSTA engine, while `launcher_sha256` separately seals
the launcher. Engine identity must not be inferred from the wrapper alone.

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

Every declared original member must have exactly one implementation disposition:
physically timed, or proven optimized away. Original identities remain covered
in both cases. Eliminated records have no numerical delay or slack and must
reference a checked source-bound proof. A missing source tag alone is not proof.

Global WNS is the minimum slack over surviving implemented original members.
Original-path TNS sums their negative slack once per member; it is not conventional
worst-slack-per-register-endpoint TNS. Separate TX/relay/commit checks are
correctness requirements and are not added to that TNS.
Reports must identify the population policy and give original, implemented, and
optimized-away counts separately. If every member is eliminated, WNS is N/A,
TNS is zero, and there is no synthetic worst path. Missing or unproved members
still make qualification incomplete.

Cross-FPGA elimination uses the source-bound disposition of a required logic
segment or shadow interface, after validating the complete segment and schedule
chain. It does not remove scheduled events or original members from structural
coverage. Interface and segment summaries distinguish measured and eliminated
records; only implemented original members produce numerical event checks.

The report identifies
`implemented-original-paths-with-explicit-optimization-dispositions-v1`.
Its original-path count and ID seal include optimized-away records, while its
implemented count, endpoint-exact/bound counts, and engine-check count exclude
them. Eliminated records retain the FPGA/source proof reference and null delay
and slack fields. Global binding checks that the proof belongs to the requested
source endpoint. Conditional initialization candidates are not elimination
proofs. When no implemented original members remain, no numerical OpenSTA run
is claimed; the report identifies the absence of applicable checks.

Coverage means the source TimingPathDB population. It does not prove that
source extraction enumerated every possible sensitizable path. A reached
extraction limit must be reported.

Physical bounds remain bounds. Model-only board timing does not become measured
hardware data. Hold/min-delay, metastability, clock-tree skew, unconstrained CDC,
and silicon PVT signoff require additional inputs and validation.

An optimized Vivado source endpoint can have several physical descendants.
Taking their worst routed delay is an
`optimized-endpoint-collection-upper-bound`, not an exact original path.
Typed segment stores retain this measurement kind and its separate count.
The historical `cone_bound_logic_paths` aggregate includes conservative endpoint
collection bounds, as well as cone and local-path bounds; it is excluded from
`endpoint_exact_logic_paths`. Complete original-member coverage remains required.

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
metrics. Every implemented same-FPGA member and cross-FPGA segment must have
source-bound routed evidence; eliminated members retain their explicit proof
references instead. A through-point query returning an enclosing
path must not be labeled an exact subsegment merely because it traverses both
requested pins; its delay must be restricted to the declared segment or carry
an explicitly supported conservative qualification.
