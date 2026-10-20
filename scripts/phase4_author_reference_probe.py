#!/usr/bin/env python3
"""Bounded Linux diagnostics of the unchanged author-supplied reference."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    source = root / "engines/synergistic_author_reference"
    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        raise RuntimeError("C++ compiler missing")
    report = {"schema": "emuflow.author-reference-probe/v1", "host": os.uname().nodename,
              "source_commit": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
              "compiler": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0],
              "scope": "unmodified-reference-diagnostic", "trials": []}
    executable = out / "author-reference"
    command = [compiler, "-std=c++11", "-O1", "-g", "-fsanitize=address,undefined",
               "-fno-omit-frame-pointer", "-I" + str(source / "src"),
               *[str(p) for p in sorted((source / "src").rglob("*.cpp"))], "-o", str(executable)]
    started = time.monotonic()
    with (out / "build.log").open("w") as log:
        build = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=180)
    report["build"] = {"argv": command, "returncode": build.returncode, "seconds": time.monotonic()-started}
    if build.returncode == 0:
        case = out / "case"
        case.mkdir()
        for name, data in {
            "design.fpga.out": "FPGA0: g0\nFPGA1: g1 g2\n",
            "design.info": "FPGA0 100\nFPGA1 100\n",
            "design.topo": "FPGA0: 0,4\nFPGA1: 4,0\n",
            "design.net": "g0 s g1 g2\n",
        }.items():
            (case / name).write_text(data)
        for name, argv in [("tiny-multicast", [str(case)]), ("missing-directory", [str(out / "absent")]), ("no-arguments", [])]:
            work = out / name
            work.mkdir()
            started = time.monotonic()
            try:
                with (work / "run.log").open("w") as log:
                    proc = subprocess.run([str(executable), *argv], cwd=work, stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=subprocess.STDOUT, timeout=10)
                code, status = proc.returncode, "completed"
            except subprocess.TimeoutExpired:
                code, status = None, "timeout"
            body = (work / "run.log").read_text(errors="replace")
            report["trials"].append({"name": name, "returncode": code, "state": status,
                                      "seconds": time.monotonic()-started,
                                      "sanitizer_error": "ERROR: AddressSanitizer" in body or "runtime error:" in body,
                                      "log_sha256": hashlib.sha256(body.encode()).hexdigest()})
    (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(out), "build": report["build"]["returncode"], "trials": report["trials"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
