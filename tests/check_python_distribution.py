"""Verify wheel contents and the installed entry without source-tree imports."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main(source: Path, wheel: Path) -> None:
    source = source.resolve(strict=True)
    wheel = wheel.resolve(strict=True)
    package = source / "src/python/emuflow"
    expected = {
        "emuflow/" + path.relative_to(package).as_posix(): path
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix in {".py", ".json", ".sql", ".tcl", ".v"}
    }
    with zipfile.ZipFile(wheel) as archive:
        actual = {name for name in archive.namelist()
                  if name.startswith("emuflow/") and not name.endswith("/")}
        assert actual == set(expected), (
            f"wheel inventory differs: missing={sorted(set(expected)-actual)}, "
            f"unexpected={sorted(actual-set(expected))}"
        )
        for name, path in expected.items():
            assert archive.read(name) == path.read_bytes(), f"wheel member changed: {name}"

    with tempfile.TemporaryDirectory(prefix="emuflow-wheel-check-") as temporary:
        root = Path(temporary)
        environment = root / "environment"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                        str(wheel)], cwd=root, check=True)
        code = """import importlib.util, pathlib, sys
import emuflow
from emuflow.flow.cli import _build_parser
from emuflow.flow.compile_options import CONTRACT_PATH, COMPILE_DEFAULTS
assert pathlib.Path(emuflow.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
assert CONTRACT_PATH.is_file()
assert COMPILE_DEFAULTS['partition_provider'] == 'patron'
assert importlib.util.find_spec('emuflow_tools') is None
assert importlib.util.find_spec('emuflow.common.io') is None
assert importlib.util.find_spec('emuflow.flow.multi_fpga_flow') is None
for backend in ('rapidwright', 'vivado'):
    _build_parser().parse_args(['run', 'top.sv', '--top', 'top', '--clock', 'clk=10',
                               '--backend', backend])
print('Installed wheel entry and resources passed')
"""
        subprocess.run([str(python), "-I", "-c", code], cwd=root, check=True)
    print(json.dumps({"status": "pass", "scope": "isolated wheel entry and package resources",
                      "package_files": len(expected), "wheel": wheel.name}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    main(args.source, args.wheel)
