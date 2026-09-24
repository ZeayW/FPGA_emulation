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
| OpenPARF native (`feature/phase7-openparf-native`) | Pinned-source probe; ordinary LUT1–LUT6/FD* atomization; singleton DSP48E2/RAMB36E2/URAM288 support; one real compiled GP → hard-resource MCF/direct-LG → ISM run; independently validated placement certificate | carry/MUX/LUT6_2; RAMB18 half-sites; cascade, clock/half-column/SLR constraints; standard packed/placement conversion; complete RapidWright export and routed Phase 7 | Primary implementation route; native runtime is proven for the audited LUT/FF plus independent hard-resource subset, and remains fail-closed elsewhere |
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

Large DLA work remains gated by the small real-runtime and resource fixtures.
There is no hidden fallback to the old greedy legalizer.

The real-runtime gate has now passed twice: first for a connected 64-LUT/64-FF
fixture, then for the same connected ring with one DSP48E2, one RAMB36E2, and
one URAM288.  The mixed run covered 131 atoms and 132 non-degenerate nets in a
single native OpenPARF invocation; every atom and final site/BEL assignment was
independently re-imported.  This proves the compiled placement path for that
bounded subset.  It does not yet prove RapidWright routing, global OpenSTA
timing, cascades, clock legality, or DLA support.
