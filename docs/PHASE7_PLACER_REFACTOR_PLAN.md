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

### 7A-4: native packing

- OpenPARF owns the choice of compatible logic clusters and hard-block modes.
- Connectivity-discovered macro members are indivisible constraints, not
  preplaced cells.
- Control sets, LUT fracturing, carry and MUX roles, BRAM half/full modes, and
  cascade order must remain explicit in the optimization database.
- The previous custom ``SitePacker`` may remain only as a small independent
  checker/fixture producer while this route is under development; it cannot
  participate in production placement evidence.

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
2. **A2 macro/resource gates -- in progress.**  CARRY8, DSP48E2, RAMB36E2,
   and URAM288 bounded fixtures have physical evidence.  MUXF7/8/9, RAMB18
   native tile modes, clock legality, and multi-SLR capacity remain gates.
3. **RAMB tile-group gate -- next.**  Extend the native device artifact with
   explicit upper/lower/whole site and BEL proofs, consume that group in
   OpenPARF, then run a two-RAMB18 real XCVU19P route/OpenSTA fixture.  Until
   this passes, all inferred half-site materialization stays rejected.
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

No branch may change the public default independently.  Experimental runtime
directories remain outside the repository and are deleted after a compact
terminal summary is sealed.
