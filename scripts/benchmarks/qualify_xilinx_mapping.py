#!/usr/bin/env python3
"""Compare one checked benchmark with one bounded UltraScale+ mapper strategy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from emuflow.benchmark import BenchmarkRun
from emuflow.errors import EmuFlowError
from emuflow.io import write_json
from emuflow.synthesis import (
    VALID_XILINX_MAPPING_STRATEGIES,
    run_xilinx_mapping_statistics,
)
from emuflow.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE


def _top_statistics(statistics: Mapping[str, Any], top: str) -> Mapping[str, Any]:
    modules = statistics.get("modules")
    if not isinstance(modules, dict):
        raise EmuFlowError("Yosys mapping statistics contain no modules")
    for candidate in (top, f"\\{top}"):
        value = modules.get(candidate)
        if isinstance(value, dict):
            return value
    if len(modules) == 1:
        value = next(iter(modules.values()))
        if isinstance(value, dict):
            return value
    raise EmuFlowError(f"Yosys mapping statistics are missing top {top!r}")


def qualify(
    benchmark_run: Path,
    source_root: Path,
    output: Path,
    *,
    yosys: Optional[str],
    strategy: str,
) -> dict[str, Any]:
    benchmark = BenchmarkRun.load(benchmark_run)
    synthesis = benchmark.value["synthesis"]
    if (
        benchmark.value.get("physical_mapping_profile")
        != XILINX_ULTRASCALEPLUS_OPEN_PROFILE
        or synthesis.get("family") != "xcup"
        or synthesis.get("policy") != "native"
    ):
        raise EmuFlowError(
            "mapping qualification requires the native checked UltraScale+ profile"
        )
    report = run_xilinx_mapping_statistics(
        benchmark.resolve_sources(source_root),
        benchmark.value["top"],
        output,
        executable=yosys,
        log_path=output.with_suffix(".log"),
        include_dirs=benchmark.resolve_include_dirs(source_root),
        defines=synthesis.get("defines", []),
        mapping_strategy=strategy,
    )
    top_statistics = _top_statistics(
        report.pop("statistics"), benchmark.value["top"]
    )
    cell_counts = top_statistics.get("num_cells_by_type")
    if not isinstance(cell_counts, dict):
        raise EmuFlowError("Yosys top statistics contain no primitive counts")
    compact = {
        **report,
        "benchmark_id": benchmark.value["id"],
        "design_id": benchmark.value["design_id"],
        "num_cells": top_statistics.get("num_cells"),
        "num_cells_by_type": dict(sorted(cell_counts.items())),
    }
    write_json(output, compact, compact=True)
    return compact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-run", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--yosys")
    parser.add_argument(
        "--strategy",
        choices=sorted(VALID_XILINX_MAPPING_STRATEGIES),
        required=True,
    )
    args = parser.parse_args()
    report = qualify(
        args.benchmark_run,
        args.source_root,
        args.output,
        yosys=args.yosys,
        strategy=args.strategy,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "strategy": report["mapping_strategy"],
                "elapsed_seconds": report["elapsed_seconds"],
                "num_cells": report["num_cells"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
