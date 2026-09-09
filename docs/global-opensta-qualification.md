# Global OpenSTA qualification

## Objective and acceptance boundary

Replace bespoke global timing arithmetic with an independently validated STA
model of the **existing** frozen transport protocol. Do not change partitioning,
system routing, TDM assignment or transport RTL to make the model easier.

Physical flows default to `--global-timing-engine opensta`. OpenSTA owns the
numeric results and does not invoke the Python timing composer. It does not claim
hardware signoff. The acceptance gates are:

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

Production builds the canonical report directly from raw binding metadata and
OpenSTA scalars. The former `adopt_opensta_results`/Python comparison is used
only for explicit qualification and historical artifact validation. Standalone
terminal validation reconstructs raw binding and verifies saved engine scalars;
it does not call the old system-timing composer or run STA again. Missing
coverage, incomplete physical evidence and missed transport events still fail
or remain incomplete. Engine provenance comes from the existing startup banner.

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
An explicit BoardLinkTimingDB takes precedence. In its absence, the declared
BoardDB cycle latency is materialized as the existing model-only directed link
database, matching the ordinary academic flow without inventing measurements.
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

## Real-design qualification

Koios DLA medium / EDA2023 case6 completed physical Phase 1--7 and terminal
independent validation: 379,357 instances, three used FPGAs, ten naturally
selected combinational cut nets, 6,239 scheduled hops and zero equivalence
mismatches. Physical execution used seed 1 and baseline Phase 6. No assignments
were forced to create cuts. The run used e16dd9cc for physical implementation
and 299e3e8f for repaired Phase 7C model binding/constraint loading; this is not
a claim that the later source revision reran synthesis or physical placement.
The authority-report projection and terminal validator subsequently passed at
`e90698cf`, without repeating physical implementation. The final regression
suite passed 962 tests with three optional skips; source audit and diff checks
also passed. Both the canonical path values and compact report aliases now
come from OpenSTA. That historical dual-execution terminal reconstruction checked the saved engine scalars
against fresh physical binding and the independent Python composer, without
launching another STA process. Only a compact terminal summary is retained;
the completed run's intermediate models and physical work directories were removed.
The later standalone change removes that Python-composer dependency. Its new
regressions forbid calls to both the old composer and the comparison projector;
the historical DLA gate does not establish fresh standalone DLA runtime numbers.

OpenSTA 2.6.0 checked 403,778 observations covering all 195,532 original paths,
with zero transport-event failures. Target-clock original-path WNS/TNS were
-235.0976160415 / -388,770.157286 ns (8,789 negative paths); runtime-clock
WNS/TNS were 2,237,914.595753 / 0 ns. Thus the target does **not** close timing.
This acceptance proves analysis consistency, not a QoR improvement.

The largest scalar difference was 0.344801 ns in long-frame arithmetic, within
the per-value float32 tolerance; short target/event observations retain their
own tighter tolerance. OpenTimer 2.1 independently checked 64 real-design paths
(32 crossing and 32 local; 224 observations), with zero event failures and
maximum arrival difference 0.000003235 ns from the raw binding.

The result includes 194,849 endpoint-exact paths and 683 cone-bound paths, with
no fallback or discontinuous compressed paths. Routed staging-chain delays
remain upper bounds, and all board hops use the declared model-only latency;
none is measured board-link timing. These limitations preclude hardware signoff.
