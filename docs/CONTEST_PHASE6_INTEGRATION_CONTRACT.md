# Contest Architecture and Phase 6 Integration Contract

Public contest import and evaluation belong to the separately installed
`emuflow-tools` distribution. They may supply explicit BoardDB and design inputs
to the maintained upstream flow; they do not select an alternate production
scheduler or physical backend. Phase 6 consumes the decisions below unchanged.

## Frozen upstream artifacts

- Instance ownership and directed cut endpoints use the current Phase 3 v4
  assignment selector and its sealed assignment index. The logical assignment
  schema remains `emuflow.partition-assignment/v1`.
- RouteStore v3 owns multicast trees and timing-path identities;
  `emuflow.system-route-constraints/v1` owns shared capacity and maximum hops.
- ScheduleStore v4 owns each demand, hop, direction, logical lane, absolute
  slot, arrival slot and `tdm_ratio`. The maintained scheduler is
  `deterministic-dependency-earliest-slot-v1`; round-barrier scheduling is retired.
- BoardDB owns FPGA identities and fixed link inventory. A contest projection
  is an academic architecture model. Real-board qualification additionally
  requires verified package pins, electrical constraints, clocks and link
  measurements; the runtime has no hardware-BSP execution chain.

Phase 6 may group entries onto physical lanes and add physical annotations.
It must preserve the route, direction, logical lane, ratio and absolute event
meaning of every schedule entry. Fixed wiring, P/N pairing, bank direction and
resource-budget checks remain mandatory.

## Phase 6 boundary

The runtime publishes split-netlists v3 and typed transport v2, with small
selectors for logical lanes, virtual anchors and fixed differential bindings.
Logical schema names do not authorize legacy whole-graph JSON production paths.
Current readers validate the source generation and typed relation coverage.

The baseline and retained Chimew adapter consume the same upstream decisions.
Chimew may select physical channels within the fixed inventory; it must preserve
schedule identity, direction and slot semantics.

## Cross-track acceptance

The contest track must prove exact contest-format parsing, official-objective
reconstruction, topology/capacity legality, reproducible input hashes, and
BoardDB projection provenance.  It may stop after Phase 5.

The Phase 6 track must prove exact schedule-entry coverage, zero lane/slot and
physical-resource collisions, independently reconstructed objectives, split
netlist integrity, and cycle equivalence.  Paper-faithful metrics and EmuFlow
board-aware extensions are reported separately.

The combined gate runs a frozen contest-derived BoardDB and Phase 5 schedule
through the upgraded Phase 6 and verifies that all frozen upstream fields are
byte-for-byte unchanged after path normalization.  Results on a contest
projection are academic architecture validation, not hardware sign-off.
