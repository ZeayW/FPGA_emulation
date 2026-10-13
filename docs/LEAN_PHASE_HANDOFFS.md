# Lean phase handoffs

## Contract

Each phase owns its newly produced facts, publishes them atomically, and passes
references rather than copied graphs or reports. The receiving phase reads only
what its algorithm needs. Diagnostics are not mandatory consumer inputs.

| Boundary | Canonical data passed | Consumer work |
| --- | --- | --- |
| Phase 1 → timing / Phase 3 | One mapped EmuIR, source/mapping/clock identity, compact resource summary | Timing reads cells/connectivity; partitioning reads resources/hyperedges. Neither remaps RTL. |
| Timing → Phase 3–5 | One indexed original-path population; derived compact net weights | Iterate paths/weights needed by the algorithm, not duplicate the original graph. |
| Phase 3 → Phase 4 | Assignment plus references to original graph and platform | Derive crossing communication demand from assignment; do not copy instance maps into diagnostic reports. |
| Phase 4 → Phase 5 | Canonical routes and link demand | Assign TDM resources/events to these routes. |
| Phase 5 → Phase 6 | Canonical route/schedule plus graph/platform references | Split logic and bind transport; verify macro-cycle semantics. |
| Phase 6 → Phase 7 | Per-FPGA physical netlist/constraints and transport timing binding | Implement each FPGA and publish physical delay/legality summaries. |
| Phase 7 → global STA | Original-path identity, physical segment/interface delays, schedule/link timing | Compute whole-original-design timing once with the selected engine. |

## Checks

Keep producer key constraints, atomic publication and actual circuit correctness
checks: legal endpoints, source/device/clock matching, capacity, routing/TDM
legality, macro-cycle equivalence, physical coverage, DRC/unrouted counts and
whole-design timing coverage. A downstream consumer must reject unsupported or
incompatible inputs and malformed records that it consumes.

Do not implicitly run SQLite `integrity_check` or `quick_check` on phase
publication or load. These are database-engine audits, not circuit verification.
Their removal deliberately drops automatic exhaustive storage-engine auditing;
it does not assert that the same physical corruption assurance is retained.
SQLite still enforces unique keys during insertion, and readers still reject
malformed files/records. Explicit corruption diagnosis can run the standard
SQLite audit on a read-only completed artifact. Do not replace the removed
audits with another unconditional whole-file scan.

For sealed external artifacts, existing explicit identity checks remain. Within
one invocation, reuse the validated object/identity rather than repeatedly
rehashing or reconstructing it. Final evidence is sealed once at completion.

## Implementation status

- Removed automatic engine audits from EmuIR publication, timing-path and
  partition-weight publication, and timing-path/weight iteration.
- Timing-weight derivation no longer loads EmuIR when its path input has already
  been semantically validated by the calling timing stage.
- Existing ordered-row decoding and direct Yosys stream import remain.
- **Pending:** the main OpenSTA exporter still materializes the EmuIR graph and
  connection indexes. Indexed/streamed timing consumption and elimination of
  unnecessary staging-to-owner rewrites require separate scale measurements;
  do not describe the whole Phase 1→2 interface as bounded-memory yet.

## Testing from existing Phase 1

When explicitly requested, reuse the existing completed Phase 1 artifact
read-only for the current investigation. Require the final published filename
and successful phase report with matching source/mapping/clocks. Never treat
`.tmp` files as completed output while their producer is live. Execute downstream
tests in a separate scratch directory, retain only compact measurements, and do
not build a persistent cache/checkpoint framework. This diagnostic continuation
alone is not new full Phase 1–7 evidence.
