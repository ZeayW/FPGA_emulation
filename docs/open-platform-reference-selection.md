# Reference-first platform redevelopment

Status: research and feasibility gates in progress; no replacement selected.

## Acceptance correction

The dual-ULX3S UART prototype has finite-trace functional and physical evidence,
but is not an accepted practical emulation platform. Its completion claim is
withdrawn. Preserve its terminal evidence as prototype evidence, not performance
qualification. Do not optimize or rerun that architecture as the default plan.

A real PCB and an open bitstream toolchain do not establish that a newly invented
multi-board protocol is a credible research platform. Select an existing
reference implementation before introducing new transport RTL.

## Initial source matrix

These are inspected primary sources, not yet a pinned implementation selection.
Upstream links to moving branches must be pinned before integration.

| Reference | Verified relevance | Remaining gap / reuse boundary |
| --- | --- | --- |
| [FireAxe documentation](https://docs.fires.im/en/main/Advanced-Usage/FireAxe-Partitioning-onto-Multiple-FPGAs/index.html) and [overview](https://docs.fires.im/en/1.21.0/Advanced-Usage/FireAxe-Partitioning-onto-Multiple-FPGAs/FireAxe-Overview.html) | Actual multi-FPGA simulation framework; explicit exact, fast and NoC modes, including combinational boundaries | Inspect bridges and supported build platforms; documentation is not evidence of an ECP5 open-toolchain port. Architectural reference, not a drop-in backend. |
| [LiteICLink](https://github.com/enjoy-digital/liteiclink) | BSD-2-Clause inter-chip cores; ECP5 SerDes implementation | A PHY is not a complete reliable emulation transport. Audit streaming semantics, buffering, alignment, error detection and reset behavior. |
| [ECPIX-5 SerDes bench](https://github.com/enjoy-digital/liteiclink/blob/master/bench/serdes/ecpix5.py) | Existing board-specific pins, reference-clock selection and initialization; default Trellis build and 2.5 Gbps configuration; UARTBone used for control | Bench sends a comma/counter pattern, not a lossless application channel. Confirm cable topology and hardware measurements; do not infer BER or arbitrary payload reliability from LEDs. |
| [ECPIX-5 hardware documentation](https://docs.lambdaconcept.com/ecpix-5/index.html) | Published schematic entry, open-toolchain support, Ethernet and SATA connectors | Audit exact FPGA variant, board revision, transceiver routing, coupling and reference clocks before choosing this board. Do not project ULX3S pin/device assumptions onto it. |
| [LiteEth](https://github.com/enjoy-digital/liteeth) | BSD-2-Clause Ethernet MAC, UDP streaming, ECP5 RGMII support | Packet loss/flow control and packet latency require explicit semantics. Ethernet line rate is not emulation rate or guaranteed fixed delay. |
| [EMiX](https://arxiv.org/abs/2604.27012) | Paper reports multi-FPGA RISC-V emulation on eight U55c devices | Abstract says open-source release is planned; not evidence that reusable sources or a commercial-tool-free build are available. |

## Stop conditions before implementation

### Code-level audit and upstream test reproduction

Inspected LiteICLink revision `8a4ce305510614266dad462dbe6b1f154f7487f4`
(BSD-2-Clause). Its unmodified nine upstream tests passed with LiteX revision
`743825d3f625d3047039dbea0d9f47b0f7785135`, Migen 0.9.2 and LiteEth 2024.12.
Using LiteX 2024.12 instead produced two construction errors because the newer
core needs `CSRStorage.wr_stb`; do not patch away that dependency requirement.
These are Python/Migen tests, not physical builds or measured link tests.

- [`SerDesECP5.add_stream_endpoints`](https://github.com/enjoy-digital/liteiclink/blob/8a4ce305510614266dad462dbe6b1f154f7487f4/liteiclink/serdes/serdes_ecp5.py)
  drives TX ready and RX valid constantly. It exposes continuously clocked
  symbols, not a remotely backpressured lossless packet channel. Reusing this
  block alone would leave most of the transport problem unsolved.
- The pinned [`SerWB demo`](https://github.com/enjoy-digital/liteiclink/blob/8a4ce305510614266dad462dbe6b1f154f7487f4/bench/serwb/demo/README.md)
  is stronger system evidence: it documents an ECPIX-5/iCEBreaker three-wire
  assembly and remote register/SRAM operations. Its source supplies board
  connectors, forwarded clock, initialization and Wishbone/Etherbone integration.
  It is a bus-extension example, not a general emulation engine.
- `serwb/genphy.py` sends one serial bit per clock using a forwarded-clock
  relationship. The demo uses 25 MHz. `serwb/packet.py` emits two 32-bit header
  words plus payload, without an end-to-end payload CRC field. Local FIFO
  ready/valid is not proof of remote overload recovery or corrupt-word rejection.
  `SERIOCore` is change-driven I/O propagation and is not a cycle-token protocol.
- The nine tests cover construction/OOB defaults, digital word alignment,
  scrambling, six-word Wishbone readback through a fake PHY and simulated
  initialization success/failure. They do not establish sustained two-board
  throughput, physical CDC closure, payload integrity or autonomous DUT execution.

FireAxe's [platform documentation](https://docs.fires.im/en/1.21.0/Advanced-Usage/FireAxe-Partitioning-onto-Multiple-FPGAs/FireAxe-Overview.html)
explicitly uses peer-to-peer PCIe on F1 or Aurora over QSFP on local FPGAs.
These are not a verified commercial-IP-free ECP5 backend. The
[ISCA 2024 paper](https://joonho3020.github.io/assets/ISCA2024-FireAxe.pdf)
provides the relevant execution reference: queued cycle tokens, output production
when dependent inputs are present and target advancement when token obligations
are satisfied. Its reported MHz results belong to its own hardware/workloads,
not to a proposed ECP5 port. Reusing this model would require explicit EmuFlow
adaptation and validation, not simply connecting wires to SerDes.

**Selection remains open:** neither a continuous-symbol SerDes bench nor the
SerWB bus demo yet meets the complete requested platform contract. Next inspect
the existing Ethernet framing/flow-control alternatives and reproduce the
selected upstream board build before introducing any new EmuFlow transport.

### Ethernet candidate: implementation evidence, not yet selected

LiteEth `8c9150ff121cb3148d8ea26ce3b1c5200479848d` with the LiteX revision above
passed 31 unmodified tests from `test_ecp5rgmii`, `test_crc`, `test_mac_packet`,
`test_stream` and `test_udp`. These cover digital RGMII behavior, CRC loopback,
complete packet buffering, late-error/oversize drops and UDP streaming. The
source provides MAC FCS checking and packet error propagation, but UDP streaming
does not itself supply end-to-end acknowledgments, loss recovery or target-cycle
identity. An EmuFlow consumer must never commit on a partial or dropped packet.

LiteX-Boards `3e606b50b3f14e31d4bf565e5ba3cac3d21f841d` provides a concrete
[ECPIX-5 target](https://github.com/litex-hub/litex-boards/blob/3e606b50b3f14e31d4bf565e5ba3cac3d21f841d/litex_boards/targets/lambdaconcept_ecpix5.py)
and pin platform: LFE5UM5G-85F-8BG554I, onboard RGMII Ethernet and Trellis build.
This is not the ULX3S LFE5U/CABGA381 device. The manufacturer publishes the
[R02 schematic](https://docs.lambdaconcept.com/ecpix-5/_static/resources/SCH_ECPIX-5_R02.PDF)
with KSZ9031 Ethernet PHY and RJ45 connection; exact timing/strap audit is still
required before selecting it.

An unmodified target configuration (`--with-etherbone --cpu-type None
--integrated-main-ram-size 32768 --toolchain trellis --device 85F`, 75 MHz,
physical seed 1 and strict nextpnr timing) was generated and actually submitted
to Yosys/nextpnr/ecppack. Synthesis passed, but nextpnr terminated at dedicated
clock routing: it could not route `crg_clkout0` to `ECLKSYNCB.ECLKI`. No bitstream
was produced. The pre-route RX clock estimate also failed 125 MHz and is not a
post-route timing result. The suggested `--allow-fabric-eclk` workaround was
**not** used because it introduces indeterminate skew. The upstream clock tree
retains DDR-oriented ECLKSYNCB/CLKDIVF even with integrated main RAM; whether
that configuration explains the failure remains a diagnosis, not an established
root cause. This failure does not prove every ECPIX-5 configuration is broken.
The attempt is terminal; only its compact failure evidence remains.

Restoring the target's default external DDR configuration removed the dedicated
edge-clock routing failure and allowed routing to finish. Strict timing still
failed: the Ethernet RX domain achieved 87.49 MHz against 125 MHz; the system
domain achieved 93.84 MHz against 75 MHz. This isolates two different problems;
it does not justify fabric routing of an edge clock.

The upstream `SoC.add_etherbone(data_width=32)` option runs the protocol/MAC
datapath in the system domain and retains the PHY clock domain through upstream
CDC/width-conversion logic. LiteX-Boards' `colorlight_5a_75x` ECP5 target already
uses that option. Applying only this configuration override to ECPIX-5 (external
DDR, 75 MHz system, seed 1) produced 189.00 MHz RX timing, passing 125 MHz, but
system timing was 73.09 MHz, failing 75 MHz. This is an explicit adaptation of
an existing option, not an unmodified default-target pass.

The explicit 50 MHz system configuration passed Yosys, strict nextpnr and
ecppack in 187.79 seconds, with physical seed 1. Post-route RX timing achieved
181.42 MHz against 125 MHz, system timing 66.45 MHz against 50 MHz, initialization
381.10 MHz against 25 MHz and input-clock logic 461.47 MHz against 100 MHz.
The build produced a bitstream, using 11,578 COMB and 4,748 FF. Neither
`--allow-fabric-eclk` nor `--timing-allow-fail` was used. Its 32-bit internal raw
bandwidth is 1.6 Gbit/s; that arithmetic is not sustained packet throughput.
This qualifies a configuration-level upstream endpoint build, **not** a
dual-board EmuFlow engine, external-interface timing signoff or measured hardware.

The configuration override is a subclass of the pinned `BaseSoC` that forwards
`add_etherbone` to the upstream implementation with `data_width=32`. Construction
uses `device="85F", toolchain="trellis", sys_clk_freq=50e6,
with_etherbone=True, cpu_type=None, uart_name="serial"`; default external DDR is
retained. `Builder.build(..., timingstrict=True)` supplies the strict build.
The native tool set was Yosys 0.69+10, nextpnr 0.11.1-25-ge47c2589 and ecppack
from OSS CAD Suite 2026-09-10. Failed attempts and routed scratch are removed
after process termination; compact terminal identities and summaries remain.

The R02 schematic advertises all RGMII link capabilities, while the inspected
target does not enable LiteEth's optional dynamic-link datapath. A selected
platform must require and verify gigabit/full-duplex negotiation or explicitly
configure the supported speed handling; silently assuming 1 Gbit/s after any
link-up is insufficient. PHY skew/MDIO settings and board timing remain open
items. CPU-free builds with DDR do not prove DDR runtime initialization, and
nextpnr's ignored false-path constraints are not CDC/electrical signoff.

Ethernet supplies stronger standardized physical/interconnect references than
custom GPIO wiring. It still does not supply an off-the-shelf complete EmuFlow
engine. Selection requires resolving the build issue, auditing PHY timing and
defining the minimal reference-backed execution adapter without reinstating
per-cycle host communication.

1. Identify the exact upstream code and license for board support, physical link,
   link initialization and transport/control. Record what is reused unchanged,
   adapted or still missing. A paper-only diagram is insufficient for claimed
   direct reuse.
2. Inspect the emulation execution model separately from the PHY. No slow host
   transaction on every DUT cycle by default. Determine input buffering, output
   draining and autonomous execution semantics from a concrete reference.
3. Reproduce an upstream open-toolchain build without commercial IP, then test
   its actual interface and fault behavior. A local loopback is not a two-board
   electrical qualification.
4. Establish a budget before implementing an EmuFlow adapter. For every round,
   count payload bits, headers/coding, direction concurrency, pipeline latency,
   acknowledgment latency and local logic delay. Report useful throughput and
   effective DUT cycles/second, not only line rate or positive runtime slack.
5. If the available references do not support the requested practical platform,
   present the missing engineering and alternatives to the user. Do not silently
   invent a replacement and describe it as a mature reference design.

## Budget baseline and intended comparison

The rejected prototype's host divider is 217 at 25 MHz and its link divider is
16. A 32-bit application word occupies a 15-byte record / 150 UART wire bits:
approximately 1.302 ms at the host and 96 us on the board link, before gaps or
handshakes. The implementation sends inputs, EXEC, outputs and DONE for each DUT
cycle. This is a structural bottleneck, not a unit conversion bug.

The inspected SerDes bench's 2.5 Gbps setting is only a candidate line rate.
It does not establish payload throughput, cable reliability or a target
macrocycle. Do not promise a numerical speedup until those terms are measured
or bounded under explicit assumptions. Select the intended operating budget
before accepting implementation, not after discovering its achieved speed.

For a transparent serialization-only comparison, six 32-bit words in one SerWB
packet require `(6 + 2) * 40 = 320` encoded bits: 12.8 us at the demo's 25 MHz
serial clock. Two sequential dependency rounds require at least 25.6 us even
with concurrent opposite directions, before Etherbone, gaps, control, logic or
CDC costs. Thus roughly 39,063 DUT cycles/s is an optimistic ceiling for that
specific hypothetical traffic pattern, not a measured platform result. At
2.5 Gbps, 8b/10b alone leaves at most 2 Gbps; a complete higher-layer framing
and latency contract is still missing. These bounds prevent treating line-rate
improvement as demonstrated emulation throughput.

For 1 Gbps Ethernet, an untagged IPv4/UDP packet carrying six 32-bit words has
24 bytes of payload plus 14 MAC, 20 IPv4, 8 UDP and 4 FCS bytes. Including eight
preamble/SFD and twelve inter-frame-gap byte times yields 90 byte times / 720 ns.
Two sequential full-duplex exchange rounds therefore cost at least 1.44 us in
serialization alone. This is a lower bound, not a delay guarantee: PHY latency,
MAC buffering, clock crossings, ARP, congestion, application headers, local
evaluation and any commit handshake are additional. Standard line rate must
not be converted into a fabricated fixed BoardDB latency.

## Delivery gates

### Decision before new transport RTL

The inspected references do not yet provide a directly reusable, complete
commercial-tool-free ECP5 multi-FPGA emulation engine. The feasible candidate is
**two ECPIX-5 boards using their existing gigabit Ethernet ports and LiteEth**,
with the upstream board pins, PHY/MAC/CDC implementation and strict open build.
FireAxe supplies an execution-model reference, not a reusable Ethernet adapter.

This candidate still requires EmuFlow-specific target-cycle/round identity,
complete-packet admission, duplicate/stale rejection, bounded buffering and a
fault policy that cannot silently advance the DUT after loss or reset. Host
input/output should be batched and target advancement autonomous. These are
new integration work, not capabilities established by the Etherbone build.
Do not quietly label the combination an existing mature emulation architecture.
Confirm acceptance of this reference-backed integration scope with the user
before adding its transport RTL. No claim of measured link latency, BER or
throughput is possible without physical boards.

- A: source and missing-component audit, with justified platform selection.
- B: unmodified upstream endpoint build and independent interface/fault tests.
- C: minimal EmuFlow adaptation, resource/clock/link contracts and timing binding.
- D: real naturally connected RTL end-to-end, one physical seed; functional
  equivalence, routed physical implementation, global path accounting,
  target/runtime timing, sustained macrocycle rate and host/link overhead.
- E: documentation, small regression suite and compact terminal evidence; push
  the development branch. No performance-qualified completion based solely on
  an observed-event deadline or a finite idle/reset trace.

No real boards are available in this task. Offline implementation is distinct
from measured electrical operation, BER, metastability and worst-case PVT.
