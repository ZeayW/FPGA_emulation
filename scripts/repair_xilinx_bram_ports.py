#!/usr/bin/env python3
"""Explicit recovery from a completed compact EmuIR, without resynthesis."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from emuflow.io import write_json
from emuflow.xilinx_bram_ports import repair_compact_bram_ports

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
report = repair_compact_bram_ports(
    args.source, args.out, ROOT / "engines/yosys/techlibs/xilinx/cells_xtra.v",
)
write_json(args.report, report, compact=True)
print(report, flush=True)
