#!/usr/bin/env python3
"""Run real open ECP5 tools for the fixed reference endpoint interface.

This is a physical endpoint gate, NOT a complete EmuFlow/board timing result.
No programming or hardware access is performed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emuflow.board_ulx3s import ulx3s_endpoint_lpf, ulx3s_pair_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rtl", type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.top):
        parser.error("top must be a simple Verilog identifier")
    rtl = args.rtl.resolve(strict=True)
    # Yosys command strings need stricter quoting than subprocess argv.
    if any(c in str(rtl) for c in '\n\r"\\'):
        parser.error("unsupported RTL path characters")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    tools = args.tools.resolve(strict=True)
    for name in ("yosys", "nextpnr-ecp5", "ecppack"):
        if not (tools / name).is_file():
            parser.error(f"missing open tool: {name}")
    for name in ("home", "tmp", "config", "cache", "data"):
        (out / name).mkdir()
    env = dict(os.environ, HOME=str(out / "home"), TMPDIR=str(out / "tmp"),
               XDG_CONFIG_HOME=str(out / "config"),
               XDG_CACHE_HOME=str(out / "cache"), XDG_DATA_HOME=str(out / "data"))
    (out / "endpoint.lpf").write_text(ulx3s_endpoint_lpf("board0"))
    stages = [
        ("synthesis", [str(tools / "yosys"), "-p",
                       f'read_verilog -sv "{rtl}"; synth_ecp5 -top {args.top} -json mapped.json']),
        ("place_route", [str(tools / "nextpnr-ecp5"), "--85k", "--package", "CABGA381",
                         "--speed", "6", "--seed", "1", "--json", "mapped.json",
                         "--lpf", "endpoint.lpf", "--textcfg", "routed.config",
                         "--report", "physical.json"]),
        ("bitstream", [str(tools / "ecppack"), "routed.config", "endpoint.bit"]),
    ]
    report = {"schema": "emuflow.open-endpoint-qualification/v1",
              "scope": "offline-physical-endpoint-only", "top": args.top,
              "profile": ulx3s_pair_profile()["id"], "seed": 1,
              "status": "running", "stages": [],
              "global_wns_tns": None, "hardware_tested": False,
              "external_timing_qualified": False}
    try:
        for name, command in stages:
            start = time.monotonic()
            with (out / f"{name}.log").open("w") as log:
                result = subprocess.run(command, cwd=out, env=env,
                                        stdout=log, stderr=subprocess.STDOUT)
            report["stages"].append({"name": name, "command": command,
                                     "seconds": time.monotonic() - start,
                                     "exit_code": result.returncode})
            if result.returncode:
                raise RuntimeError(f"{name} failed; inspect {out / (name + '.log')}")
        for artifact in ("mapped.json", "routed.config", "physical.json", "endpoint.bit"):
            if not (out / artifact).is_file() or (out / artifact).stat().st_size == 0:
                raise RuntimeError(f"missing or empty tool output: {artifact}")
        physical = json.loads((out / "physical.json").read_text())
        report["physical"] = physical
        report["status"] = "physical_outputs_generated"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "summary": str(out / "summary.json")}))


if __name__ == "__main__":
    main()
