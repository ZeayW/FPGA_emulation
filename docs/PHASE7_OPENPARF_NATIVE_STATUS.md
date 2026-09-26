# Phase 7 OpenPARF-native status

This document records implemented evidence, not a default-provider claim.

## Physical macro contract gate

`emuflow.xilinx-physical-macro-contract/v1` is a compact,
provider-neutral boundary between mapped connectivity and a future native
macro-aware placer adapter. It is derived deterministically and contains only
macro members, roles, dedicated port-to-port connectivity certificates, and
relative-resource requirements. It does not copy the mapped netlist and does
not perform packing, placement, or search.

| Structure | Connectivity contract | Device binding |
| --- | --- | --- |
| CARRY8 with eight LUT6_2 O5/O6 adapters | Implemented, fail-closed | Same UltraScale+ slice |
| MUXF7/MUXF8/MUXF9 cone | Implemented, fail-closed | Same UltraScale+ slice with explicit BEL roles |
| RAMB18E2 half-site demand | Implemented without name-based pairing | RapidWright exposes overlapping upper/lower/whole native views, but ArchitectureDB currently retains one anchor; an explicit source-sealed tile-group adapter is required |
| CARRY8/DSP48E2/RAMB18E2/RAMB36E2/URAM288 cascade | Logical chain order and exact port signals implemented | Typed native adjacency is adapter-required |

Cascade placement deliberately does not assume adjacent sites are related by
`y + 1`. The current ArchitectureDB has no authoritative typed adjacency or
half-column graph, so the contract requires same-column, same-SLR, consecutive
native-cascade neighbors and marks resolution `adapter_required`. A future
device adapter must prove those relations from authoritative device data.

The same rule applies within a BRAM tile.  The native database exposes upper
RAMB18, lower RAMB18, and whole RAMB36 as overlapping site views.  The current
ArchitectureDB anchor and its alternative templates do not prove their exact
native site/BEL mapping, so coordinate parity and site-name arithmetic are not
accepted.  RAMB18 placement remains fail-closed until a source-sealed tile
group proves those identities and whole-versus-half mutual exclusion.

Ordinary atomic LUT/FF/DSP/BRAM/URAM cells are not copied into this contract.
Unknown, branching, merging, cyclic, partial-width, multiply-owned, or
incomplete macro topology fails closed. The first explicit OpenPARF macro
adapter now consumes the CARRY8 subset of this contract; all other macro
families remain pending.

## CARRY8 native-core qualification

The isolated A2 branch now contains and has executed the first native
CARRY8/LUT6_2 implementation:

- `chain_info.cpp` retains the legacy four-PROP CLA4 behavior but recognizes
  typed `CARRY8`, extracts eight ordered `LUT6_2` members from `S[0:7]`, and
  independently proves that each `DI[i]` is driven by the same LUT instance;
- `chain_legalizer.cpp` derives member arity from the extracted chain and maps
  an eight-LUT CARRY8 unit to one full slice with A6LUT through H6LUT slots;
- `xilinx_openparf_carry8.py` emits unplaced typed atoms and CAS connectivity,
  invokes GP, native chain legalization, masked LUT/FF legalization, and ISM,
  then independently checks exact BEL roles and RapidWright-certified
  `CARRY_NEXT` adjacency;
- parsed `.shape` records remain unused.  The explicit route deliberately uses
  typed cascade and DI/S connectivity instead of claiming that inactive shape
  infrastructure provides macro support.

The audit also found and fixed two independent plumbing bugs. Bookshelf
`OUTPUT CAS` now dispatches to the output-cascade callback. ISM now builds its
fixed mask under `carry_chain_legalization_flag` and includes both the carry
primitive IDs and every associated LUT ID; it no longer depends on IO
legalization or freezes only the carry area type. Source-backed tests guard
both fixes.

`probe_xilinx_openparf_carry_native_support` validates a mapped design's
PhysicalMacroContract, reduces it to a deterministic minimum reproduction
(CARRY8 count, eight LUT6_2 adapters per unit, chain lengths, and required
O5/O6 semantics), audits those exact pinned sources, and emits the common
Xilinx placer capability contract.  A two-CARRY8/16-LUT6_2 chain fixture
proves unplaced export and independent legality/tamper detection.  The probe
records `fallback=forbidden` and `preplacement=forbidden`, so source presence
cannot be mistaken for a passing compiled run.

Accordingly this branch does not manufacture a passing runtime by changing the
macro into four ordinary LUTs, invoking the old search legalizer, or assigning
sites before OpenPARF.

The compiled runtime and physical gates have now passed on a sequential
two-CARRY8 fixture.  Native OpenPARF placed 20 logical atoms into three sites,
including two full-slice CARRY8 macros and one certified `CARRY_NEXT` edge.
The no-search bridge preserved those sites and BEL roles.  RapidWright then
routed four nets and six sinks with 18 PIPs, zero missing sinks, and three
exact logical route-delay bindings; the maximum extracted route delay was
0.32760000610351564 ns.  Standalone upstream OpenSTA 3.1.0 at revision
`051222e4ec` emitted the complete register-to-register path and independently
validated WNS +38.742802 ns, TNS 0 ns, and zero failing endpoints.  The compact
OpenSTA summary SHA-256 is
`b21f290069a48ee6e2c63ad9cd057328e6d9c12a6417c5cbe44bf518e8fc0e56`.

This qualifies the CARRY8 subset only.  MUXF7/8/9 relative placement,
RAMB18 half-site modes, DSP/BRAM/URAM cascades, clock legality, and multi-SLR
capacity remain fail-closed and must pass equivalent runtime, RWRoute, and
OpenSTA gates before Koios DLA medium is admitted.
