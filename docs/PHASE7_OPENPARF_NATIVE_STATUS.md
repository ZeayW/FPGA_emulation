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
