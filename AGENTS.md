# EmuFlow validation requirements

These requirements apply to all work in this repository.

## README synchronization is mandatory

- Before every push, review all commits being pushed for README impact without
  waiting for a user reminder.  Treat this review as a required pre-push gate.
- Any user-visible change to behavior, defaults, CLI options, schemas,
  providers, algorithms, validation requirements, benchmark availability,
  completion status, artifact locations, limitations, or recommended commands
  must update `README.md` in the same push.  Do not push the implementation
  first and leave documentation for a later milestone.
- README status and QoR statements must describe only evidence that actually
  exists.  Mark incomplete validation as pending or blocked; never infer final
  Phase 7 results from an intermediate artifact.
- If the review concludes that a push has no README impact, state
  `README reviewed: no user-visible change` in the handoff or push summary.
  This exception is for genuinely internal or mechanically equivalent changes,
  not a reason to omit documentation for a new capability or changed contract.
- After every push, verify that the remote branch contains both the intended
  implementation commits and the required README update.  If concurrent work
  advanced the branch, rebase safely and repeat the verification; never force
  push over unrelated work.

## Versioned defaults must not preserve implicit legacy behavior

- When a validated algorithm or policy becomes the default, make that default
  uniform at every user-facing CLI, Python API, and experiment-config entry
  point. Do not add hidden inference such as "an omitted policy plus an old
  depth value means legacy mode" merely to keep parameter-omitting historical
  calls behaving as before. Such inference is ambiguous, undocumented state
  and creates permanent compatibility debt.
- Historical experiment interpretation belongs to the immutable artifact:
  schema version, sealed configuration, policy/provider identifier, hashes,
  and validator. Preserve the ability to read and independently validate old
  artifacts when their schema remains supported; do not preserve their old
  invocation defaults in current producer APIs.
- A legacy algorithm remains available only through an explicit, versioned
  provider/policy and every legacy rerun must name it plus all noncurrent
  parameters explicitly. Tests that exercise legacy semantics must do the
  same. Missing parameters always mean the current documented defaults.
- Prefer one clear breaking default transition with an explicit legacy option
  over conditional fallback logic. If an external compatibility promise truly
  requires an old invocation contract, expose a separately named versioned
  command/config schema rather than making the current command guess intent.

## Default experiment lifecycle: ephemeral intermediates

These rules supersede the opt-in cache rules below for ordinary validation and
QoR experiments.

- Run a complete requested validation in one isolated experiment directory.
  Intermediate Phase 1--7 files may exist while that run is active, but they
  are scratch rather than durable experiment results.
- After the terminal validator passes, retain only the compact final evidence:
  the exact source/config/tool identities, final assignment/route/schedule
  identities needed for audit, Phase 7 physical summary, DRC and unrouted-net
  counts, global Phase 7C WNS/TNS, runtime summary, and the validation seal.
  Remove full placement/routing work directories, duplicated JSON payloads,
  temporary tool products, failed attempts, and other replay-only intermediates.
- Do not build or retain a persistent content-addressed checkpoint DAG merely
  to save time on a future run. Do not run whole-cache inventory, GC, or strong
  rehash passes as part of the normal validation path. Hash each retained final
  artifact once while producing its terminal seal.
- A fair A/B run may share immutable input files inside the same active
  experiment directory, but that sharing must not create a permanent cache of
  every phase. Run only the requested physical seed unless the user explicitly
  requests a variance study.
- On cancellation or failure, first prove that the recorded process tree is no
  longer running, then remove the attempt's scratch and intermediate outputs.
  Preserve only a compact failure summary when it contains actionable evidence.
- Existing final evidence bundles remain immutable. Existing intermediate
  caches may be removed when they are not referenced by another live task;
  never delete another task's active input or final evidence.

## Explicit opt-in checkpoint lifecycle

The content-addressed DAG and checkpoint rules in this section apply only when
the user explicitly requests persistent checkpoint reuse for a particular
experiment. They are not the default execution policy.

When explicitly enabled, these rules cover the complete selected experiment:
correctness and determinism validation, benchmark qualification, algorithm A/B
and ablation studies, scalability and performance measurements, public-contest
evaluation, synthesis and partitioning runs, routing and scheduling studies,
Phase 6 provider comparisons, physical implementation, timing closure, and
complete Phase 1--7 flows. Phase 6 is only one application of this opt-in
policy.

- Before starting any repeated, multistage, expensive, or evidence-producing
  run, express it as a content-addressed DAG through `experiment-cache`.  A
  cheap one-off diagnostic may run outside the DAG only when it will not be
  used as benchmark, qualification, completion, performance, or QoR evidence.
- Decompose the DAG at real reusable boundaries.  Phase 1--5 is not a universal
  cache node: frontend/synthesis, timing preparation, partitioning, system
  routing, and TDM scheduling are separate nodes whenever their implementations
  or inputs can change independently.  Each node must declare exact dependency
  keys, input SHA-256 values, configuration, command/environment contract,
  seed/worker count where relevant, expected artifacts, a measured peak and
  retained-byte estimate, and an independent semantic validator.
- A Git commit is provenance, not cache identity.  Every v2 node must carry a
  portable implementation closure containing the exact source, script, and
  binary files used by execution, and a separate closure for its validator.
  An implementation change invalidates that node and descendants; a validator-
  only change triggers independent revalidation without recomputing output.
  Versioned install and input paths are runtime bindings, not identity: the
  portable command identity replaces each such path with the label of its
  byte-sealed input.  Moving identical bytes must preserve the execution key;
  changing those bytes must change it.  Keep stage-specific producer and
  validator code in stage-specific closure components so a Phase 3-only edit
  cannot invalidate Phase 1 merely because both once shared a source file.
  When independent stage entry points must remain in one Python module, seal a
  recursive canonical-AST symbol closure (`path.py::entrypoint,...`), not the
  whole module.  The closure must include referenced module helpers, constants,
  and imports; unrelated symbols and formatting must not invalidate the stage.
- The DAG implementation and experiment spec must support arbitrary named
  stages and multiple dependencies; it must not hard-code one current flow's
  phase sequence.  Physical lookahead, source preparation, qualification, and
  aggregation are explicit reusable nodes whenever they are shared or have a
  different invalidation boundary.
- Plan before execution.  Inventory existing caches and repository-external
  archives, independently validate compatible prior artifacts, and import
  valid results before submitting work.  A new branch, report, experiment
  label, comparison arm, directory layout, or downstream objective is never by
  itself a reason to recompute an unchanged node.
- Execute only the smallest missing frontier.  A changed input, option, tool,
  or dependency invalidates that node and its descendants, not unrelated
  nodes or valid ancestors.  Never delete or bypass a whole cache as a shortcut
  for targeted invalidation, and never restart completed ancestors merely to
  recover from or resume a downstream failure.
- Cache reuse is authorized only by the complete content identity plus a
  passing independent validator and sealed artifacts.  Names, paths, mtimes,
  logs, a declared `pass` status, or visually similar results are insufficient.
  If a stage has no adequate validator, its result is not reusable evidence
  until that validation gap is repaired.
- Publishing and importing perform the strong content hash and semantic
  validation once.  A cache-resident output must then be read-only and its
  checkpoint manifest non-writable; routine planning may validate that managed
  object from its sealed digest table plus immutable-tree metadata instead of
  rereading multi-gigabyte content.  Explicit checkpoint/evidence validation
  always rehashes content.  External references remain mutable and therefore
  must be strongly rehashed on every reuse boundary.  When a validated import
  already resides under the object store, promote it to a managed immutable
  alias rather than retaining the repeatedly hashed external-reference mode.
- Fair A/B and ablation experiments must share the exact validated upstream
  checkpoints and differ only in the intended variable.  Compute each unique
  baseline once.  Reuse a valid baseline in every later comparison rather than
  rerunning it for symmetry or presentation.
- Preserve failed and partial run evidence outside the repository, then
  re-plan from the last valid checkpoint.  Do not overwrite another attempt's
  artifacts, silently turn a failed attempt into a fresh run directory, or
  publish a partial checkpoint as complete.
- If a stage supports independently validated internal checkpoints, recover in
  a new attempt by copy-on-write materializing the failed tree and resuming
  only from those checked boundaries.  Keep the original failure immutable.
  Finish and independently validate the complete stage artifact before using
  `experiment-cache import`; never register an internal or partial checkpoint
  directly.  Physical-lookahead recovery uses `multi-fpga physical --resume`
  followed by `experiment-stage lookahead-resume`.
- Keep three storage classes physically separate: immutable content-addressed
  checkpoints, append-only per-execution attempts, and self-contained final
  evidence bundles.  Each retry gets a new `attempt-NNNN` directory.  Logs,
  scratch, and failed partial output never become checkpoint artifacts merely
  because they share a parent directory.
- Every declared artifact has a semantic role.  `consumer-checkpoint`,
  `source-input`, and `evidence-critical` are required retention;
  `diagnostic` and `failure-diagnostic` are optional evidence; only
  `regenerable-scratch` is prunable.  File size alone never decides retention.
  In particular, 64 MiB is not a replay limit.
- Every canonical Phase 7 terminal retains its runtime/QoR bundle,
  `physical-summary.json`, and `multi-fpga-physical-flow-report.json` as
  evidence-critical artifacts. The complete per-FPGA placement/routing work
  directory remains diagnostic and may be collected after those reports and
  their independent validation certificate are sealed.
- HPC farms may submit only `ready` cache misses.  Re-plan after every completed
  frontier; skip `reuse` nodes and keep `waiting` nodes blocked on their exact
  dependency keys.  Concurrent tasks require isolated output directories and
  immutable source/tool identities.  Parallelism changes scheduling, not the
  evidence contract, unless the worker count is explicitly part of identity.
  If the whole ready frontier exceeds quota or desired concurrency, use the
  farm compiler's explicit experiment-node subset; never edit a plan or task
  command by hand. Deferred ready nodes retain the same identity for a later
  batch.
- Farm workers use leases and heartbeats.  A silent or expired task is not
  automatically dead: reconciliation must probe its recorded PID on its pinned
  node.  Only an expired lease plus a confirmed-absent process can become
  `retryable`, and the retry must use a new attempt directory.
- A cached checkpoint proves only that one node completed its declared gate.
  It does not by itself prove an end-to-end claim.  Report completion or QoR
  only when every required terminal node and claim-specific validator exists;
  otherwise retain an explicit planned, running, incomplete, or blocked state.
- Experiment specs, plans, farm state, logs, transient paths, and large
  artifacts stay outside the source repository.  Check in only reusable
  schemas, policies, small lawful fixtures, and canonical benchmark registries.
- Force-rerunning an otherwise valid checkpoint is exceptional.  Record the
  explicit reason (for example, nondeterminism replication or measurement
  noise study) and give the repeated run a distinct declared identity; never
  make force-rerun the default behavior of an automation or validation task.
- Cache reclamation is mark-and-sweep, never age/name-based deletion.  Root all
  active v2 plans and explicit pins, inventory and validate objects, then
  generate a sealed GC plan.  Apply it only by the exact approved plan SHA-256;
  abort if any candidate changed or became referenced.  Legacy runs first get
  a read-only migration inventory, then independent validation/import or an
  explicit diagnostic-retention decision.  When a noncanonical legacy case is
  deliberately retired, use `retirement-plan` followed by `retirement-apply`:
  the apply step requires the exact plan SHA-256, rehashes every selected tree
  before any mutation, refuses evidence/archive candidates, and preserves
  marker tombstones plus a non-evidence receipt outside `runs`.  Direct
  age/name/glob-based deletion remains forbidden.
- Legacy validation farms remain retirement-protected while any task state can
  still run or retry, is malformed, or has not been reconciled. An expired
  lease alone never permits cleanup. Only farms whose task states are all final
  `pass` or `failed` may enter a retirement plan, and the planner/apply path
  must hold every farm `launch.lock` while sealing and committing an explicit
  retirement marker. Launchers must refuse that marker before and after lock
  acquisition. Close all lock descriptors before tree removal so NFS cannot
  retain `.nfs*` lock files; the marker, not an open descriptor, protects the
  transaction. While still locked, atomically rename each selected top-level
  tree into its sealed quarantine path; recursively remove only that path after
  closing locks. A partial removal must never restore the original launch path.
- A final evidence bundle recursively contains every required artifact for its
  terminal nodes and ancestors and must validate after the source cache is
  unavailable.  A legacy archive containing any hash-only run file is not
  replay-complete and must never authorize deletion of its source run.
- Validate experiment-management semantics on a small deterministic design
  first.  That gate must exercise cache hits and misses, selective
  invalidation, relocation of byte-identical tools, validator-only changes,
  tampering, failed attempts, lease expiry/reconciliation, storage blocking,
  evidence replay without the cache, and reference-aware GC.  Do not use a
  large design to debug these control-plane rules.  After the small-design gate
  passes, run one canonical medium/large complete flow to validate production
  scale, long leases, large checkpoints, cross-node recovery, quota estimates,
  and final Phase 7 QoR; reuse its validated ancestors for all comparisons.

## Mandatory experiment storage boundary

- Every EmuFlow-controlled writable path on the validation servers must be
  located below `/research/d4/gds/ziyiwang21`.  This includes run directories,
  content-addressed caches, staging areas, temporary directories, farm state,
  logs, build scratch, extracted inputs, checkpoints, archives, reports, and
  tool-generated physical work directories.
- Do not use node-local or alternate storage for EmuFlow work.  In particular,
  `/dev/shm`, `/tmp`, `/var/tmp`, `/uac`, home-directory scratch, and any path
  outside `/research/d4/gds/ziyiwang21` are prohibited for experiment outputs
  or temporary artifacts, even as a quota workaround or performance
  optimization.  Set `TMPDIR` and tool-specific scratch variables to a unique
  directory under the required `/research` root when needed.
- Never silently fall back to another filesystem.  Before launching an
  expensive DAG frontier, check the user quota and estimate the frontier's
  peak retained plus temporary footprint.  If the available quota is
  insufficient, keep the frontier blocked, report the storage requirement,
  and reclaim space only through the validated archive/retention process.
- Storage cleanup must remain evidence-aware.  Preserve sealed final reports,
  manifests, hashes, placement/route artifacts required for replay, and the
  minimal valid checkpoints.  Classify and obtain an explicit safe cleanup
  set before removing regenerable physical scratch, duplicated ancestors,
  obsolete failed staging, or redundant captures; never delete unrelated
  tasks or unvalidated evidence merely to make a run fit.

## Managed-flow hot paths must stay compact

These rules apply to every producer, validator, checkpoint, and downstream
consumer in the managed Phase 1--7 flow.  They are correctness and scalability
requirements, not optional micro-optimizations.

- Give each logical fact one canonical owner.  Do not copy the same assignment,
  timing population, route table, instance map, semantic contract, or report
  payload into several artifacts merely for downstream convenience.  Other
  artifacts reference the canonical object by a stable identity and seal, and
  reconstruct derivable views only when a consumer actually needs them.
- Separate consumer interfaces from evidence and diagnostics.  A hot-path
  checkpoint contains the minimal information required by its declared
  consumers.  Detailed traces, candidate enumerations, exhaustive oracle
  state, duplicated reports, profiling counters, and human-readable dumps are
  diagnostic artifacts; they must not become mandatory inputs to every later
  phase.  A diagnostic field may enter the hot path only when a named consumer
  uses it semantically and that dependency is covered by tests.
- Large structured interfaces must use deterministic compact storage or a
  purpose-built indexed/sharded representation.  Avoid repeated object keys,
  repeated instance-to-cluster/partition maps, pretty-printed duplicate trees,
  and monolithic JSON scans when the irreducible vector plus canonical source
  data can reconstruct the same logical document.  Storage envelopes must
  preserve the logical schema, validate bounded decompression and exact row
  counts, and round-trip deterministically.
- Do not force a downstream stage to parse a large artifact merely to read a
  small summary.  Publish a compact manifest/index for frequent queries and
  load or expand the full payload only at the stage that consumes it.  Never
  derive a tiny scheduling, routing, or timing input by repeatedly walking an
  unrelated diagnostic report.
- Hash canonical bytes once while producing or importing an artifact whenever
  possible.  Do not materialize a second serialized copy solely to compute its
  identity, and do not repeatedly rehash an immutable managed object at every
  downstream boundary.  Publication/import performs the strong content hash
  and semantic validation; routine managed consumers verify the sealed
  manifest, immutable-object metadata, required dependency keys, and their
  stage-specific invariants.  Explicit evidence validation, validator-version
  changes, mutable external inputs, and final replay gates still perform the
  required strong rehash or semantic reconstruction.
- Production Phase 3 uses the scalable native optimizer plus the linear output
  contract.  Global-best move replay, full native-input/output hashing,
  exhaustive Python optimization, and complete candidate traces belong to
  small-graph algorithm qualification or explicitly requested offline studies,
  not to every managed large-design execution.
- A representation-only optimization must not change logical schemas, QoR,
  assignment, scheduling, routing, or validation strength.  Add round-trip,
  corruption, truncation, relocation, and mixed-run rejection tests, plus a
  regression that exercises a realistically large repeated structure and
  guards against restoring duplicate hot-path payloads or whole-document
  parsing.
- Before adding any field or validation pass to a managed artifact, state its
  canonical owner, which consumer requires it, whether it is derivable, when it
  is hashed, and why the check cannot reuse an existing validated certificate.
  If those answers are absent, keep the data out of the production hot path.

## End-to-end acceptance is mandatory

- A Phase 6 algorithm, provider, optimization, or default-selection change is
  not complete when only Phase 6 artifacts or proxy metrics have passed.  The
  required acceptance endpoint is the completed physical Phase 7 flow.
- The primary QoR results are the final aggregate WNS and TNS after Phase 7.
  Phase 6 metrics such as crossing bits, SLL crossings, grouping objective,
  RUDY, position SSE, estimated wirelength, and pin distance are diagnostic
  intermediate metrics.  They must be reported when useful, but they must not
  replace or be presented as final timing QoR.
- A validation report or milestone must not say that a Phase 6 change has
  completed full-flow validation unless both the baseline and candidate have
  successfully completed Phase 7 and their final WNS/TNS have been compared.

## Default timing-QoR terminology is system-global

- `emuflow multi-fpga compile` must enable timing-driven Phase 3--5
  optimization by default.  A user may explicitly select
  `--no-timing-driven` for an algorithmic baseline, but every physical flow
  must still generate, project, preserve, and independently validate the
  complete original TimingPathDB needed by Phase 7C.  Disabling optimization
  must never disable timing analysis or silently fall back to per-FPGA-only
  WNS/TNS.
- Physical execution requires explicit target periods for every analyzed
  clock.  Reject a missing period before starting partitioning or physical
  implementation; never invent a clock period or wait until Phase 7C to
  discover that routes lack timing paths.

- Unless a report explicitly qualifies the scope, `WNS` and `TNS` mean the
  whole-original-design timing result after Phase 7, including both paths
  whose endpoints remain on one FPGA and paths that cross one or more FPGAs.
  For cross-FPGA paths, the result must compose routed intra-FPGA logic and
  boundary delay with the concrete Phase 5 slot wait and board-link delay.
  For same-FPGA paths, it must use the corresponding post-route local path
  delay.  Together these two disjoint sets must cover every original
  TimingPathDB path exactly once.
- `global WNS` is the minimum composed slack over all original design paths.
  `global TNS` is the sum of every negative composed path slack, counted once
  per original TimingPathDB path.  A timing-equivalent representative used by
  an optimizer may prove WNS, but it must be expanded to its original members
  before TNS is accumulated.
- WNS/TNS reported by an individual FPGA backend, or an aggregate formed only
  from per-FPGA endpoint reports, must be labelled `per-FPGA physical WNS/TNS`.
  It is a physical diagnostic and must never be presented as the default or
  final global design timing result.
- WNS/TNS formed only from cross-FPGA paths must be labelled
  `cross-FPGA-path-subset WNS/TNS`.  Crossing the board does not by itself make
  that subset a whole-design result.  It must never be labelled `global` unless
  the same-FPGA path population is also included and exact set coverage of the
  complete original TimingPathDB is independently verified.
- Every final global timing claim must report original-path coverage,
  compressed-representative coverage, physical-delay exactness/bound status,
  target-clock and virtual-runtime-clock WNS/TNS, and negative-path counts.
  Missing complete original-member coverage makes final WNS/TNS validation
  incomplete rather than implicitly zero or equal to a per-FPGA aggregate.

## Required Phase 7 A/B comparison

For every Phase 6 QoR claim or default-provider promotion:

1. Use a real synthesizable RTL or gate-level EmuIR design with real logic,
   clocks, and timing constraints.  A contest communication graph, virtual-die
   placement, pin-plan-only bundle, or synthetic connectivity graph is not a
   valid final timing benchmark.
2. Freeze and hash the common upstream EmuIR, Phase 3 assignment, Phase 4
   routes, Phase 5 schedule, BoardDB, architecture/device, constraints, tool
   versions, seed, and relevant physical-flow options.
3. Materialize separate canonical Phase 6 splits for the frozen baseline and
   candidate.  Both splits must pass their normal independent legality,
   electrical-binding, equivalence, and artifact validation gates.
4. Run the complete Phase 7 physical flow for both sides using identical
   backend settings.  Zero unrouted nets, zero DRC violations, complete cell
   accounting, and passed timing-result validation are required before QoR is
   compared.
5. Report at least:
   - explicitly labelled per-FPGA physical WNS and TNS diagnostics;
   - global WNS over all composed original TimingPathDB paths;
   - global TNS as the sum of negative composed slack over all original
     TimingPathDB paths, without representative compression or double
     counting;
   - the absolute baseline-to-candidate change for WNS and TNS, where a
     positive slack delta is an improvement;
   - percentage improvement computed from negative-slack deficit reduction,
     not by dividing signed slack values.  If the baseline is already
     non-negative (WNS) or zero (TNS), report the percentage as N/A.  Report a
     transition across timing closure separately;
   - failing endpoint counts, critical path, runtime, unrouted nets, and DRC
     violations.
6. Preserve the reports and source hashes in a sealed, independently
   replayable bundle.  Intermediate and final results must identify their
   qualification and claim boundary (open academic model, vendor result, or
   hardware closure).

If the selected physical backend does not expose enough timing data to compute
and independently validate TNS, the validation is incomplete.  Implement or
repair TNS extraction and validation before making a final QoR claim; do not
substitute sampled paths, WNS, critical path, or a Phase 6 proxy for TNS.

## Benchmark and execution policy

- `benchmarks/end_to_end_validation_matrix.json` is the sole registry for
  provider comparisons and complete Phase 1--7 WNS/TNS claims.  Ad-hoc runs
  may diagnose a bug, but they must not be reported as benchmark evidence.
- Every registered full-flow case has two independently named axes:
  `workload` is a naturally connected, hash-pinned upstream RTL design and
  `platform` is a hash-pinned public-contest case materialized as BoardDB.
  The workload supplies cells/nets and the contest case supplies only FPGA
  topology and link capacities.  Never feed contest communication nodes to
  synthesis or describe a raw contest-graph result as a physical RTL run.
- Always identify a run by the canonical `<workload>__<suite>-<case>` ID.  A
  bare label such as `case6`, `case07`, or `NVDLA run` is ambiguous and is not
  acceptable in reports, manifests, filenames, or user-facing summaries.
- The canonical initial QoR set is Koios DLA medium combined separately with
  EDA 2023 case6, case7, and case9 BoardDBs.  Case6 is the primary QoR case;
  case7 and case9 are topology replications.  Adding or replacing a case
  requires updating the versioned matrix and its validator tests first.
- The raw public-contest coverage plan remains separately recorded in
  `benchmarks/contest_validation_matrix.json`.  Passing fetch/import/evaluate
  for that matrix proves a communication-algorithm gate only; it never
  promotes an entry in the end-to-end matrix.
- A matrix entry in `planned` or `blocked` state is not evidence. `qualified`
  requires a terminally sealed complete-flow manifest for all required
  providers, physical seeds, gates, global timing metrics, DRC, and unrouted-net
  checks. Persistent replay checkpoints are not required. Repository
  configuration must never contain transient server paths.
- Baseline, placement-aware, and Chimew Phase 6 arms must use identical frozen
  source, BoardDB, Phase 1/3/4/5 artifacts, physical backend/options, worker
  count, and physical seed.  The default acceptance seed is fixed to 1; seeds
  2/3 are an explicit statistical-robustness opt-in and are not a routine
  completion gate.  Only the Phase 6 provider may differ.
- Canonical whole-design timing uses the target period recorded in the
  versioned workload run spec. A transient experiment config may not choose or
  relabel that period; changing it is a new benchmark contract, not a rerun.
- The run spec also fixes the physical frontend mapping profile independently
  of any vendor-synthesis policy. Canonical compilation must consume that
  value; it must not silently switch between generic-soft and VTR hard blocks.
- The primary final QoR is whole-design target-clock WNS and TNS after Phase
  7/7C.  Per-FPGA WNS/TNS, Phase 6 cost, crossings, RUDY, and congestion are
  diagnostics and must not be substituted for the primary metrics.
- Canonical provider studies finish at a terminal `qor-comparison` certificate,
  not at an individual Phase 7 arm. It compares every
  baseline/placement-aware/Chimew arm for the configured physical seed set,
  rechecks their common frozen Phase 1/3/4/5 hashes, and preserves paired
  target-clock WNS/TNS deltas and per-provider statistics in final evidence.
  Use seed 1 alone by default; request multiple seeds only when measuring
  physical-tool variance rather than validating functionality or a first QoR
  result.
- Small fixtures are suitable for correctness and determinism tests, but a
  default algorithm or QoR claim also requires a materially sized real design.
- Replicated-core or artificially coupled RTL harnesses are not accepted as
  benchmark-catalog entries or as evidence for provider promotion and final
  WNS/TNS claims. Use a naturally connected upstream RTL design for those
  decisions.
- A provider comparison runs each arm in an isolated one-shot directory and
  removes its Phase 1→7 intermediates after terminal validation. Arms may share
  immutable source inputs during the active comparison, but must not publish a
  persistent intermediate DAG. Baseline Phase 6 consumes Phase 5 directly. The
  fixed physical lookahead consumes baseline Phase 6; placement-aware and
  Chimew consume Phase 5 plus that lookahead. Never coerce an incompatible
  communication-only artifact into a fake physical netlist.
- Run canonical validation through the one-command full-flow interface in an
  isolated directory; do not decompose an ordinary run into published
  `experiment-stage` checkpoints or a persistent provider/seed DAG. The run
  must bind its case to the checked-in end-to-end matrix and verify the run-spec
  RTL/top/clocks plus the contest BoardDB and route-constraints materialization
  report. Route/hop/TDM limits from that report must feed and be independently
  checked at Phase 3, Phase 4, and Phase 5; an arbitrary platform or default
  ratio quantum is not acceptable. For an A/B study, launch complete arms on
  separate nodes with the same source, options, and seed, then compare only
  their terminal evidence.
- A post-partition OpenSTA directed query is complete only with sealed
  per-cut-net driver/query/emission evidence and an independently rebuilt
  EmuIR-plus-timing-model endpoint-reachability classification.  Never silence
  a missing cut net by dropping the coverage assertion or by treating a
  producer-reported zero-path result as self-authenticating.
- The directed through-cut database is qualification evidence, not the Phase
  4/5 timing population.  Project every cross-partition path from the complete
  pre-partition TimingPathDB, seal that database SHA in the cut-timing
  checkpoint, and use the same member-ID namespace for Phase 4 routing and
  Phase 7 physical logic-segment reconstruction.  A bounded through-net query
  must never silently truncate final whole-design WNS/TNS coverage.
- OpenPARF legalization locks must project through the electric-potential,
  multiplier, gradient, and step-size calculations.  An empty active
  placement subspace is a no-op/termination condition, not a reason to divide
  a zero gradient or to hide NaN with a broad epsilon clamp.
- Do not register ordinary validation outputs in `experiment-cache`, and do not
  create a new persistent checkpoint graph to avoid rerunning a baseline. Keep
  only terminal evidence from the current comparison. The import/re-plan rules
  in the explicit opt-in checkpoint section apply only when the user requests
  persistent replay checkpoints. A changed Phase 6 option requires a fresh
  complete arm; a changed RTL or BoardDB defines a different experiment.
- Independent A/B runs may and should use different HPC nodes concurrently.
  Each run must use an isolated output directory and the same immutable source
  commit and versioned tool installation.
- A Phase 7 run may be parallelized across FPGAs, provided aggregation remains
  deterministic and the A/B worker configuration is recorded and equivalent.
- A task must remain explicitly pending or blocked until the required Phase 7
  WNS/TNS evidence exists.  Passing Phase 6 alone is not completion.
