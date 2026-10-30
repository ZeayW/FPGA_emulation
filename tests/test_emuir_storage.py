from contextlib import closing
import hashlib
import gzip
import io
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.ir.emuir_storage import is_emuir_sqlite, read_emuir_sqlite, write_emuir_sqlite, _write_emuir_sqlite_v1
from emuflow.ir.ir import EmuIR
from tests.yosys_reference import import_yosys_json, import_yosys_json_indexed


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

    def test_publication_does_not_run_storage_engine_audits(self) -> None:
        connect = sqlite3.connect

        def without_audits(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_authorizer(
                lambda action, name, *unused: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA
                and name.lower() in {"integrity_check", "quick_check"}
                else sqlite3.SQLITE_OK
            )
            return connection

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "design.emuir.sqlite"
            with patch("emuflow.ir.emuir_storage.sqlite3.connect", without_audits):
                self.ir.save(artifact)
                self.assertEqual(EmuIR.load(artifact).to_dict(), self.ir.to_dict())
            # An explicit offline engine audit remains possible, but is not
            # invoked by a producer/consumer or needed for logical round-trip.
            with closing(connect(artifact)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_duplicate_keys_reject_publication_and_preserve_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "design.emuir.sqlite"
            self.ir.save(artifact)
            original = artifact.read_bytes()
            invalid = self.ir.to_dict()
            invalid["instances"] = [*invalid["instances"], invalid["instances"][0]]
            with self.assertRaises(sqlite3.IntegrityError):
                write_emuir_sqlite(artifact, invalid)
            self.assertEqual(artifact.read_bytes(), original)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_child_rows_merge_without_whole_graph_side_indexes(self) -> None:
        value = {
            "schema": "emuflow.emuir/v1",
            "design": {"name": "many", "top": "many", "source_format": "test"},
            "ports": [], "instances": [], "nets": [], "clocks": [], "warnings": [],
        }
        for index in range(4000):
            instance = f"leaf_{index}"
            value["instances"].append({
                "id": instance, "name": instance, "type": "LUT1",
                "resources": {"lut": 1},
                "constant_connections": [{"port": "I0", "bit": 0, "value": "1", "tag": "constant"}],
                "parameters": {"INIT": "10"}, "attributes": {"src": "test.v"},
                "custom": {"index": index},
            })
            value["nets"].append({
                "id": f"n_{index}", "name": f"输出_{index}",
                "cut_class": "combinational", "fanout": 0,
                "aliases": [f"alias_{index}"],
                "drivers": [{"instance": instance, "port": "O", "bit": 0, "tag": "driver"}],
                "sinks": [], "custom": {"index": index},
            })
        ir = EmuIR(value)
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "many.emuir.sqlite"
            ir.save(artifact)
            tracemalloc.start()
            try:
                loaded = read_emuir_sqlite(artifact)
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(loaded, ir.value)
            # Measure only the loader, not the separate logical validator.
            # Old full-population child maps coexist with their output graph
            # and violate this bound. A cursor merge holds one lookahead row.
            self.assertLess(peak, retained * 1.05)
            EmuIR(loaded)

    def test_orphaned_child_rows_fail_closed(self) -> None:
        statements = (
            "INSERT INTO port_constants VALUES (999999, 0, 0, '0', '{}')",
            "INSERT INTO instance_resources VALUES (999999, 'lut', 1)",
            "INSERT INTO instance_constants VALUES (999999, 0, 'I0', 0, '0', '{}')",
            "INSERT INTO net_aliases VALUES (999999, 0, 'orphan')",
            "INSERT INTO endpoints VALUES (999999, 0, 0, NULL, 'clk', 0, '{}')",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            for index, statement in enumerate(statements):
                with self.subTest(statement=statement):
                    artifact = Path(temporary_directory) / f"orphan{index}.emuir.sqlite"
                    _write_emuir_sqlite_v1(artifact, self.ir.value)
                    with closing(sqlite3.connect(artifact)) as connection, connection:
                        connection.execute(statement)
                    with self.assertRaisesRegex(ValueError, "orphaned"):
                        EmuIR.load(artifact)

    def test_invalid_endpoint_role_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "role.emuir.sqlite"
            _write_emuir_sqlite_v1(artifact, self.ir.value)
            with closing(sqlite3.connect(artifact)) as connection, connection:
                connection.execute("UPDATE endpoints SET role = 2 WHERE net_seq = 0 AND role = 0")
            with self.assertRaisesRegex(ValueError, "endpoint role"):
                EmuIR.load(artifact)

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

    def test_hierarchical_stream_collapses_aliased_child_outputs(self) -> None:
        primitive = {
            "type": "LUT1",
            "parameters": {"INIT": "10"},
            "port_directions": {"I0": "input", "O": "output"},
            "connections": {"I0": [2], "O": [3]},
        }
        hierarchical = {
            "creator": "hierarchical-alias-test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "a": {"direction": "input", "bits": [2]},
                        "y0": {"direction": "output", "bits": [3]},
                        "y1": {"direction": "output", "bits": [4]},
                    },
                    "cells": {
                        "producer": {
                            "type": "aliased_leaf",
                            "port_directions": {
                                "a": "input",
                                "y0": "output",
                                "y1": "output",
                            },
                            "connections": {"a": [2], "y0": [3], "y1": [4]},
                        },
                        "consumer": {
                            **primitive,
                            "connections": {"I0": [4], "O": [5]},
                        },
                    },
                    "netnames": {
                        "a": {"bits": [2]},
                        "y0": {"bits": [3]},
                        "y1": {"bits": [4]},
                        "z": {"bits": [5]},
                    },
                },
                "aliased_leaf": {
                    "ports": {
                        "a": {"direction": "input", "bits": [2]},
                        "y0": {"direction": "output", "bits": [3]},
                        "y1": {"direction": "output", "bits": [3]},
                    },
                    "cells": {"driver": primitive},
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
            "creator": "hierarchical-alias-test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "a": {"direction": "input", "bits": [2]},
                        "y0": {"direction": "output", "bits": [3]},
                        "y1": {"direction": "output", "bits": [3]},
                    },
                    "cells": {
                        "consumer": {
                            **primitive,
                            "connections": {"I0": [3], "O": [5]},
                        },
                        "producer.driver": {
                            **primitive,
                            "connections": {"I0": [2], "O": [3]},
                        },
                    },
                    "netnames": {
                        "a": {"bits": [2]},
                        "y0": {"bits": [3]},
                        "y1": {"bits": [3]},
                        "z": {"bits": [5]},
                        "producer.a": {"bits": [2]},
                        "producer.y": {"bits": [3]},
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hierarchical_artifact = root / "hierarchical.emuir.sqlite"
            flattened_artifact = root / "flattened.emuir.sqlite"
            report = import_yosys_json_indexed(
                Path("<hierarchical-alias-stream>"),
                hierarchical_artifact,
                top="top",
                _source_stream=io.StringIO(json.dumps(hierarchical)),
                _expand_hierarchy=True,
            )
            import_yosys_json_indexed(
                Path("<flat-alias-stream>"),
                flattened_artifact,
                top="top",
                _source_stream=io.StringIO(json.dumps(flattened)),
            )
            actual = EmuIR.load(hierarchical_artifact).to_dict()
            expected = EmuIR.load(flattened_artifact).to_dict()
        actual["design"]["source"] = expected["design"]["source"]
        self.assertEqual(actual, expected)
        hierarchy = report["hierarchical_interchange"]
        self.assertEqual(hierarchy["collapsed_signal_bits"], 1)


if __name__ == "__main__":
    unittest.main()
