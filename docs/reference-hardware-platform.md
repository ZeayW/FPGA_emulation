# Credible reference hardware platform

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

Before selecting a two-board configuration, still establish the cable lane
mapping, oscillator setup assumptions, GT configuration at the chosen line
rate, module controls, and reset/clock-domain behavior. A pair of VCU118 boards
is an explicitly assembled reference setup, not an AMD-qualified complete
multi-FPGA emulation product.

Sources: [reference XDC](https://github.com/alexforencich/verilog-ethernet/blob/master/example/VCU118/fpga_25g/fpga.xdc),
[reference RTL](https://github.com/alexforencich/verilog-ethernet/blob/master/example/VCU118/fpga_25g/rtl/fpga.v),
[reference README](https://github.com/alexforencich/verilog-ethernet/blob/master/example/VCU118/fpga_25g/README.md).
