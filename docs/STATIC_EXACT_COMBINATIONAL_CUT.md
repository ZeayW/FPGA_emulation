# Static exact combinational-cut mode

## Status and claim boundary

The production flow defaults to generalized Static Exact partitioning through
the sole production candidate policy, `assignment-derived-acyclic-v2`. It
passes Phase 3 structural partition legality, Phase 4 native-route contract
binding, and unified Phase 5 timing-aware TDM assignment. Sequential-only
Phase 3 transports
register outputs, transport-safe register inputs, and replicated primary
inputs; other combinational connectivity remains atomic. The checked-in
`combinational-cut characterize` command is read-only. It identifies a
conservative LUT-only eligibility upper bound, combinational SCCs, potential
cut dependencies, and atomic-component reductions. It does **not** change a
partition, create a transport schedule, establish macro-cycle equivalence, or
claim physical timing closure.

The Phase 3 mode `static-exact-combinational` releases every structurally legal
candidate, reconstructs dependency depth from the selected assignment, accepts
any positive safety cap, and emits the provider-neutral v3 structural timing
contract. Phase 4 binds that
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

All sampled-virtual-wire artifacts use
`tx-sample-before-rx-shadow-update-v1`:

- A TX assigned slot `S` samples its source at the fabric rising edge for
  which the controller's pre-edge value is `S`.
- An RX assigned arrival slot `A` updates its shadow register at the fabric
  rising edge for which the pre-edge value is `A`. A TX sampling on the same
  edge observes the pre-update shadow value.
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

Phase 3 emits one versioned structural sub-contract. Phase 5 owns the separate
slot/readiness policy, and downstream artifacts bind the structural contract by
schema and digest instead of copying the full JSON:

```json
{
  "schema": "emuflow.static-exact-combinational-cut/v3",
  "mode": "static-exact-combinational",
  "candidate_selection_policy": "assignment-derived-acyclic-v2",
  "max_cross_fpga_dependency_depth": 8,
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
- Phase 3 does not compute or enforce frame/slot readiness. Concrete routing,
  contention, ratios, lane capacity, settle time, and commit feasibility are
  reconstructed and enforced by unified Phase 4/5.

The characterization report is an upper bound because it ignores capacity,
user group/fixed constraints, BoardDB hop limits, link capacity, schedule
feasibility, and physical segment deadlines.

## Delivery sequence

1. **Characterization (implemented, no behavior change).** Read-only SCC,
   eligibility, dependency-depth, and theoretical atomic-component report;
   independent exact replay; tamper tests.
2. **Phase 3 generalized structural policy (implemented, production).** The
   sole production policy releases every structurally eligible candidate,
   reconstructs the selected dependency DAG after assignment, and emits the
   provider-neutral v3 contract. Its strongest Phase 3 qualification is
   structural partition legality, never schedule feasibility.
3. **Unified Phase 4/5 arbitrary budgeted DAG depth (implemented).** Exact
   branch-level route binding, canonical contract digest binding, normal
   timing-DAG ratio/lane/slot optimization with dependency readiness constraints,
   source-ready/capture certificates, fixed-frame diagnostics, and tamper tests.
4. **Phase 6 (implemented).** Contract-bound sampled-virtual-wire boundaries,
   hidden-bypass rejection, deterministic shadow startup, event-driven
   macro-cycle simulation, complete small-model one-step enumeration, and
   representative Yosys formal one-macro-step miters for dependency depths
   one, two, and three. General-design formal closure is not claimed.
5. **Phase 7C (implemented and independently accepted on real routed DLA).**
   Contract-bound routed `launch_to_tx`, `rx_to_tx`, and `rx_to_capture`
   evidence, independent causal deadline reconstruction, explicit missing-
   evidence incompleteness, and global target-clock/virtual-runtime WNS/TNS.
   The public `schemas/static-exact-segment-deadlines-v2.schema.json` contract
   fixes the complete routed-deadline report surface. The large acceptance
   selects five natural combinational cuts without fixed partition placement,
   covers all 157,811 contract segments with endpoint-exact routed evidence,
   and replays all 195,532 original timing paths. Its negative 10 ns
   target-clock WNS/TNS is reported honestly; the gate proves complete timing
   evidence and positive causal segment deadlines, not target-clock closure.
6. **Optimizer integration.** Partition providers screen candidate assignments
   only against the reconstructed structural contract and BoardDB reachability.
   The ordinary Phase 5 timing-DAG/ratio/lane/slot providers consume sampled-wire
   readiness and capture constraints directly; there is no dedicated Static
   Exact scheduler or Phase 3 scheduler-feasibility gate.

## Canonical search-space audit

The generalized policy was audited on one immutable canonical DLA + EDA 2023
case6 frontend. Sequential-only, legacy v1, and guarded generalized v2 used the
same EmuIR bytes and the same physical seed; no arm was reconstructed from a
different synthesis result. The original design has 379,357 instances and
73,767 combinational nets. The resulting Phase 3 search spaces are:

| policy | clusters | released combinational candidates | largest cluster | clusters above 100 instances | selected real combinational cuts |
|---|---:|---:|---:|---:|---:|
| sequential-only | 246,387 | 0 | 724 | 197 | 0 |
| legacy potential-frontier v1 | 304,300 | 16,251 | 154 | 84 | 0 |
| assignment-derived v2 with guarded refinement | 367,129 | 49,695 | 52 | 0 | 2 |

V2 therefore adds 33,444 structurally legal candidate boundaries over v1 and
removes the large atomic-cluster tail: sequential-only has 72 clusters above
500 instances, whereas v2 has none above 100. The current canonical result is
not candidate-starved. Its two selected cuts are the optimizer's cost-aware
choice from the larger legal space, and both survive the unified TDM schedule,
shadow-transport, equivalence, and routed segment-deadline gates.
Forcing a larger cut count would optimize an activity metric rather than final
timing QoR and is not part of the default policy.

The accepted v2 assignment reaches actual dependency depth two, so the
configured depth-eight safety cap is not active on this case. Likewise, the
unified Phase 5 scheduler accepts arbitrary acyclic depth subject to
the fixed frame, routed latency, lane capacity, relay readiness, and capture
deadline. These measurements do not justify weakening either fail-closed gate
or replacing the fixed-frame feasibility proof with an assumed benefit.

### Cross-policy Phase 7 promotion certificate

Sequential-only and generalized Static Exact v2 do not share Phase 3--5
artifacts. Run two independent one-shot `multi-fpga compile --physical`
commands from the same immutable RTL, BoardDB, architecture, constraints, tool
installation, partition seed, and physical seed. Select `sequential-only` for
the control and `static-exact-combinational` for generalized v2. Do not publish
or retain their Phase 1--6 products as checkpoints. The terminal comparator
reads only the two compact final reports and compares whole-original-design
target-clock and virtual-runtime WNS/TNS. It also records exact-cut
count/depth, virtual frequency, transport and total physical cells, cut nets,
scheduled bit-hops, frame size, completion slot, DRC/unrouted counts, and total
wall time. Delete both complete work directories after the terminal summary is
written and checked.
The generated promotion gate is false unless v2 exercises at least one real
combinational cut and improves the paired target-clock result over
sequential-only.  A single physical seed is the routine gate; more seeds are
an explicit robustness study.

The current canonical DLA + EDA 2023 case6 run exercised 102 generalized cuts
at maximum dependency depth three. It completed Phase 7/7C with zero DRC
violations, zero unrouted nets, and zero per-FPGA physical TNS, but its global
target-clock WNS/TNS were -187.85581036 ns / -548934.0510065886 ns versus
-84.5812926868 ns / -277276.1497366623 ns for sequential-only. Completion
moved from slot 8 to slot 15. This is a valid negative algorithm result, not a
validation failure: generalized semantics and physical deadlines were
exercised, while final system-level QoR regressed. Consequently that unguarded
configuration was rejected. The later guarded generalized policy improved the
paired register-only control and is now the default together with PATRON;
sequential-only remains an explicit comparison arm. This historical negative
result is not evidence for or against the newer combined default.

The automatic Phase 3 follow-up uses directional MFSPart FM after the sealed
TritonPart assignment. Its Static Exact objective treats existing
non-combinational transport and newly available combinational boundaries as
different classes: an immutable per-net guard forbids increasing the initial
worst-sink board distance of the former, while the latter pay the ordinary
timing-weighted cut/connectivity/hop objective without inheriting an
impossible zero-distance guard. Both the optimizer and the independent native
checker reconstruct and enforce this rule. Legacy V1/V2 native refiner inputs
remain readable with their original unguarded bottleneck semantics. A
diagnostic 62-move prefix completed full Phase 7/7C at -84.913220755 ns WNS
and -159994.95141046078 ns TNS, preserving most of the TNS benefit while
cutting the earlier WNS regression to 0.332 ns. Because that prefix was chosen
diagnostically, it calibrates the automatic objective but does not itself pass
the promotion gate.

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
  --provider patron \
  --cut-mode static-exact-combinational \
  --static-exact-candidate-policy assignment-derived-acyclic-v2 \
  --max-cross-fpga-dependency-depth 8 \
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
  "minimum_combinational_cut_nets": 1
}
```

Those values are part of the Phase 3 producer and validator identities. The
compiler uses the same timing-aware Phase 4 and timing-DAG ratio/lane/slot
providers as the register-only flow. Sampled-wire readiness is part of the
Phase 5 model, so ratio and slot refinement remain enabled and independently
checked. The compiler produces one independently sealed physical Phase 7
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

The canonical paired QoR experiment makes this distinction explicit. Its
sequential arm requires zero combinational cuts, while its generalized v2 arm
inherits the requested positive exercise threshold. Both arms complete Phase
1--7 so runtime, resources, and final whole-design WNS/TNS remain comparable.
Only an exercised generalized-v2 arm can satisfy the default-promotion gate.

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
not replay the full design. The one-shot flow performs the independent replay
once before Phase 7, then discards the intermediate Phase 6 work tree after the
terminal Phase 7/7C evidence has been extracted.
