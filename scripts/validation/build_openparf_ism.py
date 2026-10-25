#!/usr/bin/env python3
"""Build the versioned ISM operator and its native dependencies with CMake."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "commit", "python"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--cmake-arg", action="append", default=[])
    args = parser.parse_args()
    source, output = Path(args.source).resolve(), Path(args.output).resolve()
    def git(*values):
        return subprocess.check_output(["git", *values], cwd=source).decode().strip()
    if git("rev-parse", "HEAD") != args.commit or git("status", "--porcelain"):
        raise RuntimeError("ISM build requires the exact clean source commit")
    if args.jobs <= 0:
        raise ValueError("jobs must be positive")
    output.mkdir(parents=True, exist_ok=False)
    start = time.time()
    build = output / "build"
    commands = [
        [args.cmake, "-S", str(source / "engines/openparf"), "-B", str(build),
         "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
         "-DBUILD_TESTING=OFF", "-DENABLE_ROUTER=OFF",
         "-DPython3_EXECUTABLE=" + str(Path(args.python).resolve()),
         "-DPYTHON_EXECUTABLE=" + str(Path(args.python).resolve()),
         "-DPython_EXECUTABLE=" + str(Path(args.python).resolve()),
         "-DCMAKE_INSTALL_PREFIX=" + str(output / "install"), *args.cmake_arg],
        [args.cmake, "--build", str(build), "--target", "ism_dp_cpp", "--parallel", str(args.jobs)],
    ]
    for command in commands:
        subprocess.run(command, cwd=output, check=True)
        if command is commands[0]:
            cache = (build / "CMakeCache.txt").read_text().splitlines()
            interpreters = [line.split("=", 1)[1] for line in cache
                            if line.startswith(("Python3_EXECUTABLE:", "PYTHON_EXECUTABLE:", "Python_EXECUTABLE:"))]
            if len(interpreters) != 3 or any(
                Path(value).resolve() != Path(args.python).resolve()
                for value in interpreters
            ):
                raise RuntimeError("CMake selected a different Python ABI for ISM")
    binaries = list((build / "openparf/ops/ism_dp").glob("ism_dp_cpp.*.so"))
    if len(binaries) != 1:
        raise RuntimeError("ISM build did not produce exactly one native operator")
    binary = binaries[0].resolve()
    receipt = dict(
        status="built", provider="ism-utilization-and-fixed-site-reservations-v2",
        source_commit=args.commit, source_root=str(source),
        python=str(Path(args.python).resolve()), commands=commands,
        seconds=time.time() - start,
        binary=dict(path=str(binary), bytes=binary.stat().st_size,
                    sha256=hashlib.sha256(binary.read_bytes()).hexdigest()),
    )
    (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
