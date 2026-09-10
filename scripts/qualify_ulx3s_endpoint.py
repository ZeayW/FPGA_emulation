#!/usr/bin/env python3
"""Run the open ECP5 physical endpoint gate, not full-flow qualification."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emuflow.ecp5_backend import run_ulx3s_physical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rtl", type=Path)
    parser.add_argument("--extra-rtl", type=Path, action="append", default=[])
    parser.add_argument("--top", required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--board", choices=("board0", "board1"), default="board0")
    parser.add_argument("--host-uart", action="store_true", help="bind board0 onboard FT231X UART pins")
    parser.add_argument("--export-timing", action="store_true", help="emit scratch routed netlist/SDF for timing binding")
    args = parser.parse_args()
    report = run_ulx3s_physical([args.rtl, *args.extra_rtl], top=args.top,
                               tools=args.tools, output_dir=args.out, board=args.board,
                               host_uart=args.host_uart, export_timing=args.export_timing)
    print(json.dumps({"status": report["status"],
                      "summary": str(args.out.resolve() / "summary.json")}))


if __name__ == "__main__":
    main()
