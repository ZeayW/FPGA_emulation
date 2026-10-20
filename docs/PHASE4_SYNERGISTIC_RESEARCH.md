# Phase 4 author router replacement

The objective is to replace the existing Phase 4 system-routing algorithm with
the author-supplied Synergistic Die-Level Router algorithm, repair concrete
defects, and establish its behavior in the complete EmuFlow flow. Originality
of the algorithm is not an acceptance condition. Work is confined to
`feature/phase4-synergistic-router`; main and all other branches are read-only.

## Starting evidence and first experiment

The baseline is main `f4af25744e67553348343916926a503ad03768cd`.
On 2026-10-20 the related Phase 3 native migration branch reached
`3c16e4a489e27e05555547d7e261b129f98d3fdb`. Its indexed semantic publication is
a compatibility target, not a hot update of the sealed baseline. Existing
PPro bank and stream-integration branches are earlier ancestors/reference
work; the baseline already contains the HP-bank model and indexed handoffs.

The current router has a paper-informed balanced-tree candidate, introduced
on 2026-08-02. The author archive provides a distinct implementation whose
normal parser collapses each FPGA to one node and whose top-level run also
executes TDM assignment. The first replacement therefore extracts the author's
connection ordering and shared-tree maze search as an explicit route provider,
feeding the existing Phase 5. It preserves source/bit/sink identities, board
capacity domains, bank direction, hop limits and Static Exact dependencies.
No current TLR candidate or hidden fallback may generate the author result.

The unmodified source receipt is
`engines/synergistic_author_reference/SOURCE.json`. The archive supplied no
license notice; its provenance is retained without assigning an invented
license. Source auditing found uninitialized net line IDs, mismatched sorted
path/delay output, unchecked search failure, possible negative delay indexing,
and a vector-reference invalidation hazard. The first server experiment builds
and runs the unmodified reference with sanitizers before evaluating the adapter.

## Current short stage

1. Run the unchanged reference on both Linux hosts with bounded tiny fixtures;
   save compiler, command, exit status and sanitizer evidence.
2. Implement `synergistic-author-route-v1` as a standalone native kernel and
   canonical Python adapter. Author routing remains distinguishable from the
   current router; Phase 5 initially stays identical.
3. Test multicast sharing, SLL/cable differences, asymmetric/parallel links,
   hop bounds, bank capacity, disconnected graphs and invalid protocol output.
4. Run public EDA2023 cases 1-5 on both hosts, with baseline and author arms
   consuming the same normalized inputs and the same downstream TDM method.
   Then use cases 6-8 for scale and the real counter/SERV/PicoRV32 ladder for
   nonzero-cut flow integration before medium physical comparisons.
5. Attribute failures, implement the smallest justified repair, push an exact
   new commit, and rerun affected tests. Do not wait through clearly stalled
   tiny tests. Extend to author TDM only after the routing boundary works.

## Runtime policy

Avent has about 28 GiB available at the initial observation and runs at most
one heavy experiment. Sbms02 initially has about 739 GiB available and runs
at most three independent trials. These are observations, not reservations.
Reuse existing native tools and immutable completed inputs. Each experiment
has a unique output directory and an exact pushed source commit. Small-test
timeouts begin at seconds/minutes; medium budgets are measured from observed
progress. One to two hours without a useful stage advance triggers immediate
analysis and evidence-based stop/revision. Sampling RSS is not a hard cap.

An ACTIVE thread heartbeat starts at 15 minutes and is adjusted to experiment
duration. The supervisor records process groups and exit state independently
of SSH. Only registered study process groups may be stopped. Every completed
or failed trial must lead to a recorded conclusion and next action. Upstream
changes are checked every few cycles and adopted only in a separately sealed
compatibility change.

## Acceptance

Algorithm diagnostics include routing legality, independent timing
reconstruction, TDM scheduling, critical connection delay, time and RSS.
Final qualification additionally requires real complete RTL, actual nonzero
cross-FPGA communication, normal transport/equivalence validation, complete
physical implementation, zero unrouted nets/DRC, and whole-design Phase 7C
timing with original-path coverage and exact/bound status. A completed flow
with negative slack is not timing closure. A contest graph is not a physical
RTL result. Routes and schedules intentionally differ across Phase 4 arms;
all common inputs, downstream algorithms, seeds and physical settings stay fixed.

The current status is development, with server reference probes pending.
No routing QoR or full-flow success is claimed by this initial source commit.
