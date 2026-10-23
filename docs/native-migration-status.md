# Native migration: remaining delivery gates

Reviewed 2026-10-23 against implementation commit
`5bc1b1178c7dd92a3cdd5eaa70659a3621381dcf`.
This is an incomplete migration. Kernel dispatch, compact representation and
individual differential tests do not establish native production completion.

## Measured priority: NVDLA Phase 3

The `nvdla-dualclock__xcvu19p4` prepared-input continuation at source
`dcd158d99953e47fb46a035f3377377f6303f37f` completed Phase 3 on 2026-10-23.
Its published `phase3-runtime.json` reports 21,771.872 seconds and sampled
process-tree peak RSS of 21,940,640 KiB. This is an older implementation,
not a measurement of the reviewed implementation. Phase 1/2 were reused;
this observation does not qualify a fresh complete Phase 1–7 run or final QoR.

| Disjoint measured work | Seconds | Share of Phase 3 |
| --- | ---: | ---: |
| PATRON native execution | 8,069.910 | 37.1% |
| Candidate assignment construction | 5,192.634 | 23.9% |
| Exact binary publication | 4,572.271 | 21.0% |
| All remaining work | 3,937.057 | 18.1% |

Do not add enclosing `patron.native_refinement` or
`publish.write_assignment_index` to these rows: their times include the child
operations. The three named operations account for about 82% of the observed
phase. Primary copy insertion took 54.706 seconds (0.25%); migrating that loop
is lower priority than measuring the three major costs.

The older run had 3,941,369 instances, 2,616,693 clusters and 1,187,189 cut nets.
Its assignment index was 10,972,807,168 bytes and exact binary
2,480,507,331 bytes. These sizes describe final artifacts, not peak scratch.
Parent-process write counters were 34,040,233,984 bytes; they exclude child
process I/O and must not be described as total flow disk traffic.

## Delivery order and evidence required

1. **Validate the major Phase 3 changes on the real input.** The reviewed
   implementation defers candidate-derived records until selection, releases
   the pressure owner, and natively generates/hashes/publishes final exact
   records. Measure the complete stage with the same input, algorithm,
   initializer, constraints and seeds. Compare total phase time and peak RSS,
   not just an old child label whose work may have moved. Require independent
   identity, ownership, resource and exact-contract validation. No current
   large-input speedup is established yet.
2. **Separate search cost from conversion cost.** The 134.5-minute native
   PATRON execution cannot be attributed to Python. Profile its actual search
   and flow work before changing its implementation. Preserve provider,
   policy, tie-breaking and independent legality checks; a reduced search
   budget is a different algorithm configuration.
3. **Finish the requested physical and timing continuation.** Inspect
   Phase 4–7/7C failures as stage blockers, retaining the same NVDLA design.
   A successful Phase 3, a physical handoff, or an isolated FPGA result cannot
   substitute for complete coverage, zero unrouted nets/DRC violations,
   final device budgets and whole-design OpenSTA timing.
4. **Close production ownership and entry-point gaps.** Track the following
   as stage deliverables rather than counting converted loops or commands.

| Boundary | Implemented foundation | Remaining gate |
| --- | --- | --- |
| W0 shared core | Typed IDs, artifact I/O, SQLite RAII, shared stage library | Consistent artifact/schema/error/metric contracts across all production producers and consumers |
| Phase 3 | Compact graph owners; native semantic/capture/hash/exact-binary production | Cluster/pressure generation still has Python owners; full-stage large-input qualification pending |
| Phase 5 | Native exact scheduling; typed ScheduleStore; cursor-based validation | Native end-to-end production ownership and remaining compatibility/other-provider data paths |
| Phase 6 | Shared typed transport/binding owner; streamed RTL/XDC | Python store decoding and adapter generation; complete lane/slot/direction/fixed-pin validation through physical implementation |
| Phase 7 | Typed physical handoff; cursor-based bridge/export paths | Remaining Python data-plane ownership and complete NVDLA physical qualification |
| Global STA | Typed segments/results, bounded/file-backed identity and stitching structures | Native input/result ownership, explicit legacy graph materialization, complete original-path coverage and NVDLA WNS/TNS qualification |
| W7 entry point | `emuflow-native` links real stage kernels | No complete `emuflow-native compile`; Python still owns production orchestration and data-plane work |

The physical handoff replacement is implemented; keep it stable while
qualifying it. New representation changes need a named remaining consumer,
an observed cost or a stage-completion dependency. Explicit reference paths
remain useful, but production must not silently fall back to them.

## Acceptance boundary

The prepared-input continuation is debugging/qualification work, not a
substitute for the registered complete-flow acceptance required by AGENTS.md.
Completion requires exact source/config/tool identities, per-phase and total
wall/RSS/I/O evidence, independent logical equivalence, complete physical
coverage and resource checks, and full original-path global timing. Until
those gates and W0/W7 ownership gates pass, the migration remains incomplete.
