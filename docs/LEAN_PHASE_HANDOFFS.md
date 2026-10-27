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

The acceptance endpoint for this performance work is the requested real design
finishing Phase 1--7 with measured stage/total runtime and final global timing.
Storage and adapter measurements alone do not qualify that endpoint.

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
  library delays, clocks or STA algorithms. Directed-through-net qualification
  retains its reader route; VTR specialization is retired.
- Escaped instance names no longer reconstruct all pins through indexed
  endpoint lookups once per emitted pin. An indexed owner-alias join identifies
  possible collisions; only candidate pairs need pin inventories. Small
  top-level and escaped-pin checks plus reported endpoint validation retain
  ambiguity rejection. A repeated escaped-name regression bounds SQL query
  count and compares emitted bytes against the reference writer.
- `PYTHONPATH=src python scripts/validation/qualify_handoffs.py` measures `repack`, `scan`,
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

## Retired Python execution paths

The old execution paths below have no retained flow consumers and have been
removed. Current stage producers, readers and required checks remain:

| Boundary | Removed | Retained |
| --- | --- | --- |
| Phase 2 | Whole-graph Python STA object index, obsolete TSV importer helpers and selected-path wrapper | Native path importer, typed path reader and independent semantic checks |
| Phase 3 | Python graph clustering, assignment and semantic producers, old graph validator/characterization commands, compressed/packed and exact-binary publishers, old PATRON host model/runner and unused wrappers | Current native whole-stage entry, typed readers and independent semantic checks |
| Phase 4 | Complete Python RouteStore writer and unused seal wrapper | Native routing/publication/seal kernels and typed readers |
| Phase 5 | Python route/schedule timing reconstruction, obsolete deadline-index execution chain and unused schedule simulation/TSV/manifest/testbench generators | Native schedule/timing projection, current typed readers and schedule legality checks |
| Phase 6 | Old transport metadata builder, complete-partition dictionary adapter and unused anchor/XDC producer helpers | Native split/transport producers, typed views, Chimew and bounded behavioral qualification |
| Phase 7 mapped netlist | Whole-graph JSON export and optional JSON/index dual writing | The canonical indexed mapped-netlist producer |
| Boundary timing | Old boundary constructor and endpoint/logic dictionary helpers | Native boundary production, typed readers, complete coverage validators and board-link timing |
| Physical query identity | Python identity-store publisher and sidecar/selector transaction | Native complete query producer, shared-lease readers and source validation |
| Physical conversion | Whole-graph Vivado primitive lowering and transport fanout transform | Indexed Vivado export and the writer-owned fanout transform |
| Global STA | Inline model exporter, Python bundle compression/expansion and scalar-result reader | Native event owner/model export/measurement import and full original-path reduction |
| Physical support | Unused placer/source-marker capability matrices, OpenPARF eligibility probe/hardblock-only qualification path and unintegrated Vivado optimization-proof producer | OpenPARF/RWRoute and Vivado implementation backends |

Small test fixtures encode explicit facts for retained readers and kernels.
Independent timing arithmetic stays bounded in tests; it has no stage entry,
store adapter or large-design mode. Historical implementations remain in Git.
The useful handoff and clock-capacity diagnostics are standalone scripts in
`scripts/validation`. Chimew remains supported. These retirements do not establish
completion of the remaining native migration or real NVDLA terminal validation.

## Testing from existing Phase 1

When explicitly requested, reuse the existing completed Phase 1 artifact
read-only for the current investigation. Require the final published filename
and successful phase report with matching source/mapping/clocks. Never treat
`.tmp` files as completed output while their producer is live. Execute downstream
tests in a separate scratch directory, retain only compact measurements, and do
not build a persistent cache/checkpoint framework. This diagnostic continuation
alone is not new full Phase 1–7 evidence.
