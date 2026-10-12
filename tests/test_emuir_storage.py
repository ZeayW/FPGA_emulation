import hashlib
import gzip
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.emuir_storage import is_emuir_sqlite
from emuflow.ir import EmuIR
from emuflow.yosys import import_yosys_json, import_yosys_json_indexed


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EmuIRStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json",
            top="counter",
            clocks=["clk"],
        )

    def test_round_trip_is_exact_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.emuir.sqlite"
            second = root / "second.emuir.sqlite"
            self.ir.save(first)
            self.ir.save(second)
            self.assertTrue(is_emuir_sqlite(first))
            self.assertEqual(EmuIR.load(first).to_dict(), self.ir.to_dict())
            self.assertEqual(_sha256(first), _sha256(second))

    def test_truncation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "design.emuir.sqlite"
            self.ir.save(artifact)
            raw = artifact.read_bytes()
            artifact.write_bytes(raw[: len(raw) // 2])
            with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                EmuIR.load(artifact)

    def test_new_writer_rejects_monolithic_json_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "design.emuir.json"
            with self.assertRaisesRegex(ValueError, "indexed"):
                self.ir.save(artifact)

    def test_indexed_yosys_import_matches_legacy_logical_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "design.emuir.sqlite"
            import_yosys_json_indexed(
                ROOT / "examples/yosys/counter.json",
                artifact,
                top="counter",
                clocks=["clk"],
            )
            self.assertEqual(EmuIR.load(artifact).to_dict(), self.ir.to_dict())

    def test_indexed_yosys_import_streams_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "counter.json.gz"
            with gzip.open(source, "wb", compresslevel=1) as stream:
                stream.write((ROOT / "examples/yosys/counter.json").read_bytes())
            artifact = root / "design.emuir.sqlite"
            import_yosys_json_indexed(
                source,
                artifact,
                top="counter",
                clocks=["clk"],
            )
            expected = import_yosys_json(
                source, top="counter", clocks=["clk"]
            )
            self.assertEqual(EmuIR.load(artifact).to_dict(), expected.to_dict())

    def test_hierarchical_stream_expands_to_flat_indexed_graph(self) -> None:
        primitive = {
            "type": "LUT1",
            "parameters": {"INIT": "10"},
            "port_directions": {"I0": "input", "O": "output"},
            "connections": {"I0": [2], "O": [3]},
        }
        hierarchical = {
            "creator": "hierarchical-test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "a": {"direction": "input", "bits": [2]},
                        "y": {"direction": "output", "bits": [3]},
                    },
                    "cells": {
                        "u0": {
                            "type": "leaf",
                            "port_directions": {
                                "a": "input", "y": "output",
                            },
                            "connections": {"a": [2], "y": [4]},
                        },
                        "u1": {
                            "type": "leaf",
                            "port_directions": {
                                "a": "input", "y": "output",
                            },
                            "connections": {"a": [4], "y": [3]},
                        },
                    },
                    "netnames": {
                        "a": {"bits": [2]},
                        "mid": {"bits": [4]},
                        "y": {"bits": [3]},
                    },
                },
                "leaf": {
                    "ports": {
                        "a": {"direction": "input", "bits": [2]},
                        "y": {"direction": "output", "bits": [3]},
                    },
                    "cells": {"p": primitive},
                    "netnames": {
                        "a": {"bits": [2]},
                        "y": {"bits": [3]},
                    },
                },
                "LUT1": {
                    "attributes": {"blackbox": "1"},
                    "ports": {
                        "I0": {"direction": "input", "bits": [2]},
                        "O": {"direction": "output", "bits": [3]},
                    },
                },
            },
        }
        flattened = {
            "creator": "hierarchical-test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": hierarchical["modules"]["top"]["ports"],
                    "cells": {
                        "u0.p": {
                            **primitive,
                            "connections": {"I0": [2], "O": [4]},
                        },
                        "u1.p": {
                            **primitive,
                            "connections": {"I0": [4], "O": [3]},
                        },
                    },
                    "netnames": {
                        **hierarchical["modules"]["top"]["netnames"],
                        "u0.a": {"bits": [2]},
                        "u0.y": {"bits": [4]},
                        "u1.a": {"bits": [4]},
                        "u1.y": {"bits": [3]},
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hierarchical_artifact = root / "hierarchical.emuir.sqlite"
            flattened_artifact = root / "flattened.emuir.sqlite"
            report = import_yosys_json_indexed(
                Path("<hierarchical-stream>"),
                hierarchical_artifact,
                top="top",
                _source_stream=io.StringIO(json.dumps(hierarchical)),
                _expand_hierarchy=True,
            )
            import_yosys_json_indexed(
                Path("<flat-stream>"),
                flattened_artifact,
                top="top",
                _source_stream=io.StringIO(json.dumps(flattened)),
            )
            actual = EmuIR.load(hierarchical_artifact).to_dict()
            expected = EmuIR.load(flattened_artifact).to_dict()
        actual["design"]["source"] = expected["design"]["source"]
        self.assertEqual(actual, expected)
        hierarchy = report["hierarchical_interchange"]
        self.assertEqual(hierarchy["module_definitions"], 3)
        self.assertEqual(hierarchy["expanded_module_instances"], 3)
        self.assertEqual(hierarchy["maximum_hierarchy_depth"], 1)


if __name__ == "__main__":
    unittest.main()
