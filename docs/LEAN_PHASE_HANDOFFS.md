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
- New producers write physical storage v2: integer instance references in
  endpoints, integer pin-name references, independently pooled resource vectors,
  parameters, attributes, constants and extras. Equal ID/name fields are stored
  once. Explicit zero resources and original logical fields remain unchanged.
  Logical schema stays `emuflow.emuir/v1`; immutable storage-v1 inputs remain
  readable. Read-only SQL views do not duplicate stored records.
- Production streamed Xilinx OpenSTA extraction from v2 uses a read-only indexed
  view, merges integer-owner pin rows one instance at a time, specializes the
  timing library from the same first-occurrence representatives and resolves
  path endpoint identities lazily. It does not construct a full Python EmuIR,
  global pin map or global endpoint-object dictionary. This is not a change to
  library delays, clocks or STA algorithms. Legacy, VTR specialization and
  directed-through-net qualification retain their existing reader route.
- `python -m emuflow.handoff_qualification` measures `repack`, `scan`,
  `export` and actual-engine `timing` in separate processes. Explicit repack
  supports either storage version, with the old writer's engine audits removed.
  It records bytes, elapsed/CPU time, separate parent/child peak RSS,
  process I/O and explicit qualification-only logical/STA-input identities.
  Identity-sealing time is reported separately from operation time. Timing runs
  seal canonical per-path records as well as reporting QoR and truncation status;
  a truncated population cannot prove whole-design timing equality.
  Existing v1 outputs can be converted without synthesis or a whole-graph load.
- **Pending:** NVDLA storage/RSS/I/O and actual OpenSTA-result A/B measurements.
  Small regression equality and a repeated-structure size/RSS guard are not
  substitutes for that requested large-design gate. Yosys normalization still
  needs temporary staging before the compact owner is published; this remaining
  conversion must be measured rather than labelled zero-copy.

## Testing from existing Phase 1

When explicitly requested, reuse the existing completed Phase 1 artifact
read-only for the current investigation. Require the final published filename
and successful phase report with matching source/mapping/clocks. Never treat
`.tmp` files as completed output while their producer is live. Execute downstream
tests in a separate scratch directory, retain only compact measurements, and do
not build a persistent cache/checkpoint framework. This diagnostic continuation
alone is not new full Phase 1–7 evidence.
