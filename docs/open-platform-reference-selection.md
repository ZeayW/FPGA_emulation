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

## Delivery gates

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
