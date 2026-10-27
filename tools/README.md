# Optional EmuFlow tools

Install the main repository first, then install this directory:

```sh
python3 -m pip install .
python3 -m pip install ./tools
emuflow-tools --help
emuflow-ppro-calibration --help
```

This distribution contains public-contest import/evaluation, standalone benchmark
qualification, public BoardDB materialization and PPro calibration tools. It
depends on `emuflow`; the runtime package does not depend on or install these tools.
For source checkout execution, use `PYTHONPATH=src:tools python3 -m emuflow_tools`.

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
