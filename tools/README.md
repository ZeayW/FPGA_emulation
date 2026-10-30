# Optional EmuFlow tools

Install the main repository first, then install this directory:

```sh
python3 -m pip install .
python3 -m pip install ./tools
emuflow-tools --help
emuflow-ppro-calibration --help
```

This distribution contains public-contest import/evaluation, standalone benchmark
qualification, complete-result auditing, reproducible input preparation,
public BoardDB materialization and PPro calibration tools. It
depends on `emuflow`; the runtime package does not depend on or install these tools.
For source checkout execution, use `PYTHONPATH=src/python:tools python3 -m emuflow_tools`.

Dataset catalogs and source fetchers remain repository inputs. Supply their
explicit paths when using an installed tool outside the checkout.

For an installed tool, select the checkout that owns the pinned fetch scripts
explicitly (the scripts are not installed in the main runtime):

```sh
emuflow-tools contest fetch-public \
  --matrix /path/to/checkout/benchmarks/contest_validation_matrix.json \
  --case-id eda2023.case1 --fetcher-root /path/to/checkout --out /path/to/new-run
```

A missing matrix-selected script is an error; the fetch command does not select
another script or source revision.

The benchmark runner uses the maintained `xcup`/`native` RTL frontend and writes
its artifacts under `frontend/phase1/`. `benchmark_report.json` uses report v2:
a compact Phase 1 summary and references to the detailed native evidence and IR.

Complete flow execution stays in `emuflow run` and `emuflow multi-fpga compile`.
Optional preparation and independent result audits use this distribution:

```sh
emuflow-tools design prepare design.json --out design.run.json
emuflow-tools validate-flow --flow runs/design --require-physical
emuflow-tools environment check --backend rapidwright
```

The runtime does not import the tool distribution. These commands are not
alternate production stage controllers.

`emuflow_tools.stage_clients` contains bounded native-stage clients and independent
replay used by qualification scripts and component tests. They are excluded from
the production runtime and expose no replacement stage command tree. The retained
small-design behavioral oracle remains in the runtime with explicit size bounds.

Domain packages under `frontend`, `partition`, `physical`, `timing` and `transport`
contain independent native-stage clients, artifact readers and qualification
helpers. The complete native flow uses its shared C++ owners directly. Runtime
JSON I/O reads ordinary control objects; `document_io.read_json` is the explicit
tool API for opening typed artifact selectors. Both use the runtime's single
atomic writer and file-identity implementation.
