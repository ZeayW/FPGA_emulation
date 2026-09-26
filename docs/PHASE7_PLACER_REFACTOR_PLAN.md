# Phase 7 native placer refactor plan

This is the implementation plan for replacing the historical
``OpenPARF guidance -> custom site packing -> greedy legalization`` path.  It
does not change the public Phase 7 default until the complete promotion gate
passes.

## Decision

OpenPARF remains the primary open placer.  The intended production pipeline is

```text
mapped netlist + source-sealed device facts
  -> connectivity-derived macro discovery
  -> OpenPARF native packing / analytical global placement
  -> OpenPARF resource and macro legalization
  -> OpenPARF detailed placement
  -> independently checked site/BEL certificate
  -> RapidWright materialization and RWRoute
  -> routed-delay binding
  -> standalone OpenSTA global WNS/TNS
```

RapidWright is the authority for Xilinx device facts and physical routing; it
is not a second placer.  OpenSTA is the timing authority; it is not used to
repair placement.  The bridge may translate identifiers and reject an invalid
answer, but it may not search for alternative sites, repack a macro, or invoke
the retired greedy legalizer.

DREAMPlaceFPGA and AMF-Placer remain isolated comparison branches.  They may
be promoted only by the same physical and timing gates; an interchange or
adapter roundtrip is not placement evidence.

This is not a plan to replace OpenPARF with a new first-party placer.  EmuFlow
may add device adapters, source-sealed constraints, and independent
certifiers, but the optimization steps in the primary route must execute in
OpenPARF.  In particular, the historical custom ``SitePacker`` and greedy
nearest-site legalizer are migration inputs only; they are not components of
the target route.

## Route portfolio and selection policy

The three branches deliberately explore different upstream placers, but they
share one placement certificate and one physical/timing tail.

| Route | Intended ownership | Current role | Promotion blocker |
| --- | --- | --- | --- |
| OpenPARF native | packing, analytical global placement, resource/macro legalization, detailed placement | primary implementation | complete UltraScale+ primitive/constraint coverage and DLA-medium evidence |
| DREAMPlaceFPGA | its native FPGA packing/placement/legalization pipeline | research comparison | source-backed XCVU19P resource model, hard macros, clocks/SLRs, and an executable native detailed-placement gate |
| AMF-Placer | its public mixed-size placement, macro legalization, CLB packing, and detailed placement | secondary comparison | public-core support for the required primitive set plus XCVU19P clock/multi-SLR qualification |

There is no provider fallback.  A route either admits the complete design and
produces a certified result, or it fails closed with an exact missing
capability.  Unsupported candidates do not delay the OpenPARF primary route.

## Phase 7 decomposition

### 7A-1: source and capability admission

- Seal mapped netlist, ArchitectureDB, native device-fact artifact, tool
  revisions, provider manifest, constraints, and one physical seed.
- Build a primitive/constraint capability matrix before placement.
- Reject an unsupported primitive or constraint; never silently lower it or
  fall back to another placer.

### 7A-2: logical macro discovery

- Derive CARRY8/LUT6_2, MUXF7/8/9, DSP, BRAM, and URAM relationships only from
  mapped connectivity.
- This stage records membership and relative requirements.  It does not choose
  a physical site and is not a site packer.

### 7A-3: authoritative device-resource model

- Import exact compatible resources, clock regions, SLRs, dedicated adjacency,
  and overlapping modes from source-sealed RapidWright facts.
- Represent one BRAM tile as an explicit group containing lower RAMB18,
  upper RAMB18, and whole RAMB36 views.  RAMB36 claims both halves; two
  RAMB18 instances may share a tile only by occupying distinct proven slots.
- A site name, coordinate parity, ``Y-1``, or ``Y/2`` is never a device proof.
  Missing native slot/BEL facts leave RAMB18 as ``adapter_required``.
- Store the half-million-site physical map once in the indexed
  ``site-map.sqlite3`` contract.  The atom/name map contains only atom identity,
  coordinate axes, and a descriptor for that database; validators query only
  the coordinates and resource rows used by the candidate.  Do not duplicate
  the site table or hard-block windows in a large JSON hot path.
- The real 512,880-site XCVU19P gate must retain the measured indexed-contract
  evidence: 8.32 seconds/843 MiB for the former full JSON parse versus
  0.73 seconds/24.1 MiB for the ten-coordinate query, with exact selected-row
  equality.  Performance evidence is invalid if conversion time is charged to
  every validation instead of once at export.

### 7A-4: native packing

- OpenPARF owns the choice of compatible logic clusters and hard-block modes.
- Connectivity-discovered macro members are indivisible constraints, not
  preplaced cells.
- Control sets, LUT fracturing, carry and MUX roles, BRAM half/full modes, and
  cascade order must remain explicit in the optimization database.
- The previous custom ``SitePacker`` may remain only as a small independent
  checker/fixture producer while this route is under development; it cannot
  participate in production placement evidence.
- EmuFlow must not pre-pack a convenient answer and ask OpenPARF merely to
  preserve it.  Only connectivity-derived indivisible macros and explicit
  user constraints may enter as fixed grouping requirements.

### 7A-5: analytical global placement

- Run OpenPARF wirelength and density optimization with typed resources,
  clock-region/SLR limits, and macro geometry active in the objective and
  feasibility model.
- Hard blocks remain movable during global placement.  Preassigned sites are
  forbidden except explicit user constraints.
- Reject disconnected, collinear, saturated, or non-finite placement domains
  before optimization.

### 7A-6: native legalization

- Use OpenPARF resource-specific legalization for LUT/FF, CARRY/MUX, DSP,
  BRAM, and URAM.
- Dedicated chains select complete source-sealed windows atomically.
- Overlapping device modes use explicit claim sets, so whole-tile and half-tile
  occupancy cannot coexist illegally.
- No post-placement nearest-site search is allowed.

### 7A-7: native detailed placement

- Run OpenPARF detailed placement after legalization while freezing macro
  membership and all resource claims.
- Detailed placement may improve wirelength/timing only within the legal move
  set; it must not dissolve macros, change BRAM mode, cross a clock/SLR limit,
  or overwrite fixed constraints.
- Independently re-import every atom and check coverage, uniqueness, site/BEL
  compatibility, control sets, relative offsets, dedicated adjacency, region
  capacity, and deterministic seed identity.

The independent checker is deliberately not a repair pass.  It cannot move a
cell, choose a BEL, split or combine a cluster, or change a hard-block mode.
Any rejected certificate returns to the owning upstream placer implementation.

### 7B: RapidWright physical realization

- Translate the checked certificate through exact native site/BEL mappings.
- Materialize every primitive, route with RWRoute, and require zero missing
  logical sinks, zero unrouted required connections, and passing physical
  checks.
- Extract endpoint-exact routed delays.  RapidWright may not change placement.

### 7C: independent global timing

- Bind routed FPGA segments, board/link timing, TDM events, and transport
  boundaries into the standalone OpenSTA model.
- Require complete timing-path coverage and report the final global WNS/TNS.
- Local HPWL, density, displacement, and per-FPGA timing are diagnostics only;
  they cannot select the default route.

## Implementation order and gates

1. **A1 engine gate -- complete.**  Ordinary LUT/FF plus independent hard
   blocks passed native OpenPARF, RapidWright routing, and OpenSTA.
2. **A2 macro/resource gates -- in progress.**  CARRY8, DSP48E2, RAMB18E2,
   RAMB36E2, and URAM288 bounded fixtures have physical evidence.  MUXF7/8/9,
   clock legality, and multi-SLR capacity remain gates.
3. **RAMB tile-group export and consumption gates -- complete.**
   The pinned real XCVU19P database exports and independently validates 2,160
   tile groups with distinct lower-RAMB18, upper-RAMB18, and whole-RAMB36
   placement/native identities.  OpenPARF selected two independent RAMB18
   half claims in one tile, and the real XCVU19P fixture passed RapidWright
   routing and standalone OpenSTA 3.1 timing.  Inferred coordinate/name
   arithmetic remains rejected.
4. **Unified OpenPARF candidate backend.**  Remove the singleton-only source
   path and run the production mapped netlist through native macro discovery,
   packing, placement, legalization, detailed placement, and the shared
   RapidWright/OpenSTA tail with no fallback.
5. **Small resource-covering qualification.**  Exercise every admitted
   primitive and constraint with exact cell accounting and tamper tests.
6. **Koios DLA medium Phase 1--7.**  Run one physical seed, retain only the
   compact terminal summary, and require the complete routed and global
   OpenSTA gate.
7. **Candidate comparison.**  Run DREAMPlaceFPGA or AMF only if its capability
   matrix has no required ``core_missing``/``unverified`` entry.  Freeze the
   same Phase 1--6 input, architecture, router, timing model, and seed.
8. **Promotion.**  Select the default only from complete legality, runtime,
   and global WNS/TNS evidence.  Otherwise keep the existing default and state
   the exact blocker.

## Shared contracts and fair comparison

All routes consume the same mapped netlist, ArchitectureDB, native device
facts, fixed constraints, clock/SLR limits, seed, and objective weights.  They
must emit the same provider-neutral certificate:

- one record for every mapped atom;
- exact logical cluster/macro ownership;
- exact physical site and BEL;
- explicit overlapping-resource claims;
- fixed/clock-region/SLR and dedicated-adjacency evidence;
- provider, source, device-fact, configuration, and seed identities.

The common tail starts only after this certificate passes.  RapidWright then
materializes the answer without placement search, RWRoute produces the routed
implementation, and standalone OpenSTA computes complete-global WNS/TNS.
HPWL, density, displacement, placer objective, and local timing are useful
diagnostics but cannot decide the default.

The first comparison uses exactly one physical seed and the same complete
Phase 1--6 input.  A candidate that lacks a required primitive or constraint
is reported as unsupported rather than being run on an easier surrogate
design.  Koios DLA medium is the promotion workload; bounded fixtures prove
individual resource contracts but cannot establish QoR.

## Branch policy

- ``feature/phase7-openparf-native-hardblocks``: primary implementation.
- ``feature/phase7-dreamplacefpga``: research control; currently not
  production-eligible because native detailed placement and several
  UltraScale+ constraints are missing upstream.
- ``feature/phase7-amf-placer``: secondary candidate; currently adapter-only
  until the public optimization core and XCVU19P constraints execute end to
  end.
- ``feature/phase7-placer-integration``: shared contracts and final comparison
  only after branch-local gates pass.

The implementation branches may progress independently, but integration is
ordered rather than speculative:

1. land provider-neutral certificate/capability changes;
2. complete the OpenPARF resource-covering and DLA-medium gates;
3. admit each candidate only when its branch-local capability gate passes;
4. run the same frozen end-to-end comparison; and
5. promote at most one default, with the losing or incomplete routes retained
   only as explicitly named research providers.

No branch may change the public default independently.  Experimental runtime
directories remain outside the repository and are deleted after a compact
terminal summary is sealed.
