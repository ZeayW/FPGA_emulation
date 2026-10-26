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

The production binder streams bounded batches into one invocation-owned
`emuflow.global-event-checks/v1` store. Path metadata, path/event identities and
packed per-check numbers have one owner. Arc vectors remain BLOBs rather than
one SQL row per arc. A separate `emuflow.global-event-index/v1` contains only
path ordering and representative spans; it never copies numerical payloads.
Integer identities are scoped to the primary artifact. The index binds its
original path and file generation.

The shared core independently validates schema, coverage, finite/nonnegative
launch and arc values, unique identities, bundle continuity, and identical
target/runtime physical chains. Numerical compression compares the complete
ordered bundle after a candidate hash match; distinct TX/commit bundles cannot
merge. Signed zeros retain their source representation and compare numerically.
Native index/export failures stop the selected path without a Python fallback.

OpenSTA input publication uses a fresh model directory plus atomic `model.json`.
The manifest records each generated file's name, size, hash and file generation;
hashes are computed while writing. Consumers verify the recorded generations
before and after engine execution without rehashing the model. The production
engine uses the shared process supervisor. `opensta.log` remains in the Global
STA output directory. Engine TSV is streamed once into the canonical numerical
owner and retired after successful publication. No expanded original-check TSV
is produced on the typed production path.
Temporary event owners close after Phase 7C publication or failure. Their
retirement is ordered after SQLite closure, including attached dependent readers;
it does not rely on an independent directory weak finalizer during cyclic GC. Public
small `EventCheck` inputs retain their direct text exporter for independent
reference checks. They are not a fallback for indexed production inputs.

`measurements.json` selects one `emuflow.global-measurements/v1` SQLite owner.
It stores exactly three engine scalars per representative check as packed IEEE
binary64 records, preserving their original precision. Original path/event
identities and absolute launch offsets come from the event owner and index.
The manifest binds bytes, file SHA-256, logical measurement SHA-256 and the
canonical event namespace. A reconstructed event owner may join saved scalars
only when that namespace agrees; unrelated integer ordinals cannot alias.

Native import rejects malformed, duplicate, missing and nonfinite endpoints.
Native reduction independently checks complete bundle mappings and restores
every original member before counting target/runtime observations, TX/commit
failures and WNS/TNS. TNS uses a bounded faithful partial-sum accumulator in
canonical original-path order. Optional raw-arc equation checks use the existing
absolute/relative tolerance and cover transport checks as well as observations.
The live production path selects OpenSTA as numerical authority; explicit saved
result validation checks all raw equations and never relaunches the engine.

The shared core writes final ordered path records directly into the existing
`qor-report-store-v1` typed columns. No per-check scalar or final per-path numeric
Python arrays are rebuilt. SQLite REAL signed zero is normalized consistently
before logical sealing. The installed QoR reader selects native ordered-record
validation for the declared native implementation; failures do not fall back to
Python. Terminal comparison uses independently validated full logical identities
and bounded headers rather than expanding two whole QoR JSON documents.
Historical expanded-TSV production results require their matching Git revision.

Current typed RouteStore/ScheduleStore and physical evidence select
`global-sta bind-events`. The native binder consumes boundary timing/identity,
logic-segment timing and optional local-path timing stores directly. It reuses
the Phase 5 scheduled-path visitor for route selection and concrete TX events;
only one original-member segment chain is active. Endpoint lookups remain in
their FPGA-scoped owners, and a bounded event cache avoids an H-sized Python
endpoint dictionary. Logic groups merge in canonical path/member/cut order;
missing, extra or globally duplicated segments fail before publication.

The binder emits the existing event primary directly, then the independent
native event index validates the complete output. It preserves every original
member, TX/commit checks, uncertainty placement, per-clock runtime periods,
registered-boundary exemptions, ratio domains and separate physical link models.
Physical lane remapping retains the schedule's logical lane identity. Local-path
sources must agree, and terminal metadata checks the original count and path-ID
seal. Unknown/mixed typed representations fail; native failure never invokes the
inline reference binder. Inline small correctness fixtures remain reference
inputs; mixed storage-oracle tests invoke their reference producer explicitly.

The same core validates current boundary identity/timing, logic-segment timing
and local-path timing stores through `physical validate-evidence`. It checks the
installed exact schema and effective columns/indexes, complete metadata, ordered
records, row counts, register/pin ownership and chain order, object/measurement
qualifications, source seals and canonical UTF-8 logical hashes. Validation does
not execute SQLite integrity/quick checks. One active record owns its register
or pin members; there is no Python full-record reconstruction. Logical/count
summaries return through a bounded source-generation request and atomic result.
A failed selected native validator stops the reader without a Python fallback.
The retired Python full-store validator and schema copies are removed.

Maximum local/segment delays and measurement coverage reuse the validated
summary. A summary query does not rescan a complete path/segment table; live
source generations remain checked. Writers retain their existing logical schema
and seals, and already validated publications do not run the new reader again.

This closes the current typed physical evidence reader/validator, physical-to-event
producer, event storage, numerical import/reduction and model/QoR export boundary.
Other physical producers and phase adapters still require migration. This does
not establish W6/W7 completion or full physical qualification.

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


The Phase 5 indexed timing descriptor binds RouteStore path/member identities and
ScheduleStore events without duplicating a path tree. Its native timing summary
preserves the pre-placement diagnostic population and event/ratio model; those
statistics are not global post-route timing. Phase 7C still requires the complete
original-member physical coverage and numerical OpenSTA checks specified above.
