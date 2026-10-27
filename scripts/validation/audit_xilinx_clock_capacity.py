#!/usr/bin/env python3
"""Independent, offline clock-sink capacity audit of a placed RWRoute input."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json, write_json
from emuflow.xilinx_rwroute import _is_clock_sink_pin


ULTRASCALE_CLOCKS_PER_REGION = 24


def audit_xilinx_clock_capacity(input_path: Path, architecture_path: Path) -> Dict[str, Any]:
    """Check the necessary 24-clock bound without loading a design as JSON.

    The TSV is streamed twice. Only internal clock nets and their clock-pin
    consumers enter the audit; a clock used as data does not consume another
    clock-region track at that data pin. This audit does not qualify complete
    clock routing, primary-clock distribution, or clock timing.
    """

    architecture = read_json(architecture_path)
    if architecture.get("schema") != "emuflow.archdb/v1":
        raise ValidationError("clock capacity architecture schema is invalid")
    metadata = {}
    clocks = set()
    sinks = defaultdict(set)
    digest = hashlib.sha256()
    with input_path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            fields = raw.decode("utf-8").rstrip("\n").split("\t")
            if fields[0] == "META" and len(fields) == 3:
                metadata[fields[1]] = fields[2]
            elif fields[0] == "NET" and len(fields) == 3 and fields[2] == "clock":
                clocks.add(fields[1])
            elif (
                fields[0] == "PIN" and len(fields) == 5
                and fields[1] in clocks and fields[4] == "sink"
                and _is_clock_sink_pin(fields[3])
            ):
                sinks[fields[1]].add(fields[2])
    if metadata.get("part") != architecture.get("part"):
        raise ValidationError("clock capacity input and architecture parts differ")
    wanted = set().union(*sinks.values()) if sinks else set()
    sites = {}
    with input_path.open(encoding="utf-8") as stream:
        for raw in stream:
            if not raw.startswith("CELL\t"):
                continue
            fields = raw.rstrip("\n").split("\t")
            if len(fields) != 6:
                raise ValidationError("clock capacity CELL row is invalid")
            if fields[1] in wanted:
                if fields[1] in sites:
                    raise ValidationError("clock capacity CELL identity is duplicated")
                sites[fields[1]] = fields[4]
    if set(sites) != wanted:
        raise ValidationError("clock capacity input lacks a clock consumer CELL")
    regions = {
        site["name"]: (site.get("physical_region") or {}).get("clock_region")
        for site in architecture["sites"]
    }
    demand = defaultdict(set)
    unresolved = {}
    for net, cells in sinks.items():
        for cell in cells:
            region = regions.get(sites[cell])
            if not isinstance(region, str) or not region:
                unresolved[cell] = sites[cell]
                continue
            demand[region].add(net)
    over = sorted(region for region, nets in demand.items()
                  if len(nets) > ULTRASCALE_CLOCKS_PER_REGION)
    return {
        "schema": "emuflow.xilinx-clock-region-capacity-audit/v1",
        "status": "fail" if over or unresolved else "pass",
        "scope": "necessary internal clock-sink placement bound; not complete clock routing",
        "capacity": ULTRASCALE_CLOCKS_PER_REGION,
        "coverage": {
            "complete": not unresolved,
            "unresolved_consumers": [
                {"cell": cell, "site": site} for cell, site in sorted(unresolved.items())
            ],
        },
        "source": {
            "rwroute_input": {"path": str(input_path), "sha256": digest.hexdigest()},
            "architecture": {"path": str(architecture_path), "sha256": file_sha256(architecture_path)},
            "route_input_identity": metadata,
        },
        "metrics": {
            "internal_clock_nets": len(sinks),
            "clock_sink_cells": len(wanted),
            "clock_regions": len(demand),
            "maximum_distinct_clocks": max(map(len, demand.values()), default=0),
            "over_capacity_regions": len(over),
            "unresolved_clock_sink_cells": len(unresolved),
        },
        "over_capacity_regions": over,
        "regions": [{"region": region, "clocks": sorted(nets)}
                    for region, nets in sorted(demand.items())],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rwroute-input", type=Path, required=True)
    parser.add_argument("--architecture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit_xilinx_clock_capacity(args.rwroute_input, args.architecture)
    write_json(args.out, report, compact=True)
    print(json.dumps({"status": report["status"], **report["metrics"]}), flush=True)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
