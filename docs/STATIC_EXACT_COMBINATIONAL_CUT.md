# Static exact combinational-cut mode

## Status and claim boundary

The production flow remains `sequential-only` pending real QoR promotion. The
legacy opt-in depth-1/depth-2 path and generalized v2 path now pass Phase 3
partition legality, Phase 4 native-route contract propagation,
and Phase 5 dependency/capture scheduling. Safe-mode Phase 3 transports
register outputs, transport-safe register inputs, and replicated primary
inputs; other combinational connectivity remains atomic. The checked-in
`combinational-cut characterize` command is read-only. It identifies a
conservative LUT-only eligibility upper bound, combinational SCCs, potential
cut dependencies, and atomic-component reductions. It does **not** change a
partition, create a transport schedule, establish macro-cycle equivalence, or
claim physical timing closure.

The opt-in Phase 3 mode `static-exact-combinational` has two explicit candidate
policies. `potential-frontier-depth-v1` preserves the historical depth-1/2
behavior. `assignment-derived-acyclic-v2` releases every structurally legal
candidate, reconstructs dependency depth from the selected assignment, accepts
any positive safety cap, and emits the v2 semantic contract. Phase 4 binds that
contract through native routing. Phase 5
uses it to prove when downstream combinational values become available and
when terminal captures are ready. Phase 6 now materializes contract-bound,
preserved TX/RX boundaries and shadow-only consumer nets, then evaluates TX
values from the state, primary inputs, and shadows visible at the scheduled
slot. It requires three deterministic random traces and, for models within a
12-variable limit, complete one-step state/input enumeration. This is
functional evidence, not physical timing closure: Phase 7C routed segment
deadlines remain required. Merely adding `combinational` to the old legal-cut
constant remains invalid.

## Slot-edge convention

All exact-mode artifacts use `fabric-rising-edge-current-slot/v1`:

- A TX assigned slot `S` samples its source at the fabric rising edge for
  which the controller's pre-edge value is `S`.
- An RX assigned arrival slot `A` updates its shadow register at the fabric
  rising edge for which the pre-edge value is `A`.
- A value captured or architecturally launched at edge `E` with a declared
  combinational budget of `B` slots is first eligible for downstream sampling
  at edge `E+B`. Consequently a one-slot relay budget requires
  `next_tx_slot >= arrival_slot + 1`.
- A cross-FPGA `register_output` is an architectural launch under this rule:
  its routed clock-to-Q, local net, and TX-input path consume the configured
  launch-to-TX budget. It therefore cannot be scheduled for TX in slot zero
  merely because its driver is a register.
- The virtual DUT commits at the rising edge whose pre-edge slot is
  `frame_slots-1`. A capture value may become ready at that edge; its physical
  delay budget must include setup/uncertainty, so no unmodelled setup window is
  implied.
- A transport arrival itself remains strictly before the commit slot, matching
  the existing runtime barrier contract.

This convention matches the current generated TX combinational mux, RX
`always_ff` shadow capture, relay `arrival+1` rule, and frame-barrier
virtual-clock enable. Scheduler, independent validator, RTL tests, and
Phase 7C must consume the same versioned convention rather than defining
local `+1` rules.

## Semantic contract

Phase 3 emits one versioned sub-contract. The implemented opt-in path binds it
through routes, schedule, Phase 6 split, and Phase 7C:

```json
{
  "schema": "emuflow.static-exact-combinational-cut/v2",
  "mode": "static-exact-combinational",
  "candidate_selection_policy": "assignment-derived-acyclic-v2",
  "max_cross_fpga_dependency_depth": 8,
  "comb_segment_budget_slots": 1,
  "slot_edge_convention": {
    "id": "fabric-rising-edge-current-slot/v1"
  },
  "cut_nodes": [],
  "dependency_edges": [],
  "logic_segments": [],
  "capture_requirements": [],
  "metrics": {},
  "source_identity": {}
}
```

Functional dependency and capture coverage come from complete EmuIR
connectivity. TimingPathDB associates delay and QoR evidence, but a truncated
timing-path sample can never define functional coverage.

## Conservative eligibility policy

The first version is intentionally fail-closed:

- exactly one virtual DUT clock is required; zero-clock, multi-clock,
  generated-clock, and general CDC designs are rejected before exact cuts are
  released;
- only single-driver, acyclic, mapped LUT soft logic is potentially cuttable;
- FF/memory state, DSP/carry/memory cascades, clock/reset, multi-driver nets,
  latches, opaque primitives, asynchronous controls, and combinational SCCs
  remain atomic;
- top-level output capture and supported synchronous FF data/control inputs
  are valid terminal boundaries;
- physical capture records preserve the reached state input pin and bit; the
  physical query translates it to the corresponding lowered primitive pin
  (for example VTR RAM `data1[bit]`) and rejects any absent pin before
  physical routing starts;
- every reconvergent predecessor is retained;
- a source cone that reconverges transported predecessors with a local
  architectural register, memory, or primary-input launch retains both the
  `rx_to_tx` and `launch_to_tx` timing branches; predecessor presence never
  suppresses the local launch branch;
- the dependency-depth limit is reconstructed from EmuIR after each candidate
  assignment, independent of the partition provider.
- v2 never filters a candidate using its depth in the graph of all *possible*
  cuts. A deep potential candidate can be the only selected boundary in its
  cone and therefore have actual depth one;
- v2 independently computes an uncongested BoardDB minimum-latency lower bound
  over the selected dependency DAG. An assignment that cannot reach every
  terminal capture before commit even under this optimistic bound is rejected
  in Phase 3. Concrete routing, contention, lane capacity, and physical delay
  remain stricter downstream gates.

The characterization report is an upper bound because it ignores capacity,
user group/fixed constraints, BoardDB hop limits, link capacity, schedule
feasibility, and physical segment deadlines.

## Delivery sequence

1. **Characterization (implemented, no behavior change).** Read-only SCC,
   eligibility, dependency-depth, and theoretical atomic-component report;
   independent exact replay; tamper tests.
2. **Phase 3 legacy depth 1/2 plus generalized v2 (implemented, opt-in).** Explicit cut policy, assignment
   semantic contract, provider-independent cluster/legality reconstruction,
   and balance fixture. Its strongest qualification is
   `partition-legality-only-provisional`.
3. **Phase 4/5 arbitrary budgeted DAG depth (implemented, opt-in).** Exact contract propagation,
   canonical contract digest binding, deterministic
   dependency-aware list scheduling, source-ready/capture certificate, fixed
   frame fail-closed diagnostics, and tamper tests.
4. **Phase 6 (implemented, opt-in).** Contract-bound exact boundaries,
   hidden-bypass rejection, deterministic shadow startup, event-driven
   macro-cycle simulation, complete small-model one-step enumeration, and a
   canonical Yosys formal miter fixture. General-design formal closure is not
   claimed.
5. **Phase 7C (implemented and independently accepted on real routed DLA).**
   Contract-bound routed `launch_to_tx`, `rx_to_tx`, and `rx_to_capture`
   evidence, independent causal deadline reconstruction, explicit missing-
   evidence incompleteness, and global target-clock/virtual-runtime WNS/TNS.
   The public `schemas/static-exact-segment-deadlines-v1.schema.json` contract
   fixes the complete routed-deadline report surface. The large acceptance
   selects five natural combinational cuts without fixed partition placement,
   covers all 157,811 contract segments with endpoint-exact routed evidence,
   and replays all 195,532 original timing paths. Its negative 10 ns
   target-clock WNS/TNS is reported honestly; the gate proves complete timing
   evidence and positive causal segment deadlines, not target-clock closure.
6. **Optimizer integration.** TritonPart screens every candidate assignment
   against the reconstructed contract and the BoardDB lower bound before seed
   selection. The dedicated exact scheduler is a generic topological,
   capacity-aware list scheduler; it is no longer limited to depth two. The
   ordinary timing-DAG/ratio optimizers remain fail-closed until they consume
   the same dependency contract.

### Cross-policy Phase 7 promotion certificate

Sequential-only, legacy Static Exact v1, and generalized Static Exact v2 do
not share Phase 3--5 artifacts.  They must therefore be three branches from
the same content-addressed frontend/timing inputs, not three Phase 6 providers
under one shared assignment.  Compile that complete DAG with:

```bash
emuflow benchmark-static-exact-ab-compile \
  --config "$CANONICAL_CASE_CONFIG" --repository-root "$SOURCE" \
  --legacy-max-depth 2 --generalized-max-depth 8 \
  --minimum-combinational-cut-nets 1 \
  --out "$EXPERIMENT/static-exact-ab-spec.json"
```

The compiler retains one frontend and one complete TimingPathDB node, forks
the partition/route/TDM/lookahead/split/physical nodes by cut policy, and adds
the comparison as the sole terminal.  Its final producer/validator command is:

```bash
emuflow experiment-stage static-exact-qor-compare-run \
  --platform "$PLATFORM" \
  --arm sequential-only 1 "$SEQ_SHARED" "$SEQ_LOOKAHEAD" "$SEQ_PHASE6" "$SEQ_PHASE7" \
  --arm legacy-static-exact-v1 1 "$V1_SHARED" "$V1_LOOKAHEAD" "$V1_PHASE6" "$V1_PHASE7" \
  --arm generalized-static-exact-v2 1 "$V2_SHARED" "$V2_LOOKAHEAD" "$V2_PHASE6" "$V2_PHASE7" \
  --out "$EVIDENCE/static-exact-qor"

emuflow experiment-stage static-exact-qor-compare-validate \
  "$EVIDENCE/static-exact-qor" --platform "$PLATFORM" \
  --arm sequential-only 1 "$SEQ_SHARED" "$SEQ_LOOKAHEAD" "$SEQ_PHASE6" "$SEQ_PHASE7" \
  --arm legacy-static-exact-v1 1 "$V1_SHARED" "$V1_LOOKAHEAD" "$V1_PHASE6" "$V1_PHASE7" \
  --arm generalized-static-exact-v2 1 "$V2_SHARED" "$V2_LOOKAHEAD" "$V2_PHASE6" "$V2_PHASE7"
```

The checker independently replays each complete terminal chain, binds the
three arms to identical source/timing/platform inputs and physical settings,
and compares whole-original-design target-clock and virtual-runtime WNS/TNS.
It also records exact-cut count/depth, virtual frequency, transport and total
physical cells, cut nets, scheduled bit-hops, frame size, and completion slot.
New managed checkpoints seal each node's measured wall time.  The comparison
therefore also reports partition, routing, TDM, Phase 6, physical-lookahead,
and Phase 7 wall times plus paired physical and Phase 3--7 totals.  These
measurements come from immutable checkpoint manifests, not mutable farm task
state; a historical imported checkpoint without runtime metadata remains
valid but is not runtime-qualified.
The generated promotion gate is false unless v2 exercises at least one real
combinational cut and improves the paired target-clock result over
sequential-only.  A single physical seed is the routine gate; more seeds are
an explicit robustness study.

## Commands

```bash
emuflow combinational-cut characterize \
  --ir build/phase1/design.emuir.json \
  --depth-limit 1 --depth-limit 2 \
  --output build/comb-cut/characterization.json

emuflow combinational-cut validate \
  --ir build/phase1/design.emuir.json \
  build/comb-cut/characterization.json

emuflow phase3 \
  --ir build/phase1/design.emuir.json \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --provider greedy \
  --cut-mode static-exact-combinational \
  --static-exact-candidate-policy assignment-derived-acyclic-v2 \
  --max-cross-fpga-dependency-depth 8 \
  --comb-segment-budget-slots 1 \
  --out build/phase3-exact

emuflow phase4 \
  --assignment build/phase3-exact/assignment.json \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --provider native-load-balanced-v1 \
  --out build/phase4-exact

emuflow phase5 \
  --routes build/phase4-exact/routes.json \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --out build/phase5-exact

emuflow schedule validate \
  build/phase5-exact/schedule.json \
  --routes build/phase4-exact/routes.json \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json

emuflow phase6 \
  --ir build/phase1/design.emuir.json \
  --assignment build/phase3-exact/assignment.json \
  --schedule build/phase5-exact/schedule.json \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --out build/phase6-exact

emuflow multi-fpga compile design.v \
  --top top --clock clk --clock-period clk=10 \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --cut-mode static-exact-combinational \
  --static-exact-candidate-policy assignment-derived-acyclic-v2 \
  --max-cross-fpga-dependency-depth 8 \
  --comb-segment-budget-slots 1 \
  --frame-slots 32 --physical --out build/exact-flow

emuflow multi-fpga validate \
  --flow build/exact-flow \
  --minimum-combinational-cut-nets 1 \
  --require-physical
```

`multi-fpga validate` is the independent full-root acceptance gate. It hashes
the declared artifacts again, replays the Phase 3--6 validators and static
exact equivalence evidence, reconstructs the runtime contract, and reruns
Phase 7C QoR from the retained physical summary. `--require-physical` prevents
a generated-only Phase 7C bundle from satisfying the real acceptance gate.

For retained evidence, the same policy is available through the canonical
Experiment v2 compiler by adding these fields to the case config:

```json
{
  "cut_mode": "static-exact-combinational",
  "static_exact_candidate_policy": "assignment-derived-acyclic-v2",
  "max_cross_fpga_dependency_depth": 8,
  "comb_segment_budget_slots": 1,
  "minimum_combinational_cut_nets": 1
}
```

Those values are part of the Phase 3 producer and validator identities.  The
compiler fixes Phase 4 to the native route tree with post-route timing
annotation, Phase 5 to the exact dependency scheduler, omits ratio optimizers,
and makes the ordinary CLI slot-refinement default zero for this mode. An
explicit nonzero request is still rejected until that optimizer is dependency-
qualified. The compiler produces one independently sealed physical Phase 7
terminal per provider at seed 1 by default. Seeds 2 and 3 remain an explicit
statistical-robustness opt-in rather than a routine completion gate. Canonical
static-exact runs default
`minimum_combinational_cut_nets` to zero because some real, legal partitions
need no combinational boundary. The producer records the selected threshold,
the separately invoked Phase 3 validator reconstructs the actual count, and
the report records whether a combinational cut was actually exercised. A
positive threshold is an explicit exercise contract, used by the small
capacity-limited acceptance fixture; a zero-cut large run is compatible
evidence, not an exercised exact-cut result.

The canonical three-policy QoR experiment makes this distinction explicit.
Its sequential arm requires zero combinational cuts, its legacy v1 arm uses a
zero minimum and becomes a `vacuous-negative-control` when the historical
potential-frontier filter releases no selected boundary, and its generalized
v2 arm alone inherits the requested positive exercise threshold. All three
arms still complete Phase 1--7 so compatibility, runtime, resources, and final
whole-design WNS/TNS remain comparable. Only an exercised generalized-v2 arm
can satisfy the default-promotion gate.

`examples/rtl/static_exact_acceptance.v` is the small real-RTL acceptance
source. Its 33-input next-state parity needs at least seven 6-input LUTs, while
each FPGA in
`platforms/virtual/static_exact_acceptance_2fpga.json` exposes only six LUTs
after utilization derating. Sequential-only clustering therefore cannot place
the atomic combinational cone, whereas exact mode must select and validate an
actual internal LUT-to-LUT cut. This pair exists only for functional and open
physical Phase 1--7 acceptance; it is explicitly not a QoR benchmark.

Characterization is deterministic and near-linear apart from sorting. Its
canonical EmuIR hash preserves the existing pretty-JSON byte identity through
incremental encoder chunks rather than allocating another complete serialized
design in memory. The
validator reconstructs the complete report from EmuIR and rejects changed
eligibility, SCC, dependency, depth, source identity, or metric fields.
The Phase 3 validator also regenerates the exact cluster release policy and
semantic contract from EmuIR, PlatformDB, normalized constraints, and the
instance assignment; provider output cannot self-declare eligibility.
The Phase 4 checker additionally binds every routed demand to the exact cut
node, and the Phase 5 checker independently reconstructs the complete
dependency/capture certificate. Phase 6 independently rebuilds the split and
replays event-driven macro-steps; its report keeps random, exhaustive, and
formal evidence types distinct. Physical qualification requires a complete
physical run with all routed source-ready/capture segment evidence plus exact
deadlines and whole-design global target/virtual-runtime WNS/TNS. The real DLA
acceptance now satisfies that evidence contract: all 157,811 segments are
endpoint-exact with no missing or failed deadline, and all 195,532 original
paths are included once. This is open academic software-flow qualification;
it does not claim 10 ns target-clock closure or hardware bring-up.

Phase 5 certificate construction and Phase 6 macro-cycle replay index the
`capture_requirement` relation once. They do not scan every logic segment for
every terminal capture; a 100,000-capture regression enforces one segment-set
scan, while the independent Phase 5 validator builds and checks its own index.
The Phase 6 producer performs one full-design reference evaluation per checked
macro-cycle and settles each partition from that reference snapshot with an
event-driven local cone update; initialization, source sampling, and commit do
not replay the full design. The managed checkpoint is then replayed once by the
independent Experiment v2 validator before publication. Lookahead and Phase 7
consumers may reuse that result only when the Phase 6 directory is an immutable,
byte-sealed managed checkpoint with an independent validation certificate.
They recheck the checkpoint and split-artifact seals but do not rerun the same
functional simulation for every downstream seed. Standalone or unsealed Phase
6 directories continue to require the complete replay.
