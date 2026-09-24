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
| RAMB18E2 half-site demand | Implemented without name-based pairing | Lower/upper half selection is adapter-required |
| CARRY8/DSP48E2/RAMB18E2/RAMB36E2/URAM288 cascade | Logical chain order and exact port signals implemented | Typed native adjacency is adapter-required |

Cascade placement deliberately does not assume adjacent sites are related by
`y + 1`. The current ArchitectureDB has no authoritative typed adjacency or
half-column graph, so the contract requires same-column, same-SLR, consecutive
native-cascade neighbors and marks resolution `adapter_required`. A future
device adapter must prove those relations from authoritative device data.

Ordinary atomic LUT/FF/DSP/BRAM/URAM cells are not copied into this contract.
Unknown, branching, merging, cyclic, partial-width, multiply-owned, or
incomplete macro topology fails closed. The OpenPARF runtime does not consume
this contract yet; native macro placement therefore remains pending.

## CARRY8 native-core qualification

The CARRY8/LUT6_2 family is now classified `core_missing`, not
`adapter_required`.  This conclusion comes from the checked-in implementation,
not from upstream documentation:

- `chain_info.cpp` allocates exactly four associated LUT IDs per chain unit and
  accepts only `PROP[0:3]`.
- `chain_legalizer.cpp` places one half-site chain unit and exactly four LUT
  slots.  It has no representation for one CARRY8 plus eight LUT6_2 cells whose
  O5 and O6 outputs jointly feed DI/S.
- Bookshelf carry `.shape` records are stored in the database but are not
  consumed by global placement, legalization, legality checking, or ISM;

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
proves the mismatch and the gate records `runtime_launched=false`,
`fallback=forbidden`, and `preplacement=forbidden`.

Accordingly this branch does not manufacture a passing runtime by changing the
macro into four ordinary LUTs, invoking the old search legalizer, or assigning
sites before OpenPARF.  A real compiled GP -> carry legalizer -> ISM run would
only become meaningful after the core and input semantics are extended to
CARRY8/LUT6_2 and independently tested.

The remaining implementation boundary is explicit in the qualification
report: variable eight-LUT CARRY8 chain metadata, full-slice CARRY8 chain
legalization, unplaced Bookshelf export of CARRY8/LUT6_2 resource and cascade
semantics, and an exact importer/validator for same-site roles and native chain
adjacency.  The carry legalizer is now initialized from the current global
placement coordinates without requiring IO legalization.  The remaining items
are structural core/adapter changes, not parameters that can be approximated by
preplacement.
