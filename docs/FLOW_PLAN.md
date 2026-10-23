# Architecture and phase contracts

EmuFlow separates logical compilation, board-level communication, per-FPGA
physical implementation, and global timing analysis. Providers exchange
versioned artifacts rather than sharing tool-internal state.

## Data flow

```text
RTL + constraints + mapping profile
  -> mapped EmuIR and resource summary
  -> original TimingPathDB and optimization weights
  -> clusters and FPGA assignment
  -> board-level routes
  -> TDM ratio/lane/slot assignment
  -> per-FPGA netlists and transport RTL
  -> physical placement/routing and delay bindings
  -> whole-design global timing
```

## Canonical inputs and outputs

| Boundary | Canonical information | Consumer responsibility |
| --- | --- | --- |
| Frontend -> timing/partitioning | EmuIR connectivity, primitive parameters, resources, clocks, identities | Use the mapped circuit; do not reinterpret it as a contest communication graph |
| Timing -> partitioning/routing/TDM | Original-path identities, clock constraints, timing weights | Preserve the relationship between optimization representatives and original members |
| Partitioning -> system routing | Assignment, cut endpoints, BoardDB, route constraints | Choose legal source-to-sink communication paths |
| System routing -> TDM | Ordered routes, directions, link demand | Allocate ratios, lanes, and slots without changing route identity |
| TDM -> splitting/transport | Schedule-entry identities and event semantics | Bind logical boundaries and generate matching transport RTL |
| Splitting -> physical implementation | Per-FPGA netlists, constraints, device/architecture inputs | Account for every cell and enforce physical resource/ownership legality |
| Physical implementation -> global STA | Logic/interface delays, original-path bindings, concrete schedule, link timing | Check event readiness and launch-to-capture timing for the declared population |

One owner supplies each large population; reports summarize it rather than
embedding duplicate netlists, maps, or timing-path trees.

The optional native one-shot controller executes this dependency sequence with
isolated shared phase adapters. It exchanges bounded configuration, artifact paths
and report projections; each phase releases its working state before the next
process starts. The transitional Python data planes remain explicit. See
[native compile control](USER_GUIDE.md#native-compile-control) for implementation
selection and unsupported modes, and the [migration contract](PHASE3_NATIVE_MIGRATION.md)
for the remaining native ownership requirements.

## Logical model

EmuIR retains directed endpoints, hierarchy, cell types and parameters,
resource vectors, clock/reset classification, and cut eligibility. Logical
schema identity is separate from its physical storage representation.

Phase 6 copies the bounded `resource_reservations_by_fpga` seal directly from
the Phase 3 assignment index into its split manifest. Its independent validator
compares that seal with the assignment index, rejecting missing or changed
reservations before the physical backend consumes the split. This control
metadata check does not load the instance ownership graph.

BoardDB describes FPGA nodes, resource budgets, and inter-FPGA links. An
academic BoardDB can omit package pins. Concrete deployment additionally
needs authoritative device resources, physical connections, electrical rules,
reference clocks, reset handling, and a board shell.

TimingPathDB identifies source logical paths and endpoints. Physical binding
must keep same-FPGA and cross-FPGA paths disjoint and preserve each original
member exactly once in the declared global population.

## Boundary semantics and timing

Sequential-only cuts retain unsupported combinational connectivity as atomic
partition units. Generalized Static Exact releases structurally supported
combinational boundaries under its explicit synchronous semantic contract.
See [Static Exact](STATIC_EXACT_COMBINATIONAL_CUT.md).

Phase 3 enforces structural capacity, fixed/group, SCC/cascade, topology, and
hop constraints. It may optimize predicted delay or TDM pressure, but does
not certify future slot readiness or physical deadlines.

Phase 4 chooses system routes. Phase 5 assigns concrete TDM resources/events.
Phase 6 materializes transport and checks macro-cycle semantics. Phase 7
provides physical delays and the global timing result. The
[contest/Phase 6 contract](CONTEST_PHASE6_INTEGRATION_CONTRACT.md) defines the
ownership of route and schedule fields.

## Physical providers

- The open academic route uses VPR packing/routing and OpenPARF placement.
- The RapidWright route uses device facts and RWRoute, with native OpenPARF
  responsible for placement and legalization.
- The optional Vivado route uses the proprietary device implementation tools.

A bridge translates identities and validates the provider result; it must not
silently choose a different placement or architecture. Resource, macro,
cascade, clock-region, and SLR requirements must be admitted explicitly.
Unsupported requirements are errors, not fallback opportunities.

## Scope

A physical flow must retain source timing identities and explicit clock periods
even when optimization is timing-oblivious. OpenSTA is the default global
numeric timing engine; its model consumes physical segment/interface and
transport-event bindings rather than precomputed Python slack.

Academic physical models and board-link models are not measured silicon or
board signoff. Bitstream generation and hardware operation require a compatible
concrete platform. See [timing scope](global-opensta-qualification.md) and
[validation requirements](VALIDATION.md).
