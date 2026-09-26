# Root-built flow engines

This directory contains the editable source of the open-source engines that
participate directly in the EmuFlow root build:

- `yosys`: logic synthesis and ABC technology mapping;
- `openroad`: OpenROAD and TritonPart, including its API-coupled OpenSTA copy;
- `opensta`: standalone OpenSTA 3.1 used by EmuFlow timing flows;
- `repart`: multilevel FPGA-aware hypergraph partitioning and replication;
- `openparf`: FPGA placement operators and flow; and
- `vtr`: VPR exact architecture packing, placement, and detailed routing; and
- `cudd`: the decision-diagram dependency required by OpenSTA.

Each engine retains its upstream license and an `EMUFLOW_PROVENANCE.md` record
with the imported revision and EmuFlow-specific changes. Compiled executables
and libraries are disposable products below `build/`; the tracked source here
is the implementation.

External RTL benchmarks and historical upstream patch records remain under
`third_party/` because they are not EmuFlow flow engines.
