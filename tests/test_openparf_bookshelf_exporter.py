import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.xilinx_netlist import (
    emit_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from emuflow.xilinx_openparf_atomic import (
    _render_library_and_nets,
    build_xilinx_openparf_atomic_source,
    export_xilinx_openparf_atomic,
)
from emuflow.yosys import import_yosys_json
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


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
            with sqlite3.connect(output / "atom-map.sqlite3") as database:
                metadata = dict(database.execute(
                    "SELECT key,value FROM metadata"
                ))
                atom_rows = list(database.execute(
                    "SELECT openparf,instance,cell_type,source_cluster,resource "
                    "FROM atoms ORDER BY atom_seq"
                ))
            self.assertEqual(metadata, {
                "schema": "emuflow.openparf-atom-map/v1", "atoms": "2",
            })
            self.assertEqual(atom_rows, [
                ("a0", "ff", "FDRE", "atomic-source-000000", "FF"),
                ("a1", "lut", "LUT1", "atomic-source-000001", "LUT"),
            ])

            overrides = root / "overrides.tsv"
            overrides.write_text("lut\tmacro:lut\n", encoding="utf-8")
            macro_output = root / "bookshelf-macro"
            completed = subprocess.run(
                [
                    str(self.executable), str(index), str(macro_output),
                    str(overrides),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr or completed.stdout
            )
            with sqlite3.connect(
                macro_output / "atom-map.sqlite3"
            ) as database:
                source_cluster = database.execute(
                    "SELECT source_cluster FROM atoms WHERE instance='lut'"
                ).fetchone()[0]
            self.assertEqual(source_cluster, "macro:lut")

    def test_production_export_uses_native_atom_summary_without_atom_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped_json, _packed, architecture = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            ir_path = root / "mapped.emuir.sqlite"
            index = root / "mapped.sqlite"
            source_path = root / "atomic-source.json"
            output = root / "openparf"
            import_yosys_json(mapped_json).save(ir_path)
            emit_xilinx_mapped_index(ir_path, index)
            with open_xilinx_mapped_index(index) as indexed:
                source = build_xilinx_openparf_atomic_source(
                    index, source_path, top="top", mapped_value=indexed
                )
                with mock.patch(
                    "emuflow.xilinx_openparf_atomic.resolve_native_executable",
                    return_value=str(self.executable),
                ), mock.patch(
                    "emuflow.xilinx_openparf_atomic._collect_atoms",
                    side_effect=AssertionError(
                        "native production export must not materialize atoms"
                    ),
                ):
                    manifest = export_xilinx_openparf_atomic(
                        index,
                        source_path,
                        architecture,
                        output,
                        top="top",
                        mapped_value=indexed,
                        packed_value=source,
                    )
            self.assertEqual(manifest["atoms"], 131)
            self.assertEqual(manifest["resources"], {
                "DSP48E2": 1,
                "FF": 64,
                "LUT": 64,
                "RAMB36E2": 1,
                "URAM288": 1,
            })
            name_map = json.loads((output / "name_map.json").read_text())
            self.assertNotIn("atoms", name_map)
            self.assertEqual(name_map["atom_database"]["atoms"], 131)


if __name__ == "__main__":
    unittest.main()
