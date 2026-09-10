"""Open-tool implementation for the fixed ULX3S board-top interface.

Separate from fixed-slot Phase 7 until asynchronous DUT and global timing
binding are implemented. No per-device timing is promoted to global timing.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from .board_ulx3s import ulx3s_endpoint_lpf, ulx3s_pair_profile
from .ecp5_qualification import qualify_ecp5_endpoint_report
from .errors import ValidationError


def run_ulx3s_physical(sources, *, top: str, tools: Path, output_dir: Path,
                      board: str = "board0") -> dict:
    """Map, place/route and pack real RTL with fixed pins and seed 1.

    No unconstrained-pin or timing-failure allowances. Fresh scratch per call;
    compact terminal report without duplicated detailed nextpnr paths.
    """
    if not isinstance(top, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top):
        raise ValidationError("top must be a simple Verilog identifier")
    lpf = ulx3s_endpoint_lpf(board)
    paths = [Path(p).resolve(strict=True) for p in sources]
    if not paths or len(set(paths)) != len(paths) or any(not p.is_file() for p in paths):
        raise ValidationError("RTL sources must be nonempty, unique files")
    if any(c in str(p) for p in paths for c in '\n\r"\\'):
        raise ValidationError("unsupported RTL path characters")
    tools = Path(tools).resolve(strict=True)
    for name in ("yosys", "nextpnr-ecp5", "ecppack"):
        if not (tools / name).is_file() or not os.access(tools / name, os.X_OK):
            raise ValidationError(f"missing executable open tool: {name}")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    for name in ("home", "tmp", "config", "cache", "data"):
        (out / name).mkdir()
    env = dict(os.environ, HOME=str(out / "home"), TMPDIR=str(out / "tmp"),
               XDG_CONFIG_HOME=str(out / "config"), XDG_CACHE_HOME=str(out / "cache"),
               XDG_DATA_HOME=str(out / "data"))
    (out / "endpoint.lpf").write_text(lpf)
    read_sources = " ".join(f'"{p}"' for p in paths)
    stages = [
        ("synthesis", [str(tools / "yosys"), "-p",
                       f'read_verilog -sv {read_sources}; synth_ecp5 -top {top} -json mapped.json']),
        ("place_route", [str(tools / "nextpnr-ecp5"), "--85k", "--package", "CABGA381",
                         "--speed", "6", "--seed", "1", "--json", "mapped.json",
                         "--lpf", "endpoint.lpf", "--textcfg", "routed.config",
                         "--report", "physical.json"]),
        ("bitstream", [str(tools / "ecppack"), "routed.config", "endpoint.bit"]),
    ]
    report = {"schema": "emuflow.open-endpoint-qualification/v1",
              "scope": "offline-physical-endpoint-only", "top": top, "board": board,
              "profile": ulx3s_pair_profile()["id"], "seed": 1,
              "sources": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in paths],
              "constraints_sha256": hashlib.sha256(lpf.encode()).hexdigest(),
              "status": "running", "stages": [], "global_wns_tns": None,
              "hardware_tested": False, "external_timing_qualified": False}
    try:
        for name, command in stages:
            start = time.monotonic()
            with (out / f"{name}.log").open("w") as log:
                result = subprocess.run(command, cwd=out, env=env,
                                        stdout=log, stderr=subprocess.STDOUT)
            report["stages"].append({"name": name, "command": command,
                                     "seconds": time.monotonic()-start,
                                     "exit_code": result.returncode})
            if result.returncode:
                raise RuntimeError(f"{name} failed; inspect {out / (name + '.log')}")
        for artifact in ("mapped.json", "routed.config", "physical.json", "endpoint.bit"):
            if not (out / artifact).is_file() or (out / artifact).stat().st_size == 0:
                raise ValidationError(f"missing or empty tool output: {artifact}")
        physical = json.loads((out / "physical.json").read_text())
        report["local_qualification"] = qualify_ecp5_endpoint_report(physical)
        report["physical"] = {key: physical[key] for key in ("utilization", "fmax")}
        report["status"] = "physical_outputs_generated"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
