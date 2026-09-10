# Credible reference hardware platform

## Active target: fully open ECP5 reference platform

The real-RTL qualification path now has an automatically generated macrocycle
testbench (`snapshot_equivalence.py`), using the unsplit mapped-logic evaluator
as reference and actual generated dual-board transport RTL as implementation.
Every mapped FF is observed after commit in addition to pre-edge host outputs.
Run it with the exact declared initial-state contract and explicit workload
vectors; passing a finite trace is not a proof over all inputs or initial states.
Keep this expensive replay in qualification, not the production hot path.

The active target supersedes the vendor-dependent plan below. Neither Vivado,
Diamond, nor proprietary communication IP is an allowed dependency of this
deliverable. The earlier MPS4/VCU118 work remains optional historical research,
not an acceptance prerequisite for the open platform.

The first fixed assembly is two ULX3S PCB 3.0.x boards populated with
LFE5U-85F-6BG381C, connected through two crossed 3.3 V GPIO signals and common
ground. This is an explicitly specified assembly of real open-hardware boards,
not a manufacturer-qualified multi-FPGA system. Do not connect the power rails
or attach 5 V signals. Cable construction, power sequencing/back-power risk and
electrical delay still require qualification before recommending physical use.

`board_ulx3s.py` owns the pinned source facts, fixed wiring profile and endpoint
LPF generation. GP0 (B11) transmits to the other board's GN0 (C11); these are
single-ended pins, not an LVDS pair. Both boards retain their independent
25 MHz G2 oscillators. D6 is active-low reset. No phase-lock or fixed wire
latency is assumed. These GPIOs avoid the documented ESP32/ADC shared pins.
The generated LPF deliberately omits the upstream blanket asynchronous/reset
timing exceptions. A pin file is not a complete timing constraint contract.

Implementation sequence and completion gates:

1. **Wiring profile (implemented, unit-tested).** Fixed two-board configuration,
   pinned upstream manual/LPF, unique pins, TX-to-RX wiring, 75% resource policy,
   no fabricated board latency. This is not yet a schedulable BoardDB: its
   asynchronous transport cannot honestly be described by the existing fixed
   link latency field.
2. **Open physical endpoint (partial: routed bitstreams pass).** Yosys `synth_ecp5`, nextpnr-ecp5
   `--85k --package CABGA381 --speed 6`, and Project Trellis `ecppack` must
   produce a routed design and bitstream without commercial tools. Check the
   package pin database and all clock/I/O constraints, not only process exit.
3. **Transport (implemented; qualification incomplete).** Portable framed
   GPIO RTL now includes synchronized receive, CRC/sequence checks,
   backpressure, multi-round exchange and logical commit. Independent-clock
   simulations and real routed implementations pass. Recovery remains
   coordinated reset/new-session restart, not transparent retry or rollback.
   Physical reset recovery/removal, metastability and external electrical
   assumptions are not established by those tests. Variable transport latency
   is not hidden behind an arbitrary fixed cycle count.
4. **EmuFlow integration (partial).** The generalized TritonPart LUT4/FF
   frontend, source-bound dual-board splitter, physical ECP5 backend and
   combined DUT/transport resource checks exist. Real SERV has passed
   four declared macrocycles, dual-board placement/routing and bitstreams.
   Source boundary-influence correspondence and native OpenSTA maximum-delay
   queries cover both local and cross-board boundary segments, including
   extracted synchronous controls. The `emuflow ulx3s-qualify` one-command
   qualification now also covers all 102,211 SERV structural source paths:
   101,170 numerically timed members and 1,041 explicitly classified retention
   or Boolean-independent members. Native OpenSTA observed-event analysis has
   no TX/commit readiness violations, runtime WNS +291,598.204058 ns and TNS 0;
   at the original 40 ns target, WNS is -2,716,800.197959 ns. UART is therefore
   a slow functional emulation transport, not target-frequency execution.
   These are finite-trace, ideal-skew routed-cone bounds, not measured link or
   universal timing guarantees. Reset/CDC and integrated acceptance remain
   incomplete. Never reinterpret
   VTR or AMD resource counts as ECP5 counts; handshake progress and local
   Fmax are not whole-design WNS/TNS.
5. **Acceptance (pending).** Real RTL through Phase 1--7, one physical seed,
   complete original-path timing coverage, macro-cycle equivalence, checked
   placement/routing and bitstream production. Keep only compact terminal
   evidence. Offline success does not establish measured signal integrity,
   BER, cable delay or reliable power-on behavior.

Source revision: `emard/ulx3s` commit
`6a92cec6b177191c5b0f80e260013a1f8ec147dd`; manual section “Connectors” and
`doc/constraints/ulx3s_v20.lpf` (declared compatible with v3.0.x).
The wiring profile explicitly rejects claims of full-flow qualification today.
Wiring alone is not run evidence. The unused always-raise qualification stub
has been replaced by `snapshot_acceptance.validate_snapshot_offline_result`,
which checks the completed run's compact gates and returns a finite offline
claim with explicit assumptions, not universal or hardware qualification.

### Integrated physical-host evidence and remaining boundary

The dedicated `emuflow ulx3s-qualify` command has completed a naturally connected
SERV run through serial host records, the generated physical reset/host wrappers,
dual-board transport, physical implementation and native global analysis.
This is stronger than the earlier internal-request-interface simulation:
all 1,336 FF states and 168 output bits matched for four declared macrocycles.
The single-seed run completed in 1,844.03 seconds; all 102,211 structural paths
were accounted for, with 101,170 numerical paths and explicit nonnumerical
retention/Boolean-independence classes. Runtime WNS was +291,558.215395 ns,
TNS zero; original-40-ns-target WNS was -18,274,584.785104 ns and path-summed
TNS -1,043,072,220,003.8145 ns. Serial host waiting belongs to this timeline.

Do not promote this finite digital execution to measured or worst-case hardware
closure. The test has ideal digital interboard wires, fixed declared clock
offsets, no jitter/metastability model, seeded initial state and only the declared
input trace. Physical data timing uses exported SDF bounds and ideal clock skew;
raw asynchronous reset recovery/removal and external electrical behavior remain
unqualified. The integrated acceptance claim must retain those boundaries, not
turn existing `False` qualification fields into unconditional pass flags.
The retained physical-host SERV result passed the scoped offline acceptance
checker. No physical stages, waveform replay or numerical timing were rerun
to check that terminal contract.

### Open physical bring-up evidence

`scripts/qualify_ulx3s_endpoint.py` runs real tools and produces an explicitly
endpoint-only report. With OSS CAD Suite 2026-09-10 (Yosys 0.69+10,
nextpnr 0.11.1-25-ge47c2589), the checked-in GPIO physical smoke fixture passed
synthesis, placement/routing and bitstream generation on the exact 85k/CABGA381
speed-6 target, seed 1. It used 18 TRELLIS_COMB, 13 TRELLIS_FF and four I/Os;
the routed local clock check passed its 25 MHz constraint. This is not evidence
for external I/O timing, a functioning communication protocol or global timing.
In particular the reported local Fmax is not whole-design WNS/TNS.

The smoke fixture is an explicitly synthetic tool test, not a benchmark entry.
Run the script with its RTL, `--top ulx3s_gpio_physical_smoke`, `--tools` pointing
to the OSS suite's `bin`, and a fresh `--out` directory. It sets HOME, TMPDIR and
XDG directories within that output, avoiding tool-wrapper writes to a default
home. Its `physical_outputs_generated` status deliberately does not say that
the hardware platform or external clock-domain crossings are qualified.

The initial byte PHY is `rtl/transport/emuflow_gpio_uart.sv`: portable 8N1,
two-stage receive synchronization, ready/valid buffering, explicit overrun and
framing-error pulses, and break recovery. It never overwrites an unread byte
without reporting an error. The upper framed transport must invalidate a frame
on either error; this byte module alone does not supply remote backpressure,
CRC/retry, bounded latency, or virtual-cycle commit. The independent-clock
testbench exercises all 256 byte values, unread-data preservation, overflow
and break recovery. Physical CDC constraints/MTBF and complete transport
qualification remain pending.

`emuflow_gpio_record.sv` adds the initial fixed 64-bit record envelope: magic,
version, 16-bit sequence, payload and CRC-16/CCITT-FALSE. It buffers receive
records, holds transmit bytes under backpressure, and latches faults on CRC,
sequence, receive overflow or PHY errors. It is deliberately fail-stop rather
than silently dropping or replaying DUT data. Both peers must be reset into a
new coordinated session; independent reset recovery and distributed commit
are not implemented yet. The record test uses independently calculated CRC
vectors; integration with the UART and physical implementation are subsequent
gates. No record-layer success qualifies virtual-cycle semantics by itself.

The composed `emuflow_gpio_endpoint` has now completed 16 checked-record
roundtrips in each of two independent-clock simulations (receive half-period
19.8 ns and 20.2 ns against 20 ns, plus phase offset). Its echo qualification
top, including UART and CRC/sequence/buffering logic, also completed real ECP5
synthesis, placement/routing and bitstream production with the above open
toolchain. It used 758 TRELLIS_COMB, 344 TRELLIS_FF and four I/Os. The local
25 MHz constraint passed. This is not a measured link, CDC MTBF proof, global
WNS/TNS result, or a full Phase 1--7 DUT run. The echo top is a tool fixture,
not a substitute for the required naturally connected workload.

The runner additionally checks nextpnr resource accounting against the 85k
device inventory and applies the 75% ceiling to LUT4/FF/DP16KD/MULT18X18D
resources, including communication overhead. All reported local clocks must
satisfy their constraints (at least the endpoint's 25 MHz clock). Missing,
non-finite or malformed clock/resource data fails qualification. This is a
single-clock endpoint gate, not a general multi-clock timing analyzer; device
I/O availability is not interpreted as the board's free connector budget.

### Logical snapshot exchange (experimental)

`emuflow_gpio_exchange` operates on checked ordered records. Both fixed roles
first exchange an identical externally supplied session identifier and word
count. The leader requests a transaction; both peers retain their local
snapshots, exchange indexed 32-bit words, then perform COMMIT/ACK. The follower
commits first; the leader commits only after ACK. Neither may begin another
transaction before the leader has completed that handshake. These are logical
macrocycle barriers, not simultaneous oscillator edges. A DUT consumer must
hold its exported snapshot through each transaction and use the commit pulse
as a clock enable. The current multi-round extension is described below;
complete physical settling and general split-netlist integration remain pending.

Unexpected records, changed session IDs, transport errors or in-flight timeout
invalidate the session. No auto-retry or independent-reset recovery is claimed;
restart requires coordinated reset and a new externally selected session ID.
Epoch wrap is rejected. Timeout is a fail-stop policy, not a wire-delay bound.
A lost ACK can leave one peer committed: the run is invalid, and this protocol
does not claim fault-tolerant atomic rollback or physical hardware reliability.
The composed UART/record/exchange simulation passes eight two-word transactions
at both tested clock offsets and rejects a disconnected transaction by timeout
without advancing either consumer. Additional injected protocol-error and
controller physical fixtures have now passed as well. The injected tests cover
wrong session, same role, width mismatch, peer silence, unexpected restart,
changed session, word order, epoch mismatch and an ACK coincident with PHY fault.

Both fixed-role physical tops include UART, CRC records, exchange controller
and a small stateful consumer (a synthetic qualification fixture, not a DUT
benchmark). With the same open toolchain and physical seed 1:

| Role | TRELLIS_COMB | TRELLIS_FF | I/O | Local constraint | Synthesis / P&R / pack |
|---|---:|---:|---:|---|---|
| Leader | 1,254 | 605 | 4 | 25 MHz passed | 7.05 / 12.77 / 2.06 s |
| Follower | 1,278 | 605 | 4 | 25 MHz passed | 7.66 / 14.81 / 1.89 s |

Device inventory and 75% resource checks pass for both. P&R and packing logs
have no warnings/errors; synthesis includes Yosys/ABC informational warnings
about `translate_off`, boxed carry handling and combinational subnetworks.
These results qualify the scoped physical fixture, not external CDC/MTBF,
whole-design timing, complete EmuFlow or measured hardware operation.

The tool runner now lives in `emuflow.ecp5_backend.run_ulx3s_physical` for
subsequent DUT integration. The endpoint CLI is a thin adapter with an explicit
`--board` identity. It validates inputs/tools before creating scratch, refuses
to overwrite an output directory, stops on tool failure, rejects missing
outputs and retains only resource/clock metrics rather than copying full
nextpnr path diagnostics. Input RTL and generated constraints are identified
once in the terminal summary. Fixed-slot Phase 7 does not silently select this
runner: asynchronous DUT/transport and global timing binding remain required.
After extraction, the follower fixture was rerun with the real open tools
through this API and `--board board1`. All three stages and scoped checks
passed; nextpnr reported the same 84.338363647 MHz local Fmax against 25 MHz.
The terminal report has five RTL input identities and no detailed path copy.

### DUT binding interface

`build_ulx3s_snapshot_top` generates the four-pin board top around a partition
with ports `clk`, `reset`, `step`, `exported_values`, `imported_values`. It gates
`step` with commit and both protocol/PHY fault signals. Unequal directional
widths share an explicitly selected word envelope with zero padding; invalid
widths, peer IDs and recursive module bindings are rejected. Both peers must
use the same envelope and session identifier. All state must already have been
lowered to step-enable semantics, and exported values must satisfy the snapshot
contract. This interface does not translate a fixed-slot transport netlist.

The initial binder still requires automatic EmuIR lowering and combinational
evaluation-round support before claiming general DUT acceptance. The generated
interface test compares unequal-width stateful consumers against a synchronous
reference; it is a synthetic semantic test, not the required real-RTL benchmark.
Actual Icarus RTL validation now passes 16 synchronous-reference macrocycles,
with independent 20/20.2 ns half-period clocks and unequal 33/17-bit directional
payloads. Both generated tops are exercised, including padding and clock-enable
updates. This does not establish automatic arbitrary-RTL lowering, combinational
evaluation rounds, physical timing or full-flow acceptance.

`build_snapshot_boundary_plan` is the connectivity bridge from a validated
EmuIR and full instance assignment to directional envelope bit order. It uses
actual net drivers/sinks, includes combinational cuts, deduplicates fanout,
and requires explicit ownership for external data ports. Clock/reset wiring
remains a separate board-service binding. No assignment is rejected or ranked
using predicted scheduling feasibility; the currently implemented envelope
size is checked after partitioning. This plan still needs the automatic
partition RTL lowering and evaluation/timing binding consumers.

### Automatic mapped partition lowering (initial subset)

`emit_snapshot_partition` consumes actual EmuIR connectivity and assignment,
emitting local LUT functions, positive-edge generic FF/FDRE/FDSE behavior,
snapshot export/import indices and explicit host I/O vectors. The source is
not mutated. Coordinated reset uses an explicit binary initial-state contract;
missing state is not implicitly zero. Unknown constants, missing or multiply
bound primitive pins, inverted FF controls, unsupported stateful primitives,
generated/multiple clocks and unbound DUT resets fail explicitly.

This is the initial lowerer, not yet full-design support. Host data inputs must
be connected by the host adapter, and combinational snapshot values require
the evaluation/settling contract before committing the original DUT state.
The lowerer does not remove combinational cuts or constrain partitioning to
make the transport test easier. Actual Icarus simulation of two automatically
emitted partitions now agrees with an independent synchronous reference for
32 macrocycles, including a LUT-driven combinational cut and pauses between
state updates. That test verifies netlist lowering only: its explicit snapshot
transfer model is not physical link or whole-flow timing evidence.

### Multi-round combinational evaluation (RTL and scoped physical gates passed)

The exchange controller now performs `ROUNDS` acknowledged snapshot exchanges
per DUT macrocycle. Intermediate exchanges update only remote shadows; DUT
state commits on the final exchange. Re-evaluated local combinational outputs
are sampled for the next round. Both peers explicitly exchange their round
count after the session/role/width handshake and reject disagreement. An epoch
identifies an exchange, not a DUT cycle; epoch exhaustion invalidates the run
instead of wrapping. The new protocol requires matching implementations on
both peers and is not silently compatible with the earlier single-handshake
fixture.

This is a post-partition transport evaluation mechanism, not a Phase 3 guard
or partitioning objective. The caller must derive adequate rounds from the
actual combinational dependencies and validate local settling against physical
timing. Neither a configured round count nor a successful handshake proves
whole-design timing. Actual Icarus Verilog 13.0 tests pass the three-crossing
inverter chain across independent clocks. The same circuit with only one round
fails specifically on stale data, rather than merely timing out. Protocol
round-count mismatch is rejected, and existing CRC/error/clock-offset regressions
pass with the revised handshake.

An additional composition test automatically lowers the EmuIR LUT/FF partitions,
derives three rounds, generates both board tops and connects their actual UART
signals. Both original registers agree with their synchronous reference for 16
macrocycles. The closed fixture has no host data ports; only an unused padding
port is tied to zero. This does not qualify arbitrary external host I/O, a real
workload, physical timing, or complete Phase 1–7. The test command is
`PYTHONPATH=src python3 -m unittest discover -s tests -p test_ulx3s_rtl.py -v`;
both Icarus executables must be installed (`IVERILOG`/`VVP` override PATH).
An explicit missing-tool skip is not a passing RTL gate. All six test methods
(11 simulations including the required negative case) passed at that gate. The physical
bitstreams recorded above predate this protocol revision. A new physical gate
explicitly selects three rounds on both roles using the same open toolchain,
fixed device and seed 1. Actual synthesis, nextpnr P&R and Trellis packing pass:

| Role | TRELLIS_COMB | TRELLIS_FF | I/O | Local constraint | Synthesis / P&R / pack |
|---|---:|---:|---:|---|---|
| Leader | 1,384 | 623 | 4 | 25 MHz passed | 9.29 / 15.71 / 2.23 s |
| Follower | 1,437 | 623 | 4 | 25 MHz passed | 8.51 / 15.62 / 1.86 s |

Both pass the resource-inventory and 75% gates. Only compact terminal summaries
are retained; the completed physical scratch was removed. This scoped physical
result does not establish external settling, intended-clock coverage, global
timing, real-workload integration or measured board operation.

### Host transaction composition (logical interface qualified)

`emit_snapshot_pair` composes the lowerer, post-partition rounds and actual
UART/exchange RTL. All host data ports must be explicitly owned by board0.
Its request/response interface is synchronous to that board's clock; it is
not an unsynchronized external pin interface. An accepted request latches
inputs, waits a clock edge before snapshot launch, then runs the derived
exchanges. Original DUT state changes only on commit. The result samples
outputs immediately before that original active edge, not from an arbitrary
later physical instant. The result stays valid and unchanged until consumed;
no next request is accepted while a transaction or unconsumed result exists.
Fault suppresses both interfaces and requires coordinated session restart.

The composition RTL test drives new live input values during an in-flight
request, pauses response consumption, and checks 16 macrocycles against an
independent synchronous XOR-state reference. This brings the automated RTL
suite to seven test methods / twelve simulations. Missing host ownership and
invalid session/module contracts fail explicitly. The interface vectors retain
their original port/bit mapping; no actual DUT input is silently tied off.
Additional primitive support, full Phase 1–7 and global timing remain
outstanding. A logical adapter alone is not a usable board-level host connection.

### Onboard USB-serial host binding

The pinned upstream LPF's USBSERIAL section binds `ftdi_txd` to M1 (FPGA input)
and `ftdi_rxd` to L4 (FPGA output), with LVCMOS33 and pull-ups. The pinned manual
identifies US1's onboard FT231X and its factory configuration. These are not
the raw USB US2 pins: the existing USB bridge converts host USB to UART, so no
FPGA USB core or proprietary IP is needed. Existing board EEPROM configuration
is assumed; EmuFlow must not silently reprogram it or run a JTAG programmer
concurrently with host UART use.

`emit_snapshot_pair` now includes generated physical wrappers in each board's
canonical RTL string, referenced by `physical_top`. Board0 adds `host_rx`/`host_tx`;
board1 retains the four-pin interface. `run_ulx3s_physical(host_uart=True)` and
`qualify_ulx3s_endpoint.py --host-uart` bind those extra pins on board0 only.
The port is 115200 baud, 8N1, no hardware or software flow control. At nominal
25 MHz, divider 217 yields 115207.37 baud (about +0.0064%). This is a divider
calculation, not a measured USB/oscillator rate or transport-latency guarantee.

`emuflow_snapshot_host` consumes CRC/sequence-checked 64-bit records. Each row
below gives the payload fields in most-significant-first order:

| Direction / operation | Payload fields |
|---|---|
| Host HELLO | `60`, version `01`, zero 16 bits, session ID 32 bits |
| Device CONFIG | `61`, version `01`, zero 16 bits, input bits 16, output bits 16 |
| Host INPUT (each ascending word) | `70`, index 8, epoch 16, data 32 |
| Host EXECUTE | `71`, zero 8, epoch 16, zero 32 |
| Device OUTPUT (each ascending word) | `72`, index 8, epoch 16, data 32 |
| Device DONE | `73`, zero 8, epoch 16, zero 32 |

Input/output vectors are limited to 256 words, little-word-first within the
packed vector; each record itself uses the existing big-endian byte envelope.
Unused high input bits must be zero. Width-one padding remains for a vector
with no actual host bits; the interface's original port/bit map distinguishes
padding from DUT data. Every request must supply all input words and EXECUTE.
The host waits for all output words and matching DONE before the next request.
Epoch starts at zero and exhaustion fails rather than wrapping. Missing/partial
input never executes. Malformed session/index/epoch/padding or link/core faults
stop the adapter; no automatic retry or rollback is promised. Software must
use a bounded receive timeout and invalidate the run after a timeout.

Actual RTL tests pass command backpressure/error cases and eight original-state
macrocycles over the complete serial-host/generated-pair chain, with independent
clocks. The RTL suite now has nine methods / fourteen simulations. The actual
generated physical wrappers have now separately passed Yosys ECP5 synthesis,
nextpnr place/route and Trellis packing using the same fixed target and seed 1:

| Board | TRELLIS_COMB | TRELLIS_FF | I/O | Local constraint | Synthesis / P&R / pack |
|---|---:|---:|---:|---|---|
| board0 (host and peer UARTs) | 1,889 | 830 | 6 | 25 MHz passed | 11.19 / 21.40 / 2.15 s |
| board1 (DUT and peer UART) | 1,141 | 465 | 4 | 25 MHz passed | 9.47 / 11.23 / 1.94 s |

The exact-device inventory and 75% checks pass. This includes the generated
mapped XOR-state fixture and host adapter, not just a disconnected protocol
module. It is still a synthetic correctness fixture, not the required natural
RTL workload. Only compact terminal reports were retained after both foreground
tool chains completed; physical scratch was removed. Measured FTDI/electrical
operation, full Phase 1–7 and whole-design timing remain pending.

### Host software

`scripts/ulx3s_host.py` uses `SnapshotHostClient` and the dependency-free POSIX
`SnapshotSerialPort`. The latter opens only the explicitly selected TTY at
115200 8N1, raw mode, without hardware/software flow control. It does not run
a programmer, change FTDI EEPROM, automatically toggle reset, or discard bytes
to guess a new packet boundary. Opening a serial device still has normal
OS/driver control-line behavior; physical power/reset behavior is unmeasured.

After loading matching generated board bitstreams and coordinating their reset,
use the exact session and packed vector widths from that generated design. For
the documented one-bit semantic fixture, an example command is:

```sh
python3 scripts/ulx3s_host.py --device /dev/ttyUSB0 --session 0x789 \
  --input-bits 1 --output-bits 1 --timeout 30 1 0 1
```

The device path is an example, never autodetected or opened by tests. It emits
one compact JSON result per completed macrocycle. Outputs are valid only after
all indexed words and the matching DONE arrive. Returned values use the
generated interface's original port/bit order and pre-active-edge semantics.
The example requires the matching fixture bitstream; it is not a real workload
benchmark command or a way to control arbitrary existing firmware.

The client checks the known record CRC vectors, header/version, sequence,
configuration widths, output word/epoch, zero padding and final completion.
Partial writes and reads are handled under a single deadline for each connect
or step. Timeout/protocol/I/O errors latch software failure; reconnect and
further steps on that client are forbidden. A fresh session requires coordinated
hardware restart, not replay of a possibly already committed input. The client
is synchronous and not thread-safe.

Seven software tests pass, including fault/no-retry cases, shrinking total
deadline, exact multiword commands, golden CRC and binary I/O through a real
OS pseudo-terminal. They do not prove FTDI hardware operation or end-to-end
software-to-hardware execution; that remains an explicitly unmeasured gate.

### Real-RTL frontend binding (in progress)

The generic Yosys API can explicitly map `lut_size=4` for ECP5, instead of
counting generic LUT6 cells as individual LUT4 resources. The existing LUT6
default for other consumers is unchanged. Final native ECP5 resource checks
still include communication and physical remapping overhead.

`emit_snapshot_pair(..., reset_data_ports=[...])` explicitly binds named
synchronous DUT reset inputs as sampled host data. Its binder follows the
combinational fanout to state endpoints, allowing supported FF data and
synchronous controls, but rejecting clock pins, asynchronous resets and unknown
stateful semantics. Original polarity and logic remain unchanged. A separate
view changes only accepted root nets' classification; the original EmuIR is
not mutated. Board reset, coordinated session reset and declared initial state
are not replaced by this operation. Missing/unbound reset handling still fails.
This is an integration capability with unit tests, not yet real-RTL full-flow
qualification or permission to convert arbitrary asynchronous resets.

Physical qualification now has an integrated snapshot interface in
`run_ulx3s_physical(snapshot_interface=...)`. It checks the routed FF clock
population, SDF wire/primitive identity, UART synchronizer structure, reset
assert/release structure, original FF bindings and TX/RX storage together.
Its compact report explicitly retains pending recovery/removal, metastability,
original-path delay coverage and asynchronous global timing obligations.
The structural checks do not establish MTBF, external reset pulse width,
electrical closure or complete global STA. Natural SERV has passed this
integrated structural gate on both physical boards; full-flow qualification
remains incomplete until those timing obligations are resolved.

`derive_snapshot_rounds` now computes the logical exchange count for supported
LUT/FF netlists by a linear DAG traversal after partition selection. Edges
carry zero for local connectivity and one for a board crossing; original FF
outputs start a new launch path. External port ownership remains explicit.
Pure combinational cycles have no finite propagation proof and are rejected,
whereas feedback through an original FF is permitted. The result is a logical
round count only, conditional on stable launch inputs and local settling.

## Historical vendor reference investigation

## Deliverable and current status

Deliver one source-backed hardware configuration that implements a real RTL
workload through Phase 1--7, including the communication hardware. This is
offline vendor implementation and simulation qualification, not an assertion
of measured board operation. The platform is **not yet qualified**.

Reuse the existing MPS4 materializer, board overlay, open PCS/runtime transport,
and Vivado adapters. Do not replace the partitioning or timing algorithms as
part of this platform task. Preserve the current main defaults.

## Ordered acceptance gates

1. **Hardware facts.** Audit the MPS4 part/package, lane endpoints, cable mapping,
   reference-clock pins and distribution, reset pins/electrical levels, and
   initialization against public vendor documentation or reference sources.
   Identify exact document sections for bindings. A connector drawing alone
   does not prove cable lane mapping; a device database does not prove PCB
   wiring. If essential facts cannot be obtained, evaluate VCU118 instead;
   never fill missing facts with plausible constants.
2. **Minimal physical endpoint.** Confirm the selected device and required IP
   are supported by the available vendor tool/license. Implement the existing
   communication endpoint with actual GT primitives, complete clock/reset
   constraints and no unresolved black boxes. Check implementation, DRC,
   unconstrained paths and clock-domain crossings before running a large DUT.
3. **Communication contract.** Fix an explicit protocol configuration. Account
   for framing/encoding, payload throughput, CDC, buffering, reset/training,
   runtime synchronization and resource overhead. Distinguish device limits,
   selected engineering settings, derived timing and unmeasured link bounds.
   Verify transmission and macro-cycle semantics in simulation. Do not model
   packet buffering or link training as an undocumented constant delay.
4. **Full flow.** Register a naturally connected real RTL workload paired with
   the selected hardware configuration. Extend the benchmark contract where
   necessary to admit a source-backed hardware platform without pretending it
   is a contest case. Run the one-command complete flow with one physical
   seed; place and route DUT and communication logic together. Validate
   resource accounting, equivalence, route/transport legality, DRC, unrouted
   nets, and complete original-path coverage in final global timing.
5. **Release.** Publish the reproducible command, tool requirements, source
   references, compact terminal runtime/resource/global WNS/TNS summary and
   qualification limits. Mark completion only after gate 4 passes. Keep
   generated vendor products and experiment intermediates out of the repo;
   remove scratch when no longer active under the project lifecycle policy.

## Evidence boundary

The current record envelope is implemented in
`rtl/pcs/emuflow_xgmii_record_framer.sv`: HEADER, BODY, TERM occupy three PCS
clocks per 64-bit payload. Serial provider v3 validation now enforces this
necessary nominal-rate bound. For 156.25 MHz PCS and 50 MHz user clocks the
nominal service rate is 52.0833 million records/s versus 50 million arrivals/s.
Do not treat the remaining margin as proof of CDC, oscillator tolerance or
control-traffic feasibility; verify those separately for the selected endpoint.

### Vendor installation preflight

Run `scripts/reference_hardware_preflight.tcl` from an isolated scratch directory
with Vivado batch mode and `-tclargs PART OUTPUT_DIRECTORY`. It requires the
exact MPS4 or VCU118 part and GTYE4 channel/common inventory. Its compact TSV
explicitly marks license checkout, physical implementation and board wiring
as unverified; database availability alone is not an acceptance result.
The Tcl control-flow tests use stubs and are not vendor-tool evidence.

The device-view query has now also been exercised with actual Vivado 2025.1
(build 6140274): `xcvu13p-fhga2104-1-e` exposes 128 GTYE4 channels and 32
commons; `xcvu9p-flga2104-2L-e` exposes 120 channels and 30 commons. These are
device-site inventories, not counts of board-connected or usable link lanes.
The probe must open a PinPlanning I/O design before querying sites; an empty
project alone returns an empty site list. Implementation licensing and routed
communication qualification remain pending.

The checked-in 10G GT Wizard recipe has also generated both channel and
common-owner IP products for the exact MPS4 part in Vivado 2025.1. Actual
out-of-context synthesis was attempted but did not obtain a synthesis license;
it is not a successful implementation result. In project mode use one
`create_ip_run` call per XCI, then `launch_runs` and `wait_on_run`, checking
each run's completion and expected checkpoint. A trailing Tcl success message
after `synth_ip` is insufficient: the tool can report synthesis errors and
return control without a successful design. License availability must be
established before any full-flow hardware qualification run.

Public hardware facts, implementation choices, tool-derived properties and
unmeasured assumptions must remain distinguishable. A structural validator
passing is not proof of source accuracy. Vendor implementation validates the
specified constraints; it does not prove that those constraints match a PCB.
Offline qualification cannot establish measured BER, signal integrity,
power-on reliability or board-link latency. Report timing dependent on an
unmeasured bound as conditional on that bound, never as measured closure.

## Initial sources

- [Arm MPS4 TRM, Issue 02](https://documentation-service.arm.com/static/669a306a43b8ec1e18652768):
  sections 3.3 and 3.6.4 and appendix A.2.6. Existing public topology/pin model;
  complete clock/reset and cable bindings remain to be established.
- [AMD VCU118 UG1224](https://docs.amd.com/v/u/en-US/ug1224-vcu118-eval-bd):
  alternative real board, not a prequalified multi-board EmuFlow system.
- [VCU118 Ethernet examples](https://github.com/alexforencich/verilog-ethernet/tree/master/example/VCU118):
  reference RTL/constraints, not a direct replacement for emulation transport.
  The parent project is deprecated in favor of TAXI; evaluate licensing and
  maintenance before selecting any dependency.

## VCU118 fallback: facts located, qualification pending

`src/emuflow/board_vcu118.py` implements the documented QSFP1 endpoint and
board-service overlay primitives. The overlay is usable only with a matching
BoardDB and a caller-supplied GT site map; downstream device-map validation
remains mandatory. The four unit tests cover bindings, wrong part/pins,
missing/extra sites and ownership of returned data, not PCB qualification.

AMD UG1224 Table 3-18 independently agrees with the QSFP1 data and W9/W8
reference-clock bindings; its GPIO table agrees with L19 active-high CPU reset.
The programmable-clock prose mentions U32 inside the U38 subsection; preserve
the explicit U38 circuit/pin mapping rather than copying that inconsistent
reference designator. Oscillator frequency configuration still needs a
reproducible setup contract.

The public `example/VCU118/fpga_25g/fpga.xdc` reference has the following
explicit bindings. These are reference-design evidence to cross-check against
UG1224, not a substitute for vendor implementation or a complete BoardDB:

| Function | Package pins | Reference setting |
| --- | --- | --- |
| Fabric oscillator input | AY24 / AY23 | LVDS, 125 MHz |
| User reset | L19 | LVCMOS12, active-high input |
| QSFP1 reference clock | W9 / W8 | 156.25 MHz, reference source U38 |
| QSFP1 lane 0 TX / RX | V7,V6 / Y2,Y1 | GTY channel in bank 231 |
| QSFP1 lane 1 TX / RX | T7,T6 / W4,W3 | GTY channel in bank 231 |
| QSFP1 lane 2 TX / RX | P7,P6 / V2,V1 | GTY channel in bank 231 |
| QSFP1 lane 3 TX / RX | M7,M6 / U4,U3 | GTY channel in bank 231 |

The reference README selects `xcvu9p-flga2104-2L-e`. Its RTL drives QSFP module
control and instantiates `IBUFDS_GTE4`; it is a UDP example, not an emulation
transport implementation. In particular, do not copy the example's broad
false-path constraints into EmuFlow without validating the actual synchronizers.
The reference's 25G operating configuration must not silently replace the
existing EmuFlow 10G PCS configuration.

The two-board candidate uses a Black Box QSFP-H40G-CU1M-BB passive cable
between the two QSFP1 connectors. The manufacturer's page 3 X1/X2 pin tables
map TX1..4 to RX1..4 in both directions with preserved differential polarity;
page 4 identifies the one-meter model. Combined with UG1224 Table 3-22, this
establishes lane-index-preserving FPGA-to-FPGA wiring. It does not establish
cable propagation delay, BER, or GT equalization settings.

`build_vcu118_pair_boarddb(latency_cycles=...)` constructs this fixed pair with
DS890 Table 15 VU9P logic/memory capacities and a 75% utilization ceiling.
The 64-bit/50 MHz user interface and 156.25 MHz PCS at 10.3125 Gb/s are selected
transport settings, not values supplied by the cable datasheet. Latency has
no default and is labelled an unmeasured caller assumption, not a verified
bound. No arbitrary external DUT I/O budget is exposed. This candidate is for
integration work and cannot qualify hardware or final timing by itself.

The QSFP reference clock's documented power-on setting is 156.25 MHz; the
setup contract requires cold power-on and no subsequent oscillator writes.
The manual specifies 50 ppm tolerance, so separate boards must not be modeled
as phase-locked. The overlay now supplies MODSELL=0 (AM21), RESETL=1 (BA22),
and LPMODE=0 (AN21), matching the public reference RTL/XDC's LVCMOS18 outputs.
These constants are emitted in both the integration shell and the actual
Vivado DUT+serial top, with package constraints. Optional overlay
`static_outputs` owns these bindings; the wrapper manifest carries only the
relevant FPGA's outputs. Validation rejects collisions with clock, reset and
GT pins, duplicate IDs, unsafe identifiers and nonbinary values. This mechanism
is for constant outputs only, not reset sequencing or a generic GPIO engine.
Presence/interrupt monitoring, management I2C, and reset/clock-domain physical
validation remain incomplete. No module-control false-path exceptions are
copied from the upstream example. A pair of VCU118 boards
is an explicitly assembled reference setup, not an AMD-qualified complete
multi-FPGA emulation product.

Cable source: [Black Box QSFP-H40G-CUXM-BB datasheet, pages 3-4](https://cdn.blackbox.com/cms/docs/datasheets/data_sheet-qsfp-40g-dac-networking.pdf).

Sources: [reference XDC](https://github.com/alexforencich/verilog-ethernet/blob/master/example/VCU118/fpga_25g/fpga.xdc),
[reference RTL](https://github.com/alexforencich/verilog-ethernet/blob/master/example/VCU118/fpga_25g/rtl/fpga.v),
[reference README](https://github.com/alexforencich/verilog-ethernet/blob/master/example/VCU118/fpga_25g/README.md).
