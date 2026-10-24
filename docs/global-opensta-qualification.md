# Global timing model and qualification

OpenSTA is the only production global physical timing engine. It computes
numeric results from bound physical logic/interface/link arcs and explicit
transport-event constraints. The Python timing engine and historical
comparison/adoption replay are retired. Independent numerical oracles are
bounded test utilities and are not installed with the production package.
Unsupported physical evidence fails explicitly; no partition-maxima or
alternative-engine fallback substitutes for missing event/segment coverage.

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

The following disposition contract is a completion requirement. The indexed
Vivado proof owner exists, but terminal global timing does not yet consume
elimination records; unresolved endpoints still fail coverage.

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
and checks saved engine scalars without running STA again. Historical physical
reports without standalone OpenSTA evidence require their matching Git version;
the current terminal validator does not reconstruct them with another engine.

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
