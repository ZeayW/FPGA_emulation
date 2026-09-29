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
| OpenPARF native (`feature/phase7-openparf-native-hardblocks`) | A1 atomic route passed real XCVU19P placement → RWRoute → routed-delay → standalone OpenSTA 3.1; bounded MUXF7/8/9, CARRY8, DSP48E2, RAMB18E2/RAMB36E2, and URAM288 fixtures pass exact placement validation, and the resource-covering hard-block fixtures pass the complete physical chain; source-sealed real-device facts cover slice macros, split-BRAM modes, CARRY, DSP, BRAM, and URAM adjacency; renewed Koios DLA medium native placement and independent certification pass on both partitions. Provider v3 pins the upstream ISPD `stop_overflow=0.10` together with the 0.15 adjustment threshold so native RUDY and pin-density inflation execute. Provider v4 replaces the one-step native termination decision with a consecutive feasible settling window, HPWL patience, restoration of the best feasible iterate, and a compact fail-closed convergence certificate. Fresh v4 placements completed at 8.8232553M and 4.1540576M legal HPWL. The compiled 1x2-SLR gate now passes native SLL placement and independent 128-atom certification | Terminal RWRoute on both DLA partitions, per-partition OpenSTA, and global OpenSTA evidence; authoritative clock and half-column limits remain fail-closed | Primary route and only production-eligible candidate; not yet the default until terminal DLA evidence passes |
| DREAMPlaceFPGA (`feature/phase7-dreamplacefpga-native`) | Pinned-source and compiled-runtime gate; mapped/packed/ArchitectureDB to official FPGA Interchange logical netlist; real Cap'n Proto LUT/FF/DSP48E2/RAMB36E2 roundtrip; `.phys` to a packed-cluster-preserving candidate certificate; sealed fail-closed RapidWright boundary | The official supported PyTorch 1.6--1.8 runtime has not completed a native fixture in the current environment; upstream detailed placement is absent; several UltraScale+ primitives, cascade, clock-region, and multi-SLR constraints are missing | Research candidate only; not eligible for the production route |
| AMF-Placer (`feature/phase7-amf-native`) | Pinned-source probe; bounded design/device/result adapters; sealed executable manifest; isolated native subprocess/config boundary; LUT/FF/CARRY8 constant-normalization fixture; direct placement certificate and independent validator; explicit test-double evidence separation | Real patched public optimizer fixture run; MUXF9/URAM; cascade, XCVU19P clock legality and multi-SLR support; routed Phase 7 | Secondary candidate; implemented runner is not a native pass until the actual public binary passes it |

The integration/selection branch is `feature/phase7-placer-selection`.  It contains
the shared contract, deterministic comparison gate, and the validated adapter
work from all three branches.  No branch adds a public CLI selector or changes
the default provider yet.

Large atomic placement assignments now live in one independently checked,
byte-sealed SQLite sidecar.  Its compact JSON certificate contains identity,
source seals, summary, convergence metrics, row counts, byte count, and
SHA-256; terminal per-FPGA reports do not embed another copy of the cluster
payload.  The bridge reads the table once in cluster order and indexes sites
once, replacing both the 60--65 MiB certificate JSON and the previous
quadratic site lookup.  Historical inline certificates remain readable for
audit.  This storage change is common to the selection branch and does not
weaken the promotion gate.

The route-aware-v3 qualification produced one terminal DLA-medium partition:
first-iteration overlap fell from 840,507 to 215,895 and first-iteration
runtime from 5,366.75 seconds to 69.25 seconds; RWRoute reached zero PIP
overlap in 462.64 seconds.  Its sibling began with 189,553 overlaps, fell to
3,538 after thirteen roughly 23--26 minute iterations, and then terminated
without a route artifact after about five hours.  Postmortem evidence showed
that the failing partition's native placement stopped during a strong
post-inflation HPWL oscillation and legalized at 8.936M HPWL versus 4.066M for
the routed sibling.  Provider v4 addresses that general termination-policy
defect.  Fresh placement passed on both partitions: the 223,389-atom partition
stopped at iteration 1,699 and legalized to 8.8232553M HPWL; the 240,377-atom
partition stopped at iteration 1,676 and legalized to 4.1540576M HPWL.  These
are placement diagnostics, not default-promotion evidence; both routes,
per-partition OpenSTA, and global OpenSTA remain required.  The downstream
v2 implementation no longer materializes the near-gigabyte route proof as one
JSON tree.  RapidWright writes a small sealed manifest plus deterministic net
and compact PIP JSONL streams; validation reconstructs one net at a time with
bounded-memory global PIP ownership, while timing binding reads only the net
stream.  Routed endpoint timing is likewise a small manifest plus one sealed
JSONL stream, and validation hands each checked endpoint directly to OpenSTA
staging in the same pass.  Legacy v1 artifacts remain readable, but new
production artifacts use the streaming v2 schemas.

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
The complete stage-by-stage refactor and promotion gates are specified in
[the native placer refactor plan](PHASE7_PLACER_REFACTOR_PLAN.md).

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
2. add MUXF7/MUXF8/MUXF9 relative placement, then source-sealed RAMB18
   upper/lower/whole tile groups and mutually exclusive mode constraints;
3. add DSP48E2, BRAM, and URAM cascade ordering from typed native adjacency,
   with a family-specific proof contract where the device database exposes a
   hard-block-internal cascade differently from ordinary routing;
4. consume authoritative clock-region and SLR membership/capacity during
   global placement and legalization;
5. retain fail-closed behavior for half-column clock capacity until an
   authoritative device source is available;
6. preserve every macro through native detailed placement, then independently
   verify exact BEL roles, offsets, adjacency, coverage, and non-overlap.

The bridge may translate and verify OpenPARF's answer, but it may not choose a
different legal site, repack a macro, or invoke the retired greedy legalizer.

The RAMB18 native-device audit found that ArchitectureDB currently retains one
`RAMB181` anchor per physical BRAM tile, while RapidWright exposes three
overlapping native views: upper RAMB18, lower RAMB18, and whole RAMB36.  The
names and coordinates are not a sufficient binding between those views.
Accordingly RAMB18 is supported only through the source-sealed tile-group
adapter: the production path exports exact native site/BEL identities and
OpenPARF chooses conflict-free half/full claims. `Y-1`, `Y/2`, name rewrites,
and forced instance pairing are forbidden. The placement certificate carries
the selected tile/anchor/role/claim identity through the standard bridge, whose
independent validator rejects corruption and whole-versus-half overlap.

The pinned RapidWright device probe further showed that logical placement and
primary database identities differ.  Lower, upper, and whole placement use
`RAMB180/RAMB18E2_L`, `RAMB181/RAMB18E2_U`, and `RAMB36/RAMB36E2`; their
primary database views are `RAMBFIFO18`, `RAMB181`, and `RAMBFIFO36`.  The
source-sealed tile-group contract records both sides of this mapping and fails
closed on any mismatch.

The typed hard-block implementation now follows that boundary. A compact
source-sealed contract enumerates exact legal native windows. An OpenPARF
operator chooses conflict-free whole-chain windows from the global-placement
displacement cost, excludes every owned hard block from singleton MCF, and
freezes the result before ISM. The current allocator is deliberately described
as a deterministic conflict-aware heuristic, not as an exact optimizer. Its
output is independently checked against the native adjacency artifact. Real
multi-site tiles are represented as one Bookshelf capacity site plus exact,
source-ordered physical-site slots; unrelated device sites are not admitted
to the placement problem. This preserves tile geometry without losing the
identity of either DSP/URAM sibling site. A typed area type must have complete
contract ownership: it remains wirelength-movable during global placement but
is removed from generic density and its convergence gate because exact window
selection supplies the capacity proof. Partial ownership fails closed. The
typed legalizer uses the exact Bookshelf site center in memory while `.pl`
serialization retains the lower-left site key; an illegal typed result aborts
before ISM detailed placement. ISM handles empty coordinates in a sparse
real-device SITEMAP as inert entries, without fabricating sites or skipping
detailed placement. DSP,
then BRAM, then URAM must each pass compiled placement, RWRoute, and OpenSTA
before this implementation is counted as qualified evidence.

### Route B: DREAMPlaceFPGA research control

The DREAMPlaceFPGA branch is retained to measure its official Interchange
front end and analytical placement behavior.  Because upstream does not
provide the required UltraScale+ detailed placement and macro/clock/SLR
contracts, this route remains ineligible for default promotion unless those
missing core capabilities are implemented and independently verified.

The branch now has one internal native-run boundary rather than a public CLI
or provider selector.  It accepts the same mapped netlist, PackedSiteNetlist,
and ArchitectureDB identities used by the other candidates, serializes only
the natively supported LUT1--LUT6/LUT6_2, FDRE, DSP48E2, and RAMB36E2 subset,
and invokes the pinned official `Placer.py` process.  It does not contain a
fake placer or a fallback to EmuFlow's old greedy site legalizer.  A returned
`.phys` must preserve every packed cluster, exact legal BEL candidate, cell
type, and complete cell ownership before a diagnostic candidate certificate
is emitted.  The following RapidWright boundary remains `blocked` and cannot
be converted into `emuflow.xilinx-placement/v1` while detailed placement,
cascade, clock-region, and multi-SLR capabilities are missing.

The resource-covering adapter fixture has passed real official-schema
serialization for LUT6, FDRE, DSP48E2, and RAMB36E2 and strict certificate
revalidation.  This is adapter evidence, not a native optimization result.
The runtime gate additionally requires the exact upstream revision, compiled
`place_io`, successful compiled-module import, and an upstream-qualified
PyTorch 1.6, 1.7, or 1.8 runtime.  The available local PyTorch 2.8 toolchain
requires C++17 while the pinned upstream compiles its extensions as C++14, so
the current environment is reported as `core_missing:torch_version`; no
native runtime pass is claimed.

### Route C: AMF-Placer secondary candidate

The AMF branch establishes a bounded adapter and a sealed native-runner path.
The runner requires exact source/binary/patch identity, calls one external AMF
process, requires evidence for packing, global placement, detailed placement,
and completion, then independently certifies the returned site/BEL ownership.
The test-double subprocess regression is labelled `test-only`; AMF becomes a
real candidate only after the patched public optimizer itself runs this path.
It is subject to the same RapidWright routing and OpenSTA gates as Route A.

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

The isolated A2 branch extends the native chain extractor and legalizer for
`CARRY8 + 8xLUT6_2`, emits an empty placement seed, and independently checks
same-site BEL roles plus typed `CARRY_NEXT` adjacency.  The modified C++
operators have now run on the sequential two-CARRY8 fixture without
preplacement or fallback: 20 logical atoms occupied three sites.  The
no-search bridge preserved the result; RapidWright routed four nets, six sinks,
and 18 PIPs with zero missing sinks and three exact route-delay bindings.
Standalone OpenSTA 3.1.0 (`051222e4ec`) reported one complete timed endpoint,
zero failures, WNS +38.742802 ns, and TNS 0 ns.  This qualifies CARRY8, not the
remaining hard-block cascades or the DLA workload.

Native device constraints are split by evidence.  The version-2 exporter
seals its ArchitectureDB and RapidWright source identity, scopes all sites to
the exact ArchitectureDB inventory, and proves each vector lane rather than
accepting coordinate proximity.  On the real XCVU19P model it currently
exports 508,992 `CARRY_NEXT`, 3,808 `DSP_CASCADE`, 1,980 `BRAM_CASCADE`,
and 316 `URAM_CASCADE` edges.  Carry endpoints share the certified
continuation node; DSP and URAM endpoints use one typed direct native arc to
the matching target-site-pin vector.  BRAM validates all 72 data/parity lanes
and both ECC cascade lanes: data/parity traverse two directed native arcs via
a sealed hard-block-internal node, while ECC traverses one.  At the RAMB18 /
RAMB36 native mode branch, only the arc that reaches the exact RAMB36 target
vector is accepted.  AMD's same-clock-region cascade rule then cuts the
physical graph at every clock-region boundary; a present device PIP alone is
not treated as semantic legality.  The ArchitectureDB-facing chain uses the
exact co-tiled RAMB18/RAMB36 mode anchor rather than an invented coordinate
alias.  Clock-region and SLR
membership/capacity are available.  XCVU19P half-column membership and maximum
unique-clock capacity are not exposed by the pinned RapidWright API, so
OpenPARF's benchmark-specific 12/24-clock constants are not imported and that
capability stays fail-closed.
