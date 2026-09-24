# Phase 7 AMF-Placer public-release capability audit

This document records the P0/P1 audit of the public basic implementation of
[AMF-Placer 2.0](https://github.com/zslwyuan/AMF-Placer), pinned at upstream
revision `70d98288153046ea4fd07190e748b6530e3042f5`. It does not qualify AMF as
an EmuFlow Phase 7 provider and does not reproduce claims made for the separate
advanced AMF release.

## Decision

The public code is a serious placement candidate rather than only a global
coordinate generator. Its executable flow contains mixed-size analytical
global placement, progressive macro legalization, incremental/final CLB
packing, exception rip-up, and timing-driven detailed-placement passes.

It is not directly usable by the current open XCVU19P backend:

- design and device inputs use AMF's Vivado-extracted text archives and
  compatibility tables;
- the result is emitted as a Vivado `place_cell` Tcl script;
- `MUXF9` and `URAM288` are absent from the public core type and packing model;
- the public source contains clock-region heuristics, but clock tree synthesis
  is a stated TODO and some clock-region logic is VCU108-specific;
- no explicit SLR object or multi-SLR legality model was found in the audited
  public source; and
- the documented reference platform is VCU108/UltraScale, not a complete
  XCVU19P/UltraScale+ qualification.

The route therefore remains fail-closed. A future adapter must translate
EmuFlow's mapped Yosys JSON and ArchitectureDB without invoking Vivado, import
AMF's exact packing/site/BEL result without executing Tcl, and pass the existing
independent placement/cascade validators. It must also reject unsupported
primitive, clock, or SLR requirements before launching AMF.

## Current primitive matrix

| EmuFlow primitive | Public AMF status | Required work |
|---|---|---|
| LUT1--LUT6, LUT6_2 | `native_supported` | Input/output adaptation and XCVU19P qualification |
| FDCE, FDPE, FDRE, FDSE | `native_supported` | Preserve control-set and alias-net semantics |
| CARRY8 | `native_supported` | Preserve chain and exact site adjacency |
| DSP48E2 | `native_supported` | XCVU19P column/cascade qualification |
| MUXF7, MUXF8 | `native_supported` | Exact BEL import and validation |
| RAMB18E2, RAMB36E2 | `native_supported` | Half/full-site and cascade qualification |
| GND, VCC | `adapter_required` | Lower physical constant cells to AMF constant nets |
| MUXF9 | `core_missing` | Add core type, macro recognition, packing, and BEL mapping |
| URAM288 | `core_missing` | Add core type, device compatibility, legalization, cascade, and result emission |
| Any other primitive | `unverified` | Audit before use; never silently map or drop it |

The checked-in probe emits the shared
`emuflow.xilinx-placer-capability/v1` report and exposes only four states:
`native_supported`, `adapter_required`, `core_missing`, and `unverified`.
Unknown cells remain `unverified`; no paper or README claim promotes them.
Every `adapter_required` entry remains `missing` until a focused adapter test
passes, so the common qualification gate rejects the candidate today.

## Import, placement, and output audit

| Capability | Status | Public-source evidence |
|---|---|---|
| Netlist import | `adapter_required` | `DesignInfo.cc` parses `vivado extracted design information file` |
| Device import | `adapter_required` | `DeviceInfo.cc` parses the extracted site/BEL/clock-region archive |
| Mixed-size global placement | `native_supported` | `globalPlacement/` and the public `AMFPlacer::run()` flow |
| Packing and macro legalization | `native_supported` | `legalization/`, `InitialPacker`, and `ParallelCLBPacker` |
| Detailed placement | `native_supported` | timing-driven shortest-path/swap passes in `ParallelCLBPacker.cc` |
| XCVU19P UltraScale+ | `unverified` | public reference input is VCU108 and the docs warn of portability work |
| Multi-SLR legality | `core_missing` | no explicit SLR model in the audited public source |
| Clock legality | `unverified` | clock-region checks exist; CTS is still a public TODO |
| Vivado-free EmuFlow execution | `adapter_required` | AMF can run on extracted files, but current extraction/loading is Vivado Tcl |

## Adapter boundary

`emuflow.amf_placer.build_amf_placer_adapter_contract()` defines the internal
contract. No user-facing CLI or default is added in this branch. The future
adapter must own three conversions:

1. `emuflow-to-amf-design-v1`: normalized mapped JSON to AMF cell/pin/net data;
2. `archdb-to-amf-device-v1`: ArchitectureDB to AMF site/BEL/compatibility and
   clock-region data; and
3. `amf-result-to-emuflow-v1`: AMF final packing/placement to
   `emuflow.packed-site-netlist/v1` and `emuflow.xilinx-placement/v1`.

The contract permanently rejects runtime Vivado extraction or Tcl execution,
lost/duplicated cells, unsupported primitives, unqualified multi-SLR or clock
requirements, and placements that fail EmuFlow's independent exact validator.

## P1 outcome and next cost

The deterministic source/capability probe and its tests are executable now. A real resource
fixture placement is intentionally not claimed: even a LUT/FF-only executable
roundtrip still needs all three adapters, while the complete current primitive
profile additionally needs MUXF9 and URAM core work.

Estimated engineering cost, before performance qualification:

- design/device/result adapters and small LUT/FF/CARRY fixture: about 1--2
  engineer-weeks;
- MUXF9 and URAM core support plus focused legality tests: about 1--2 weeks;
- XCVU19P clock-region and multi-SLR modeling/qualification: about 2--4 weeks;
- medium-design robustness and Phase 7 routing/timing qualification: additional
  work after the correctness gates above.

These estimates assume the public basic release only. They do not assume
access to, or capabilities from, the separately distributed advanced release.
