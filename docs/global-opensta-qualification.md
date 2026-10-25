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

## Ratio estimates and schedule-resolved PHY timing

The required serial-PHY timing contract has two stages. Before the concrete
signal order is known, a ratio-dependent formula estimates the complete
cross-FPGA delay conservatively. After scheduling, per-signal timing is
resolved from the actual execution events instead of assigning the same ratio
estimate to every member. This is the contract required for the
[8:1 PHY reference](HARDWARE_PLATFORM.md#serial-phy-time-units); the current
one-bit-per-fabric-cycle adapter has not qualified its implementation.

For the bundled platform, the planning expression is
`D_pre(R) = 30 ns + R * 0.625 ns = 30 ns + (R / 8) * 5 ns`.
Its TDM clock is 200 MHz; the supplied PHY reference's fixed 45 ns is not
substituted for the platform's configured 30 ns. Post-schedule analysis retains
that same fixed component and replaces only the variable estimate with each
signal's resolved TDM contribution. Under the supported model assumptions,
`0 <= T_TDM,i <= 0.625R` and `D_post,i <= D_pre(R)`; do not clamp a violating
measurement to satisfy the bound.
Using it as a worst-case bound requires a stated readiness/phase/round policy
and characterization that supports that bound. The algebra alone does not
prove the bound, or identify an externally fitted formula as average or worst
case. Every serial/word timing model must state that scope explicitly.

Post-schedule analysis must retain the following events per signal and hop:

- `ready`: the value is available after its preceding routed logic;
- `word_load`: its assigned word is sampled by the TX PHY;
- `serial_tx`: its bit is transmitted within that word;
- `rx_visible`: the receiving interface makes the value available to logic.

The execution policy must distinguish a periodic table from a one-shot
calendar. For a declared periodic table, each physical lane repeats its
assigned word phases every R/8 word cycles. Its execution resolver selects the
first occurrence after complete source-cone readiness and setup; this wait is
at most R serial UIs. Readiness must cover all contributing fanins, rather than
one chosen original path in isolation. A concrete occurrence, once frozen for
the event check, must meet readiness; a missed occurrence fails instead of
being silently moved. One-shot calendars do not gain periodic semantics merely
because wrapping would improve their reported result.
Serial positions use UIs, parallel words use word-clock cycles, and event
phases come from the actual clock/PHY binding. If a receiver exposes a complete
word at once, bits in that word share its visibility event; changing only
their serial bit order does not establish different logic-visible arrival
times.

The effective post-schedule hop delay is `rx_visible - ready`. The fixed PHY
component must have explicit endpoints: serializer latency, board propagation,
deserializer latency, alignment, and buffering must each be included exactly
once. A configured fixed component is not automatically a cable-only delay or
a fully characterized TX-to-RX timing arc. Do not compute
`D_pre(R) + schedule_wait`, which counts an already estimated TDM component
again. Replace that estimate with the resolved events and the separately
defined fixed component.

For a multi-hop path, the next hop's readiness is the preceding `rx_visible`
plus its routed relay logic delay. Analyze the whole event chain with common
clock phases; independently choosing favorable phases on each hop is invalid.
Original launch/capture edges, clock relationships, setup, uncertainty, and
margin still determine the terminal timing requirement. Model-exact event
timing does not turn uncharacterized PHY arcs into measured hardware timing.
If phase, fixed-delay boundaries, or receive visibility are unknown, report
the remaining bound or incomplete qualification rather than claiming exactness.

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

## Indexed result ownership

The OpenSTA producer keeps event measurements in three typed scalar columns
and original-path results in five scalar columns over the canonical metadata.
Individual dictionaries are projected only for the current consumer row.
Indexed production measurements must retain contiguous original-path bundles;
interleaving is rejected rather than building a second population of objects.

`qor_report.json` is an `emuflow.system-qor-storage/v1` selector. Its `header`
contains the bounded QoR summary; its named sibling
`qor_report.json.paths.<digest>.sqlite` owns all original-path records. The
SQLite store uses typed columns and interned metadata strings, including null
scalars and proof references for optimized-away members. Neither publication
nor a summary read expands the path population into JSON. The logical schemas
remain `emuflow.qor-report/v4` and `emuflow.system-timing/v2`. The shared
native publication core admits this result owner and the nullable v2 boundary,
logic-segment and local timing stores. Historical measured-only v1 stores
retain their own version checks.

`emuflow.io.read_json` exposes the logical document with a lazy `timing.paths`
sequence. Ordinal lookup and `timing.paths.by_path(original_id)` read only the
selected record; raw JSON tools find summary fields under `header.timing`.
Move the selector and its named sibling together. Phase 7C and full-flow
manifests retain the payload reference and its producer-computed seal.
`validate_system_qor` explicitly scans records with bounded memory to verify
the logical seal, exact source-ID coverage, counts, null dispositions, and
WNS/TNS against stored engine scalars. Ordinary summary reads do not open or
hash the path store. Consumers must keep the owner available until lazy row
reads and comparisons finish.

When every original member is eliminated, Phase 7C publishes a header-only
`global-opensta/measurements.tsv`. Terminal validation rebinds the physical
inputs and requires that file to contain no numerical rows. This preserves
explicit result validation without invoking OpenSTA or the Python composer;
there is no fabricated engine log or numerical-execution claim.

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
