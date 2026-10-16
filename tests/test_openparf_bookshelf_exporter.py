import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.xilinx_netlist import (
    emit_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from emuflow.xilinx_openparf_atomic import _render_library_and_nets
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


class NativeOpenparfBookshelfExporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = (
            shutil.which("c++") or shutil.which("g++")
            or shutil.which("clang++")
        )
        if compiler is None:
            raise unittest.SkipTest("a C++17 compiler is required")
        cls._build = tempfile.TemporaryDirectory()
        cls.executable = Path(cls._build.name) / "exporter"
        completed = subprocess.run(
            [
                compiler, "-std=c++17", "-O2",
                str(ROOT / "src/native/openparf_bookshelf_exporter.cpp"),
                "-lsqlite3", "-o", str(cls.executable),
            ],
            text=True, capture_output=True, check=False,
        )
        if completed.returncode != 0:
            cls._build.cleanup()
            raise unittest.SkipTest(
                "SQLite C++ development files are unavailable: "
                + (completed.stderr or completed.stdout)
            )

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "_build"):
            cls._build.cleanup()

    def test_native_sqlite_export_matches_reference_renderer(self) -> None:
        mapped = {
            "creator": "fixture",
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "clk": {"direction": "input", "bits": [5]},
                    "y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "ff": {
                        "type": "FDRE", "parameters": {}, "attributes": {},
                        "port_directions": {
                            "C": "input", "CE": "input", "D": "input",
                            "Q": "output", "R": "input",
                        },
                        "connections": {
                            "C": [5], "CE": ["1"], "D": [3],
                            "Q": [4], "R": ["0"],
                        },
                    },
                    "lut": {
                        "type": "LUT1", "parameters": {"INIT": "10"},
                        "attributes": {},
                        "port_directions": {"I0": "input", "O": "output"},
                        "connections": {"I0": [2], "O": [3]},
                    },
                },
                "netnames": {
                    "a": {"bits": [2]}, "mid": {"bits": [3]},
                    "y": {"bits": [4]}, "clk": {"bits": [5]},
                },
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            output = root / "bookshelf"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            emit_xilinx_mapped_index(ir_path, index)

            completed = subprocess.run(
                [str(self.executable), str(index), str(output)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr or completed.stdout
            )
            report = json.loads(
                (output / "bookshelf-export-report.json").read_text()
            )
            self.assertEqual(report["provider"], "native-sqlite-stream-v1")
            self.assertEqual(report["atoms"], 2)

            atoms = [
                {"instance": "ff", "cell_type": "FDRE"},
                {"instance": "lut", "cell_type": "LUT1"},
            ]
            names = {"ff": "a0", "lut": "a1"}
            with open_xilinx_mapped_index(index) as indexed:
                cells = indexed["modules"]["top"]["cells"]
                library, nets, net_count, dropped = _render_library_and_nets(
                    cells, atoms, names
                )
            self.assertEqual((output / "design.lib").read_text(), library)
            self.assertEqual((output / "design.nets").read_text(), nets)
            self.assertEqual(report["nets"], net_count)
            self.assertEqual(report["dropped_single_endpoint_nets"], dropped)
            self.assertEqual(
                (output / "design.nodes").read_text(),
                "a0 FDRE\na1 LUT1\n",
            )


if __name__ == "__main__":
    unittest.main()
