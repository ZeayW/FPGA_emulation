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

Raw maximum delay and minimum operating period are separate results. A safe
frequency calculation needs the original launch/capture edge and phase
relationship, common clock scaling, exceptions and physical setup/skew/margin
budget. The current global binding does not preserve that complete contract;
its `minimum_safe_period_bound_ns` and `maximum_safe_frequency_bound_mhz` are
therefore null, with an explicit `operating_frequency_status`. In particular,
it must not report the reciprocal of raw maximum delay as a safe frequency.
Fixed-period WNS/TNS retain their independently bound check scope. Extending
them to a frequency claim requires the additional clock-budget contract.

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
one-bit-per-fabric-cycle adapter has not qualified its implementation. Native
Phase 5 word-table planning and Phase 6 logical word RTL are available. The
fixed-pin PHY binding and physical periodic-event projection are not yet
admitted. The global binder
rejects such plans rather than assigning the pre-schedule ratio arc to them.

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

Word policy v2 uses a DUT-release/startup epoch and positive load indices. The
epoch edge cannot load a newly launched value. The first phase-zero occurrence
is therefore the following table round; other phases use their first positive
indices. The corresponding RTL uses the controller interval `slot` to prepare
load edge `slot + 1`, and drains the channel before commit. Physical event
projection must reject an occurrence that would enter this drained region.

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

The shared native `word_events` kernel resolves a complete fanin DAG using
explicit architectural launch phases and measured sampling costs. It selects
the first positive occurrence in each fixed periodic table, requires every
contributing RX shadow capture to precede its next TX edge, and rejects cycles,
missing dynamic sources, nonfinite costs and unrepresentable event coordinates.
Its explicit frame bound requires RX capture before `FRAME_SLOTS - 1`, matching
the emitted RTL's drained region.
Configuration-stable cones require an explicit classification. Its result owns
only TX word indices; RX coordinates follow from the declared fixed latency.
Phase 5 uses the same integer calendar selection primitive. The physical
binding below supplies measured fanins and source clocks to this kernel;
word-mode global acceptance remains disabled until checked eliminations,
original captures and the independent global OpenSTA checks are integrated.
The native `ExactSemanticView` reads the published v4 assignment index and its
v2 semantic sidecar as one owner pair. It checks the bounded manifest identity,
part/cut identities, segment coordinates and complete per-cut source-group
coverage. Required net, segment and source-group sections are checked while
streaming them once; it retains integer coordinates and flat source ranges,
without reconstructing logical JSON or a net-name map. Both files must retain
their bound generations, and a relocated pair must retain its sibling binding.
This source-fanin view does not consume capture endpoint payloads or certify
physical timing coverage; those checks belong to their actual consumers.
The separate native validator consumes the TX support slacks in the same
canonical fanin order. It checks the selected edge and its immediately preceding
table occurrence using the change in capture deadline, as well as strict RX
capture ordering and frame drain. It does not call the projector, reuse its
readiness guard or calculate another data-path arrival. Slack tolerance is an
explicit consumer input. Missing, extra, nonfinite or failing engine records,
and an earlier occurrence that has not been independently excluded, fail
validation. The support-check visitor below supplies canonical measured fanins;
production still needs to bind the complete combined engine execution and all
original capture records.

Sampling costs are effective nominal-edge guards, distinct from raw data
arrival. Their signed clock/check adjustment shifts the OpenSTA deadline while
the data arc stays nonnegative. A favorable adjustment does not permit a relay
to reuse its RX capture edge. Subtracting such a guard from a chosen TX edge
does not, by itself, measure physical TDM waiting or characterize the fixed PHY
arc. The kernel therefore exports occurrences, not a claimed physical wait,
raw end-to-end delay or operating frequency.
A bounded [Vivado phase-reference diagnostic](../scripts/vivado/qualify_clock_reference_phase.tcl)
keeps one routed FF-to-TX endpoint pair fixed and shifts only its in-memory
primary-clock waveform. It tests whether the common-reference data and sampling
cost remain independent of nominal source phase. Its three query cases do not
modify the saved checkpoint or qualify whole-design timing.
Changing the DUT period can change architectural launch phases and selected
table rounds. Feasibility need not be monotone: a half-cycle launch at 40 ns
can use a 45 ns load and arrive at 75 ns within an 80 ns period, while a launch
at 49.5 ns misses that load, uses 85 ns and arrives at 115 ns beyond a 99 ns
period. Candidate periods therefore require fresh projection; a binary search
over assumed monotone feasibility is not a qualified frequency calculation.

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

### Phase 2 clock-budget owner

The explicit `run_opensta_path_database(..., clock_budget_policy="engine-edge-v1")`
policy publishes `emuflow.sta-path-database/v4` with `sqlite-sta-path/v2` storage.
One `clock_budgets` row references each original `paths.ordinal`; it shares the
same immutable SQLite generation and publication seal as paths and ordered nets.
The relation owns source clock/period, launch and capture edges, cycle offset,
target cycle time, multicycle adjustment, check margin, clock latency/skew and
check classification. Target clock/period remain owned by the path row.
Import checks these facts while streaming the producer output; readers stream
budgets once per path, including indexed selections, rather than per net.
This policy requires the engine's terminal completion record and matches its
clock count, output identity and emitted-row count before publication; a zero
exit after a Tcl error cannot publish a partial path population.
Missing/extra records and schema/format mixtures are rejected. Native PATRON
retains its direct indexed path projection for both storage versions.

The policy requires compact indexed EmuIR and projects combinational clock
cones in a temporary SQLite relation, leaving the source store read-only.
For clock-cone LUT1--LUT6 and LUT6_2 instances, fully defined INIT truth tables
select distinct timing models with positive, negative or non-unate arcs for
each input/output pair. Functionally unused inputs have no arc.
Direct constant inputs and indexed GND/VCC drivers are folded before polarity
is calculated; a tied-input XOR can therefore become a buffer or inverter.
Unknown or conflicting clock-LUT constants are rejected. Derived timing types
remain in the temporary SQLite relation and are streamed with source cells.
Data-only LUTs keep their scalar model; distinct clock LUT models, rather than a second
instance map or complete Python graph, supply the Liberty/Verilog adapter.
Unknown INIT or clock-cone cells without explicit arc polarity fail closed.
The manifest binds `clock_logic_policy=indexed-init-clock-sense-v1`.
This prevents an AND clock gate from being interpreted as a possible inverter;
primitive IS_C_INVERTED alone cannot establish the source design's phases.

For this policy, `fixed_delay_ns` is the engine arrival relative to the source
launch edge after removing the engine cycle offset. It includes any explicit
source-clock latency; it is not a routed physical datapath measurement. The
engine's check margin is a reference-model fact, not a substitute for routed
setup/skew evidence. Edge windows are computed from engine cycle accounting,
not inferred from slack or a register's name. Latch, absolute-delay and missing
clock-edge cases fail explicitly rather than receiving an assumed full cycle.

The named `legacy-absolute-arrival-v1` policy remains the default during global
budget integration. Historical indexed v3/v1 data remains readable without
inventing missing clock facts. Clock-budget rows cannot be repacked through the
legacy key-free JSON row writer. Native word Phase 7B executes independently;
word-mode global timing and safe-frequency reporting remain unqualified until
their consumers use these budgets and physical margins.

The separate indexed producer qualification uses real positive and negative
`FDRE` models behind an INIT-defined AND clock gate and two cancelling
inversions (a tied-input XOR and an inverter), a pinned architecture
timing database and explicit output delay:

```bash
PYTHONPATH=src python3 scripts/validation/qualify_opensta_phase2_clock_budget.py \
  --sta sta --architecture-timing-db path/to/xilinx-preplacement-timing.json \
  --out build/phase2-clock-budget
```

This is synthetic Phase 2/API evidence, not whole-design or physical timing.

The standalone engine exposes its source clock edge/offset, target cycle time
and multicycle adjustment through read-only PathEnd methods. The separate
`scripts/validation/qualify_opensta_clock_budget.py --sta sta --out build/clock-budget`
checks full-cycle, half-cycle launches and captures, multicycle, rational-period
cycle offsets and output-delay paths in the
real engine. Its source-relative arrivals remove the launch phase and engine
cycle offset explicitly. This is a synthetic API check; it does not supply
physical margins or qualify full-flow operating-frequency fields.
The separate `scripts/validation/qualify_opensta_launch_clocks.py --sta sta
--out build/launch-clocks` reads every clock/data launch tag at requested
architectural pins, including unselected fanins and shared macro outputs with
multiple source clocks. It requires declared clock identities, preserves
distinct phases, and rejects unclocked or combinational-through pins. The
read-only Path accessors use existing engine tags; no timing algorithm changes.
This is bounded synthetic API qualification, separate from the production
canonical launch-fact owner and full original-path coverage.
The streamed `export_launch_clock_edges.tcl` consumer reads ordered source
coordinates and queries each exact original pin through that API. It shares
`design_context.tcl` with Phase 2 path extraction, so original primary clocks
and top-level I/O delays are applied once using the same setup. It updates
timing once and exports all distinct clock/period/phase tags per requested
source, with explicit source and fact completion counts. Missing, repeated or
unordered source coordinates, noncanonical names, combinational through-pins
and unclocked sources fail. This interchange is not yet the canonical
source-bound launch-fact store or a whole-design timing certificate.

`emuflow-native phase7 build-launch-clocks` consumes current clock-aware logic
and local-path timing stores together with an already source-sealed OpenSTA
model. The bounded Python control bridge is
`source_launch_clocks.build_source_launch_clock_store`; it passes only file
identities, parent seals and tool options. C++ scans implemented complete-contract
launch queries and original local starts, resolves logical Vivado names against
original driver endpoints, deduplicates original `(cell, port, bit)` coordinates,
runs the streamed engine query and atomically publishes
`emuflow.source-launch-clocks/v1`. Optimized physical aliases are not original
endpoint identities. Top-level inputs must be connected original drivers;
cell names must match the exact escaped-name recipe, and bus pins use the
original connected width. Unsupported or ambiguous names fail.

This store owns each requested original coordinate once, declared and engine
clock periods once per clock, and every distinct source clock/phase fact. Query
names and TSV files are invocation scratch. Physical source, route, schedule,
semantic and timing-policy bindings must agree. Existing emitter/probe hashes
identify the model, engine and script inputs; retained file generations must
remain unchanged through tool execution and publication.
The control bridge requires the parents' original validated generation bindings;
taking a fresh snapshot cannot renew an older content seal. Missing, duplicate,
unordered, nonfinite or undeclared engine facts, inconsistent periods and
missing terminal counts cannot replace an existing owner. Cancellation reaps
the engine before retiring its scratch.

`SourceLaunchClockView` requires the original source seal and explicit target
periods. It scales each engine phase as `phase / engine_period * target_period`;
using the engine period preserves exact half-cycle ratios despite its floating
time representation. It does not invent a zero launch for a missing clock.
Its typed facts retain source and clock coordinates within the single launch
owner, with engine period, engine phase and the derived implementation phase.
A fact from another owner fails even when its bytes and coordinates agree.
The physical consumer also checks all six original source, path-population,
route, schedule, semantic and I/O parent seals. Uniform implementation-phase
scaling does not establish the waveform of an external paused runtime clock.

The same launch invocation exports a complete raw engine-period catalog for
all declared primary clocks, including roots with no requested launch tags.
These periods occupy the existing per-clock owner rows; the catalog TSV is
scratch. Missing or inconsistent periods cannot be replaced by the displayed
clock period. `SourceLaunchClockView::ClockReferences` exposes this bounded
catalog to original-path consumers.

`OriginalTimingView` streams every original member and its clock budget from
the paired, sealed v4 selector and v2 SQLite owner, without reading ordered
nets or building a Python path graph. It requires indexed INIT-based clock
sense, unique original identities, dense path coordinates, supported check
classes and exact budget coverage. Its identities remain owned by that input
generation. `ScaleOriginalWindow` divides the engine edge window by the raw
capture-clock period before applying the explicit implementation target; it
also requires consistent raw source periods and uniform root scaling. Using
the rounded displayed period would perturb exact full/half-cycle word
boundaries. No epsilon snap substitutes for the canonical engine denominator.

`BindOriginalCaptureCheck` keeps the routed data arc `D` and shifts the
original capture deadline by `E-D`, where `E` is the routed sampling cost.
The reference model's setup/check margin remains an original fact and is not
charged a second time. Signed physical clock adjustments remain signed.
These are native binding APIs; they do not yet supply checked optimization
elimination, complete physical capture coverage or a global frequency result.

`BindPhysicalWordModel` is the native indexed join for measured fanins. It
consumes the paired assignment/semantic owner, current ScheduleStore, launch
owner and each partition's already validated logic-cost, boundary-identity and
boundary-cost owners. It checks dense transfer coordinates, route roots,
unique arrivals, word ratios/phases, fixed PHY latency, frame drain, endpoint
class and complete per-cut semantic source groups. The supplied word clock and
fixed latency must come from the caller's validated Phase 6 PHY plan; they are
independent of its explicit DUT implementation-clock periods. Physical input
ownership must already be bound to the same validated Phase 7 invocation;
matching design names alone do not certify their provenance.

Only complete-contract launch/transition rows supply readiness, including
architectural sources absent from selected original-path queries. Each launch
expands all of its original clock/phase facts. Root indices are canonical within
that clock owner, independent of physical worker enumeration. Each transition
must start at its corresponding RX shadow. A forwarded relay must refer to that
same shadow, and uses its TX boundary data delay and sampling cost once. The RX
word-to-shadow cost must satisfy the word-stage timing check but is already
inside the fixed PHY arc. It is never added again to a relay's readiness.
Configuration stability requires the semantic producer's explicit constant
classification; missing queries or clock facts cannot create it. Required
optimized-away rows currently fail until their checked native disposition
owner is connected. Such a rejection is not a numerical zero or a completed
coverage record.

The join retains native coordinates and references to canonical physical cost
rows, not another Python circuit graph or persistent derived timing database.
It uses at most three attachments per partition connection, independent of
the number of FPGAs, and preserves all input generation leases. Its derived
`VisitPhysicalWordSupportChecks` visitor emits launch time, nonnegative data
arc and deadline shifted by `sampling_cost - data_delay` in canonical
transfer/fanin order. Those support checks belong alongside original capture
checks in one OpenSTA invocation. Its engine slacks feed the independent
first-occurrence validator. The bounded qualification includes a late table
occurrence whose engine slacks pass but whose first-occurrence check fails;
synthetic observation checks are not original DUT capture coverage.
The join and visitor do not supply the original-path clock budgets, prove
physical delay exactness, derive the runtime pulse waveform, or release global
WNS/TNS or an operating-frequency result on their own.

The shared `clock_budget` reader preserves this edge window under uniform
source/capture clock scaling, rejects independent rescaling and unsupported
latch/absolute-delay checks, and keeps setup and physical margins separate.

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
