# Open-source RTL benchmark catalog

The reproducible source catalog is stored in
`benchmarks/rtl_catalog.json`. Each entry pins an upstream Git commit, license,
top-level module, source paths, approximate scale, and current EmuFlow
readiness. Third-party source is fetched into the ignored
`third_party/rtl/` directory rather than copied into this repository.

List the catalog:

```bash
python3 scripts/benchmarks/fetch.py list
```

Fetch one design:

```bash
python3 scripts/benchmarks/fetch.py fetch picorv32
```

The checked-in run contracts cover SERV L1, PicoRV32 L2, secworks AES L3,
and the current Koios L5 fixtures. Run the AES progression rung with:

```bash
python3 scripts/benchmarks/fetch.py fetch secworks_aes
emuflow benchmark benchmarks/runs/secworks_aes_l3.json \
  --source-root third_party/rtl/secworks_aes \
  --out build/secworks-aes-l3
```

Its `logic-only` policy is an open-flow integration baseline, not a claim that
its mapped QoR matches the upstream Kintex-7 result.

## Recommended progression

The source catalog and fetcher cover the following progression:

| Level | Design | Approximate upstream scale | Role |
| --- | --- | --- | --- |
| L1 | SERV | about 125 LUT and 164 FF on Artix-7 | Fast real-RTL regression |
| L2 | PicoRV32 | 761-2019 LUT and 442-1085 FF, plus LUTRAM | Main Phase 2 growth target |
| L3 | secworks AES | about 3020 LUT and 2992 FF on Kintex-7 | Medium routing/density and forced partition stress |
| L4 | VTR classic and Ibex | mixed; Ibex is 16.85-66.02 kGE by configuration | Frontend diversity, SystemVerilog and dependency stress |
| L5-L6 | Koios 2.0 | 40 medium and large DL designs | Large RTL, BRAM/DSP and multi-FPGA system stress |
| L7 | NVDLA nvdlav1 | 3,123,117 synthesized cells; 1,825,473 LUTs and 915,739 FFs | Real connected million-cell stress |

The progression is an acceptance ladder, not a checked-in result table.
Machine configuration, exact synthesis counts, QoR measurements, logs, and
artifact hashes are maintained in local experiment records.

Replicated-core and artificially coupled RTL harnesses are deliberately not
part of this catalog. They can be useful private stress fixtures, but their
regular structure and invented communication do not qualify them for Phase 6
provider promotion or final WNS/TNS claims. Materially sized acceptance runs
must use a naturally connected upstream RTL design.

## Canonical full-flow combinations

RTL workload selection and BoardDB topology selection are independent axes.
For complete Phase 1--7 validation, use the real workload from this catalog
and replace the generic platform in its Phase 1 run contract with a
hash-pinned contest-derived BoardDB.  Contest nodes and nets remain inputs to
the communication-algorithm adapters only; they are never treated as RTL.

The only authoritative full-flow registry is
`benchmarks/end_to_end_validation_matrix.json`.  The initial set uses Koios
DLA medium with EDA 2023 case6, case7, and case9 separately.  The full
combination ID, such as `koios-dla-medium-l5__eda2023-case6`, must be used in
reports and artifacts; a bare contest case name is ambiguous.

The separate `benchmarks/contest_validation_matrix.json` tracks raw public
contest fetch/import/evaluation coverage.  Success there does not constitute
RTL synthesis, FPGA-internal place-and-route, Phase 7 closure, or WNS/TNS
evidence.  Validate the end-to-end registry with:

```bash
emuflow benchmark-matrix-validate \
  benchmarks/end_to_end_validation_matrix.json
```

The checked-in entries start in `planned` state.  Only content-addressed,
independently replayable Phase 1--7/7C evidence for baseline,
placement-aware, and Chimew across the required physical seed set (seed 1 by
default) can change a
case to `qualified`.  Final decisions use whole-design target-clock WNS/TNS;
per-FPGA timing and Phase 6 costs are diagnostics.

## Current flow gaps exposed by larger designs

The present Phase 2 smoke-test path accepts LUT1-LUT6 and primary FF
placements. Real synthesis of the catalog designs will also exercise:

- CARRY4/CARRY8 conversion;
- LUTRAM and possibly BRAM mapping;
- SRL and wide-mux primitives;
- larger, non-sampled ArchitectureDB regions;
- complete FDCE/FDPE/FDSE control-pin models;
- scalable OpenPARF filler generation and detailed placement;
- fixed clock, IO, memory, DSP, and macro constraints.

For an initial PicoRV32 run, a logic-only Yosys policy can disable carry,
DSP, BRAM, LUTRAM, SRL, and wide-LUT inference. That provides a large LUT/FF
placement test while the native UltraScale+ packer is implemented. It should
be treated as a placement regression configuration, not as the final QoR
configuration.

Koios remains useful for intermediate BRAM/DSP coverage, while the official
NVDLA top is the final scale target. Compile one Koios source file at a time:
several variants reuse top-level module names. Native BRAM/DSP preservation is
required before interpreting logic-only Koios results as representative QoR.
