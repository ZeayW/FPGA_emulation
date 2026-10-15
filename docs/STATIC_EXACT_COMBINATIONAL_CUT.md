# Static Exact combinational cuts

Static Exact is an explicit boundary-semantics mode for supported synchronous
circuits. It allows structurally supported combinational signals to cross FPGA
boundaries while retaining the same virtual-cycle behavior.

Select it with `--cut-mode static-exact-combinational`. The ordinary full-flow
CLI defaults to `sequential-only`. Optimizer choice and cut semantics are
separate options.

## Supported circuit model

The implementation requires one virtual DUT clock and the supported
synchronous reset/state model. It does not admit arbitrary generated clocks,
general CDC, opaque IP, or combinational feedback as transparent cuts.

Stateful logic, hard-macro/cascade relationships, clock/reset networks,
unsupported primitive cones, multiply-driven nets, and combinational SCCs
retain their structural atomicity. Generalized candidates are selected from
supported mapped soft logic; eligibility is not a physical timing guarantee.

Functional dependency and capture coverage comes from EmuIR connectivity.
TimingPathDB supplies optimization and timing identities, but a truncated
timing sample cannot define functional coverage.

## Phase ownership

- Phase 3 selects an assignment under provider-neutral structural constraints.
  Dependency relationships are reconstructed from the selected assignment;
  they are not a guessed physical/schedule risk certificate.
- Phase 4 binds selected communication to actual system routes.
- Phase 5 assigns concrete TDM resources/events and checks dependency readiness.
- Phase 6 materializes preserved TX/RX boundaries and shadow consumer nets,
  then checks macro-cycle behavior.
- Phase 7 binds routed logic/interface delays and validates event deadlines and
  original-path timing.

A cut is valid only under the resulting complete contract. A legal partition
alone does not prove that its implementation meets a target clock.

## Slot-edge convention

The transport uses `tx-sample-before-rx-shadow-update-v1`:

1. A TX at slot `S` samples the source using the controller's pre-edge value.
2. An RX at arrival slot `A` updates its shadow at that edge. A simultaneous
   TX sees the pre-update shadow.
3. A value launched/captured at edge `E` with a budget of `B` slots becomes
   eligible for downstream sampling at `E+B`.
4. A one-slot relay therefore requires `next_tx_slot >= arrival_slot + 1`.
5. Architectural register output timing includes routed clock-to-Q and local
   launch-to-TX delay; it is not automatically eligible at slot zero.
6. A transport arrival precedes the commit slot. Capture timing at commit must
   include setup and uncertainty rather than inventing an extra timing window.

Scheduling, RTL generation, equivalence checking, and physical deadline
binding must use the same convention.

## Validation boundary

The structural contract identifies cut nodes, dependency edges, logic segments,
and capture requirements. Route/schedule/physical artifacts reference its
identity rather than copying another complete graph.

Qualification requires actual algorithm-selected combinational cuts and complete
functional coverage. Small models can use exhaustive one-step state/input
enumeration; larger trace checks must be labelled as trace validation rather
than general proof.

Global timing additionally needs routed `launch_to_tx`, `rx_to_tx`, and
`rx_to_capture` segments where applicable. Missing or ambiguous physical
evidence remains incomplete or explicitly bounded. A zero-cut run, proxy
objective, or successful intermediate stage is not a substitute for this gate.

See [validation requirements](VALIDATION.md) and
[global timing scope](global-opensta-qualification.md).
