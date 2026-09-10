# Credible reference hardware platform

## Active target: fully open ECP5 reference platform

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
2. **Open physical endpoint (pending).** Yosys `synth_ecp5`, nextpnr-ecp5
   `--85k --package CABGA381 --speed 6`, and Project Trellis `ecppack` must
   produce a routed design and bitstream without commercial tools. Check the
   package pin database and all clock/I/O constraints, not only process exit.
3. **Transport (pending).** Implement portable framed GPIO communication with
   CDC-safe receive, reset/recovery, integrity checks and backpressure. Verify
   independent clocks and clock tolerance. Virtual design state must advance
   only after all required transfers are complete; variable transport latency
   must not be hidden behind an arbitrary fixed cycle count.
4. **EmuFlow integration (pending).** Add an ECP5 physical backend, family-aware
   LUT4/FF/RAM/DSP accounting including communication overhead, and physical
   timing binding. Never reinterpret VTR or AMD resource counts as ECP5 counts.
   Physical segment timing and protocol timing need explicit global analysis;
   handshake progress and local Fmax are not whole-design WNS/TNS.
5. **Acceptance (pending).** Real RTL through Phase 1--7, one physical seed,
   complete original-path timing coverage, macro-cycle equivalence, checked
   placement/routing and bitstream production. Keep only compact terminal
   evidence. Offline success does not establish measured signal integrity,
   BER, cable delay or reliable power-on behavior.

Source revision: `emard/ulx3s` commit
`6a92cec6b177191c5b0f80e260013a1f8ec147dd`; manual section “Connectors” and
`doc/constraints/ulx3s_v20.lpf` (declared compatible with v3.0.x).
The wiring profile explicitly rejects claims of full-flow qualification today.

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
