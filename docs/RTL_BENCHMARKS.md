# RTL benchmarks

[The source catalog](../benchmarks/rtl_catalog.json) records upstream design
revisions, licenses, top modules, source paths, and readiness metadata.
Fetched sources live in the ignored `third_party/rtl/` directory.

List and fetch designs:

```bash
python3 scripts/benchmarks/fetch.py list
python3 scripts/benchmarks/fetch.py fetch picorv32
```

Use a checked-in workload contract with its pinned source checkout:

```bash
python3 scripts/benchmarks/fetch.py fetch secworks_aes
emuflow benchmark benchmarks/runs/secworks_aes_l3.json \
  --source-root third_party/rtl/secworks_aes \
  --out build/secworks-aes
```

A single-workload benchmark command is not automatically a complete multi-FPGA
physical comparison. Consult its run contract, selected platform, and terminal
validator to identify the completed boundary.

## Workloads and platforms

Real RTL supplies logic, state, clocks, and resource demand. BoardDB supplies
FPGA topology and communication constraints. These are independent inputs;
never treat a contest communication graph as a synthesized circuit.

The catalog includes small RTL such as SERV, PicoRV32, and AES, along with
larger Koios, VeeR, and NVDLA sources. Catalog membership or a pinned source
revision does not establish that every mapping/device/platform combination is
supported. RAM, DSP, carry, wide-mux, and macro constraints must match the
selected physical backend. Do not flatten unsupported hard blocks into soft
logic silently and report unchanged hardware capacity.

Use a naturally connected design for final provider comparisons.
Replicated-core or artificially coupled harnesses may be private stress
fixtures, but are not substitutes for representative full-flow evidence.

## Registries

- [RTL catalog](../benchmarks/rtl_catalog.json): upstream source definitions.
- [Contest matrix](../benchmarks/contest_validation_matrix.json):
  raw communication-algorithm inputs.

Identify each comparison by its full workload/platform combination, not a bare
contest case number. A registry's planned entry is not a completed result.
A qualified entry needs the declared complete physical and global-timing gates.
The VTR-only KOIOS medium run specification and its planned full-flow matrix
are retired, including `benchmark-matrix-validate`. Do not reinterpret their
identities as Xilinx experiments. Other RTL catalog entries remain available.

## Outputs

Machine configuration, raw tool products, measured resource counts, QoR tables,
and experiment histories stay outside the repository. Report complete original-
path coverage, mapping/architecture identity, seed, runtime, DRC/unrouted counts,
and target/runtime global WNS/TNS in the terminal comparison summary.

See [validation requirements](VALIDATION.md).
