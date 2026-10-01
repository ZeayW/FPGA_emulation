#!/usr/bin/env python3
"""Synthetic ordinary-report producer for black-box runner tests."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def _csv(path: Path, fields, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("pass", "missing", "license", "tool", "capacity"), default="pass"
    )
    args = parser.parse_args()
    if args.mode == "license":
        print("license checkout failed: synthetic fixture", file=sys.stderr)
        return 23
    if args.mode == "tool":
        print("synthetic provider internal error", file=sys.stderr)
        return 17
    if args.mode == "capacity":
        print(
            "ERROR node cannot be placed on any FPGA because of [LUT].",
            file=sys.stdout,
        )
        return 1

    root = Path.cwd()
    _csv(
        root / "resource_summary.csv",
        ["resource", "demand"],
        [
            {"resource": "lut", "demand": 740},
            {"resource": "ff", "demand": 950},
            {"resource": "bram", "demand": 0},
            {"resource": "dsp", "demand": 0},
        ],
    )
    _csv(
        root / "partition_summary.csv",
        [
            "partition",
            "fpga",
            "lut_utilization",
            "ff_utilization",
            "bram_utilization",
            "dsp_utilization",
        ],
        [
            {
                "partition": "P0",
                "fpga": "F0",
                "lut_utilization": 0.00007,
                "ff_utilization": 0.00008,
                "bram_utilization": 0,
                "dsp_utilization": 0,
            },
            {
                "partition": "P1",
                "fpga": "F1",
                "lut_utilization": 0.00006,
                "ff_utilization": 0.00007,
                "bram_utilization": 0,
                "dsp_utilization": 0,
            },
        ],
    )
    _csv(
        root / "route_summary.csv",
        ["route", "source", "sinks", "effective_hops", "path_count", "maximum_tdm_ratio"],
        [
            {
                "route": "route-0",
                "source": "F0",
                "sinks": "F1",
                "effective_hops": 1,
                "path_count": 64,
                "maximum_tdm_ratio": 1,
            }
        ],
    )
    if args.mode != "missing":
        (root / "sr0_time.rpt").write_text(
            "Worst Cross FPGA Delay (ns): 12.5\n"
            "Cross FPGA Path Count: 64\n"
            "Maximum TDM Ratio: 1\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
