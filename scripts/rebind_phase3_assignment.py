#!/usr/bin/env python3
"""Explicit debugging continuation: preserve owners and rebuild current cuts."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from emuflow.phase3_rebind import rebind_phase3_assignment

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--ir", type=Path, required=True)
parser.add_argument("--platform", type=Path, required=True)
parser.add_argument("--frozen-phase3", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--route-constraints", type=Path)
parser.add_argument("--max-cross-fpga-dependency-depth", type=int, default=8)
args = parser.parse_args()
report = rebind_phase3_assignment(
    args.ir, args.platform, args.frozen_phase3, args.out,
    route_constraints_path=args.route_constraints,
    max_cross_fpga_dependency_depth=args.max_cross_fpga_dependency_depth,
)
print({"status": report["status"], "partition_decision": report["partition_decision"],
       "validation": report["validation"]}, flush=True)
