# Phase 7 placer route status

This document records implementation evidence for the parallel placer study.
It is not a declaration that any candidate is a production backend.  The
existing Phase 7 default remains unchanged until one route passes the complete
physical and system-timing promotion gate.

## Shared contract

All candidates emit `emuflow.xilinx-placer-capability/v1`.  Each required
stage, primitive, and constraint is classified as one of:

- `native_supported`
- `adapter_required`
- `core_missing`
- `unverified`

An adapter-dependent capability qualifies only after its adapter validation is
`pass`.  Capability qualification only admits a route to physical QoR testing;
it never selects a default.  `emuflow.xilinx-placer-comparison/v1` therefore
leaves default selection deferred until routed Phase 7 legality, runtime, and
global OpenSTA WNS/TNS are available on identical inputs and seed.

## Candidate branches

| Route | Implemented evidence | Remaining production blockers | Decision |
|---|---|---|---|
| OpenPARF native (`feature/phase7-openparf-native-macros`) | A1 atomic route passed real XCVU19P placement → RWRoute → routed-delay → standalone OpenSTA 3.1; A2 adds typed CARRY8 extraction, full-slice eight-LUT legalization, unplaced macro export, and independent BEL/CARRY_NEXT validation | Modified C++ runtime build/execution, macro bridge, RWRoute/OpenSTA gate; then MUX/RAMB18/cascade and clock/half-column/SLR | A1 passed end to end; A2 source/unit gate passes but remains unqualified until the real compiled and physical gates pass |
| DREAMPlaceFPGA (`feature/phase7-dreamplacefpga`) | Pinned-source probe; Yosys mapped JSON to official FPGA Interchange logical netlist; physical netlist to validated placement certificate; real Cap'n Proto schema roundtrip | Upstream detailed placement is absent; several UltraScale+ primitives, cascade, clock-region, and multi-SLR constraints are missing | Research candidate only; not eligible for the production route |
| AMF-Placer (`feature/phase7-amf-placer`) | Pinned-source probe; bounded design/device/result adapters; LUT/FF/CARRY8 and explicit constant-normalization fixture; independent exact placement revalidation | Public optimization-core runner/config integration; MUXF9/URAM; XCVU19P clock legality; multi-SLR support; full RapidWright export and routed Phase 7 | Secondary candidate; adapter roundtrip is not an AMF optimization result |

The integration branch is `feature/phase7-placer-integration`.  It contains
the shared contract, deterministic comparison gate, and the validated adapter
work from all three branches.  No branch adds a public CLI selector or changes
the default provider yet.

## Promotion sequence

1. Run each candidate's smallest supported fixture against its real compiled
   runtime.  Fake or contract-only tests do not satisfy this gate.
2. Run a resource-covering fixture and reject any silent primitive lowering,
   lost relative constraint, overlap, missing cell, or unsupported clock/SLR
   condition.
3. Export through RapidWright, complete all supported routing connections, and
   independently revalidate site/BEL/pin ownership.
4. Run standalone OpenSTA on bound routed delays and require complete path
   coverage and valid global WNS/TNS.
5. Run one fixed physical seed of Koios DLA medium with identical Phase 1–6,
   router, resource limits, and timing model for every surviving route.
6. Select a default only from complete end-to-end evidence.  A route with
   better intermediate HPWL but incomplete physical or timing evidence cannot
   be promoted.

## Updated implementation plan

OpenPARF remains the primary route.  The parallel branches are controlled
alternatives and independent implementation probes; they do not replace the
OpenPARF work merely because an interchange adapter can be made to roundtrip.

### Route A1: OpenPARF atomic qualification

This route qualifies the real compiled engine and the complete downstream
handoff using ordinary LUT/FF atoms plus singleton DSP48E2, RAMB36E2, and
URAM288 atoms.  It must pass all of the following without a placement fallback:

1. connected two-dimensional device-region and density admission;
2. native global placement, resource legalization, and detailed placement;
3. exact placement-certificate re-import;
4. RapidWright routing with zero missing logical sinks;
5. routed-delay extraction and independent OpenSTA endpoint timing.

Passing A1 proves the engine and handoff, but cannot by itself qualify a DLA
backend because it intentionally excludes physical macros and cascade chains.

### Route A2: OpenPARF native macro production route

The production route extends the pinned OpenPARF core rather than adding a
post-placement site search.  Work is ordered by the first unsupported DLA
primitive or constraint:

1. represent one `CARRY8 + 8xLUT6_2` group as an indivisible full-slice unit,
   including ordered inter-site carry-chain adjacency;
2. add MUXF7/MUXF8/MUXF9 relative placement and RAMB18 pair/mode constraints;
3. add DSP48E2, BRAM, and URAM cascade ordering from typed native adjacency;
4. consume authoritative clock-region and SLR membership/capacity during
   global placement and legalization;
5. retain fail-closed behavior for half-column clock capacity until an
   authoritative device source is available;
6. preserve every macro through native detailed placement, then independently
   verify exact BEL roles, offsets, adjacency, coverage, and non-overlap.

The bridge may translate and verify OpenPARF's answer, but it may not choose a
different legal site, repack a macro, or invoke the retired greedy legalizer.

### Route B: DREAMPlaceFPGA research control

The DREAMPlaceFPGA branch is retained to measure its official Interchange
front end and analytical placement behavior.  Because upstream does not
provide the required UltraScale+ detailed placement and macro/clock/SLR
contracts, this route remains ineligible for default promotion unless those
missing core capabilities are implemented and independently verified.

### Route C: AMF-Placer secondary candidate

The AMF branch first establishes typed UltraScale+ physical constraints and a
bounded adapter.  It becomes a real candidate only after the public AMF
optimization core, configuration, and result path run end to end; an adapter
roundtrip alone is not placement evidence.  It is subject to the same
RapidWright routing and OpenSTA gates as Route A.

### Final comparison

Only routes surviving their small real-runtime and resource/macro fixtures are
run on Koios DLA medium.  The comparison freezes Phase 1--6, architecture,
resource limits, timing model, router, and one physical seed.  The decision is
based on complete routed legality, runtime, and global OpenSTA WNS/TNS; HPWL,
density, or legalization cost are diagnostic metrics rather than promotion
criteria.

Large DLA work remains gated by the A2 macro/resource fixtures.  There is no
hidden fallback to the old greedy legalizer.

The real-runtime gate has now passed twice: first for a connected 64-LUT/64-FF
fixture, then for the same connected ring with one DSP48E2, one RAMB36E2, and
one URAM288.  The mixed run covered 131 atoms and 132 non-degenerate nets in a
single native OpenPARF invocation; every atom and final site/BEL assignment was
independently re-imported.  This proves the compiled placement path for that
bounded subset.  It does not prove cascades, clock legality, or DLA support.

The first real-XCVU19P routing gate exposed a test-region error before routing:
the selected 16 slice sites formed a `1x16` collinear Bookshelf domain.  Native
OpenPARF correctly could not produce a finite two-dimensional density solution
(`HPWL=-INF`) and direct legalization rejected every atom.  The adapter now
rejects collinear, disconnected, density-saturated, or non-finite site regions
before runtime, and records the region and density proof in its manifest.  That
gate failure is not counted as RapidWright or QoR evidence.  The corrected
connected two-dimensional gate is recorded below.

The next connected `4x4` region passed the two-dimensional geometry check but
was correctly rejected by the Route A reservation gate: the fixture occupied
all 16 slice sites while the route contract permits at most 75% local site
utilization.  The gate input is being enlarged; the utilization requirement is
not weakened to make the test pass.

### A1 atomic qualification result

The corrected real-XCVU19P atomic fixture passed the complete A1 sequence
without a placement, packing, legalization, or routing fallback:

- native OpenPARF placed 128 atoms into 11 occupied sites;
- RapidWright routed 22 nets and 70 sinks using 185 PIPs, with zero missing
  logical sinks;
- independent routed-timing validation covered all 21 inter-site logical
  endpoints and observed a maximum route delay of 0.4403999938964844 ns;
- standalone upstream OpenSTA 3.1.0 at revision `051222e4ec` emitted all 64
  queried endpoint paths;
- independent OpenSTA validation reported 64 timed endpoints, zero failing
  endpoints, WNS +39.091599 ns, and TNS 0 ns;
- the compact OpenSTA summary SHA-256 is
  `d3c2c94f8f194e42beb5741a1ae2177bdfac04a280e585e3bf2897d201f6ad3c`.

The older OpenSTA 2.6.0 runtime crashed after a bounded path query because of
an upstream collection/path ownership defect.  The same frozen export passed
on official OpenSTA 3.1.0, and the EmuFlow producer plus its independent
validator then passed on that engine.  This is an engine qualification result,
not an EmuFlow placement or timing-algorithm failure.  A1 remains an atomic
handoff qualification only; it does not qualify the A2 macro route or Koios
DLA medium.

Macro support will use a compact physical-macro contract derived from mapped
connectivity.  It must preserve exact BEL roles, same-site membership, relative
site offsets, RAMB18 mode, and cascade ordering.  OpenPARF must place those
groups as indivisible units; the bridge may validate and materialize the result
but may not repack macros or search for legal sites after placement.

The isolated A2 branch now extends the native chain extractor and legalizer for
`CARRY8 + 8xLUT6_2`, emits an empty placement seed, and independently checks
same-site BEL roles plus typed `CARRY_NEXT` adjacency.  Its focused Python and
source-audit gate passes.  This is not runtime evidence: the route remains
fail-closed until the modified C++ operators execute and their answer passes
the RapidWright routing and OpenSTA gates.  It may not manufacture a pass by
preplacing the macro or by calling the retired greedy legalizer.

Native device constraints are split by evidence.  RapidWright can export
typed carry/DSP/BRAM/URAM adjacency only when the primitive-specific BEL/site
endpoints share the same canonical native `Node`; coordinate proximity is not
accepted.  Clock-region and SLR membership/capacity are available.  XCVU19P
half-column membership and maximum unique-clock capacity are not exposed by
the pinned RapidWright API, so OpenPARF's benchmark-specific 12/24-clock
constants are not imported and that capability stays fail-closed.
