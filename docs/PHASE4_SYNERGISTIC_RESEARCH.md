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
one heavy experiment; the user allows using its available memory. Sbms02
initially has about 739 GiB available, but the user limits this study to
128 GB aggregate memory unless additional explicit approval is obtained.
Run at most three independent trials within that total. Observed availability
is not an allocation. Every temporary/cache/build/data path stays in the
existing sandbox, including TMPDIR and Python/compiler cache paths.
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

## First observed result and next action

Unmodified reference commit `34d8a522` compiled on avent with GCC 13 and
ASan/UBSan. Its tiny multicast fixture immediately reports a heap-buffer
overflow in `RouteTree::calculateDelay`, source line 144; missing-directory
and no-argument probes also report sanitizer errors. These are completed
negative controls, not routing passes. On sbms02 the GCC 8 sanitizer build
failed to link a missing system libasan; this is a toolchain issue, not an
algorithm failure. Static sanitizer archives are also absent from its checked
GCC installations. The next sbms probe uses its ordinary compiler with
`--sanitizers none`, while avent supplies sanitizer evidence; no external
environment installation is needed.

The replacement kernel avoids the unsafe reference delay/output routines.
It compiled with standard C++17 headers and passed direct sanitizer fixtures.
The combined existing Phase 4 and new author integration suite passed 35 tests,
including both routers feeding the same current timing-DAG Phase 5, and
Static Exact dependent-return event scheduling. The provider is opt-in while
broader experiments run. The author route-only kernel does not yet optimize
STA criticality; its metadata says so. The common route proxy still uses the
maximum sink delay of a multicast net; P5 and final global timing remain the
authoritative member/event-aware gates.

Next: run bounded public-case A/B tests on both hosts, then act on measured
capacity/ordering failures before scaling. Full physical/global timing and
replacement-default qualification remain pending.
