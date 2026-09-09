# Global OpenSTA qualification

## Objective and acceptance boundary

Replace bespoke global timing arithmetic with an independently validated STA
model of the **existing** frozen transport protocol. Do not change partitioning,
system routing, TDM assignment or transport RTL to make the model easier.

The initial implementation is opt-in and qualification-only. It must not claim
signoff or become the default authority before the following gates pass.

1. Export measured logic/interface/link arcs and fixed-event constraints from
   their canonical sources; do not export Python-composed delay/slack as arcs.
2. Check small hand-computed cases with real OpenSTA: local paths, slot zero,
   multi-hop relays, consecutive combination cuts, uncertainty, late TX with
   otherwise legal commit, missing measurements and distinct clock periods.
3. Compare arrival/required/slack for **every original member**, not just WNS.
   Exercise large runtime frames and quantify floating-point precision.
4. Integrate the check with a complete physical flow, preserving whole-original
   path coverage, delay-bound qualifications and a single physical seed.
5. Only after that gate, promote OpenSTA values to authority, retain the Python
   composer as cross-check, and fail discrepancies. An independent OpenTimer
   spot-check remains a separate gate, not evidence supplied by OpenSTA itself.

The tested `adopt_opensta_results` projection updates the canonical path values
and every target/runtime scalar alias together. It is not yet enabled by the
flow: the real-design gate remains pending. It preserves incomplete/failing
upstream status and fails a missed transport event, even with legal final
latency. Engine provenance comes from the existing process startup banner.

## Why two types of observation are necessary

An ordinary STA path stops at each transport register. Its register-to-register
slack is **not** a DUT launch-to-capture path's macro-cycle latency. Accordingly,
the timing abstraction has both event readiness checks and a terminal observation
per original TimingPathDB member. Every upstream event must pass before the
terminal observation represents current-frame data.

For a frozen edge at time `t`, fixed-edge cutpoints lower to an SDC input launch
time and an explicit output deadline relative to one epoch. This is an
alternative to enumerating periodic generated clocks: a missed edge cannot
implicitly wrap into the next frame. No estimated TDM wait is a combinational
arc. The physical arcs between cutpoints remain a Verilog/Liberty chain whose
delay is propagated by OpenSTA.

The observation view is an analysis abstraction, not new transport hardware.
It does not claim to validate hold, metastability, clock-tree skew or silicon
PVT corners without the necessary physical input data.

## Metric identity

Preserve original-path WNS and original-path TNS as explicitly named project
metrics. The latter sums negative slack once per original TimingPathDB member;
it is not the conventional worst-slack-per-register-endpoint TNS. TX/relay/commit
check slacks must not be added to that TNS. Coverage means the declared source
TimingPathDB population, not a proof that source extraction enumerated every
possible sensitizable path in the circuit.

## Storage and execution

One OpenSTA process queries all observations in a batch. Numerical model
construction is linear in bound paths/arcs; no optimizer replay, artifact DAG,
duplicated per-path JSON or repeated hashing is introduced. Verilog, Liberty,
SDC and measurement TSV are active-run scratch. Only the compact terminal
qualification report is retained after acceptance. The measurement reader checks each
event's arrival, deadline and slack against that binding in a single linear
pass, including readiness observations outside the original-path TNS population.
A corrupt positive event slack cannot conceal a missed deadline. A scalar Liberty arc carries
the measured delay directly; a duplicate SDF carrying the same numbers is
deliberately unnecessary.

## Third-engine spot-check

`scripts/opensta/opentimer_event_check.cpp` is an offline driver linked against
upstream OpenTimer. It reads the same exported Liberty, Verilog and SDC, checks
both transition polarities, and writes endpoint arrival/required/slack values.
Compile it with C++17, the OpenTimer include root and `libOpenTimer.a`; set
`EMUFLOW_TEST_OPENTIMER` to the resulting executable when running
`tests/test_global_sta.py`. This is not a production dependency or a second
large-design analysis pass. The exporter uses non-ANSI port declarations and
an explicit zero input slew for portable constant-arc analysis.

The tests include a deliberately missed TX with a legal final commit and a
mixed 256-path arc-chain population. Passing these establishes model-level
third-engine evidence only; it does not replace the real-design physical gate.
