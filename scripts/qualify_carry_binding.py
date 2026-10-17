#!/usr/bin/env python3
"""Two-cell physical carry-pin qualification, never whole-flow evidence."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from emuflow.io import read_json, write_json
from emuflow.xilinx_rwroute import export_rwroute_input, iter_xilinx_route_nets, run_rwroute

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--part", required=True)
parser.add_argument("--carry-site", required=True)
parser.add_argument("--source-site", required=True)
parser.add_argument("--mode", choices=("fabric", "cascade"), required=True)
parser.add_argument("--carry-type", choices=("SINGLE_CY8", "DUAL_CY4"), default="SINGLE_CY8")
for name in ("rapidwright-jar", "java", "device-data", "timing-data"):
    parser.add_argument("--" + name, type=Path, required=True)
args = parser.parse_args()
if args.out.exists():
    parser.error("qualification requires a new output directory")
args.out.mkdir(parents=True)
source = ({"type": "FDRE", "port_directions": {"Q": "output"},
           "connections": {"Q": [10]}} if args.mode == "fabric" else
          {"type": "CARRY8", "parameters": {"CARRY_TYPE": "SINGLE_CY8"},
           "port_directions": {"CO": "output"}, "connections": {"CO": ["x"] * 7 + [10]}})
mapped = {"modules": {"probe": {"cells": {
    "source": source,
    "carry": {"type": "CARRY8", "parameters": {"CARRY_TYPE": args.carry_type},
              "port_directions": {"CI": "input"}, "connections": {"CI": [10]}},
}}}}
assignments = [
    {"instance": "source", "cell_type": source["type"],
     "bel": "AFF" if args.mode == "fabric" else "CARRY8", "site": args.source_site},
    {"instance": "carry", "cell_type": "CARRY8", "bel": "CARRY8", "site": args.carry_site},
]
# This fixture qualifies the route-input adapter only. It does not claim that
# the deliberately partial primitive graph passed full packing/equivalence.
packed = {"schema": "emuflow.packed-site-netlist/v1", "top": "probe",
          "clusters": [{"assignments": assignments}]}
placement = {"schema": "emuflow.xilinx-placement/v1", "part": args.part,
             "clusters": [{"site": args.carry_site, "assignments": assignments}]}
paths = [args.out / name for name in ("mapped.json", "packed.json", "placement.json")]
for path, value in zip(paths, (mapped, packed, placement)):
    write_json(path, value, compact=True)
input_path, route_path = args.out / "route.tsv", args.out / "route.json"
export_rwroute_input(*paths, input_path)
report = run_rwroute(
    input_path, route_path, rapidwright_jar=args.rapidwright_jar, java=args.java,
    classes_dir=args.out / "classes", java_source=ROOT / "scripts/rapidwright/EmuFlowRWRoute.java",
    device_data_root=args.device_data, timing_data_dir=args.timing_data,
    log_path=args.out / "rwroute.log", mapped_path=paths[0], packed_path=paths[1],
    placement_path=paths[2],
)
header = read_json(route_path)
net = next(n for n in iter_xilinx_route_nets(route_path, header) if n["net"] == "n10")
expected = "AX" if args.mode == "fabric" else "CIN"
if not any(p["site"] == args.carry_site and p["pin"] == expected
           and not p["is_output"] for p in net["pins"]):
    raise RuntimeError("routed carry sink did not retain the expected physical binding")
result = {"status": "pass", "qualification": "carry-input-physical-connectivity-only",
          "mode": args.mode, "carry_type": args.carry_type, "sink_pin": expected,
          "validation": report, "whole_flow": False}
write_json(args.out / "qualification.json", result, compact=True)
print(result, flush=True)
