import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.verilog import mapped_verilog
from emuflow.xilinx_netlist import (
    emit_xilinx_mapped_index,
    emit_xilinx_mapped_json,
    is_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from emuflow.xilinx_physical_handoff import write_xilinx_physical_handoff
from emuflow.yosys import import_yosys_json


class XilinxMappedNetlistTests(unittest.TestCase):
    @staticmethod
    def _fixture() -> dict:
        return {
            "creator": "fixture",
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "a": {"direction": "input", "bits": [3]},
                    "y": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "lut": {
                        "type": "LUT1", "parameters": {"INIT": "10"},
                        "attributes": {"keep": "1"},
                        "port_directions": {"I0": "input", "O": "output"},
                        "connections": {"I0": [3], "O": [4]},
                    },
                    "ff": {
                        "type": "FDRE", "parameters": {"INIT": "0"},
                        "attributes": {},
                        "port_directions": {
                            "C": "input", "CE": "input", "D": "input",
                            "R": "input", "Q": "output",
                        },
                        "connections": {
                            "C": [2], "CE": ["1"], "D": [4],
                            "R": ["0"], "Q": [5],
                        },
                    },
                },
                "netnames": {
                    "clk": {"bits": [2]}, "a": {"bits": [3]},
                    "comb": {"bits": [4]}, "y": {"bits": [5]},
                },
            }},
        }

    @staticmethod
    def _table_rows(path: Path, table: str) -> list[tuple]:
        connection = sqlite3.connect(path)
        try:
            columns = [
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            order = ",".join(columns)
            return list(connection.execute(f"SELECT * FROM {table} ORDER BY {order}"))
        finally:
            connection.close()

    def test_native_production_index_matches_python_reference_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            native_index = root / "native.sqlite"
            reference_index = root / "reference.sqlite"
            reference_json = root / "reference.json"
            source.write_text(json.dumps(self._fixture()), encoding="utf-8")
            import_yosys_json(source, clocks=("clk",)).save(ir_path)
            source_hash = hashlib.sha256(ir_path.read_bytes()).hexdigest()
            source_mtime = ir_path.stat().st_mtime_ns

            native_report = emit_xilinx_mapped_index(ir_path, native_index)
            reference_report = emit_xilinx_mapped_json(
                ir_path, reference_json, index_path=reference_index
            )

            self.assertEqual(
                native_report["index_builder"], "sqlite-native-primitive-v1"
            )
            self.assertEqual(
                reference_report["index_builder"], "python-lowering-v2"
            )
            for table in ("metadata", "cells", "cell_pins", "netnames"):
                self.assertEqual(
                    self._table_rows(native_index, table),
                    self._table_rows(reference_index, table),
                )
            self.assertEqual(
                hashlib.sha256(ir_path.read_bytes()).hexdigest(), source_hash
            )
            self.assertEqual(ir_path.stat().st_mtime_ns, source_mtime)

    def test_generic_lut_keeps_python_lowering_fallback(self) -> None:
        mapped = self._fixture()
        lut = mapped["modules"]["top"]["cells"]["lut"]
        lut["type"] = "$lut"
        lut["parameters"] = {"WIDTH": "1", "LUT": "10"}
        lut["port_directions"] = {"A": "input", "Y": "output"}
        lut["connections"] = {"A": [3], "Y": [4]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source, clocks=("clk",)).save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, index)
            with open_xilinx_mapped_index(index) as indexed:
                self.assertEqual(
                    indexed.cell_type_and_parameters("lut"),
                    ("LUT1", {"INIT": "10"}),
                )
        self.assertEqual(report["index_builder"], "python-lowering-v2")

    def test_native_projection_rejects_one_pin_on_two_nets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(self._fixture()), encoding="utf-8")
            import_yosys_json(source, clocks=("clk",)).save(ir_path)
            connection = sqlite3.connect(ir_path)
            try:
                cell_seq = connection.execute(
                    "SELECT seq FROM cell_records WHERE id='lut'"
                ).fetchone()[0]
                port_seq = connection.execute(
                    "SELECT seq FROM pin_names WHERE name='I0'"
                ).fetchone()[0]
                extras_seq = connection.execute(
                    "SELECT min(seq) FROM payloads"
                ).fetchone()[0]
                ordinal = connection.execute(
                    "SELECT coalesce(max(ordinal),-1)+1 FROM endpoint_records "
                    "WHERE net_seq=0 AND role=1"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)",
                    (0, 1, ordinal, cell_seq, port_seq, 0, extras_seq),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                ValidationError, "multiple nets"
            ):
                emit_xilinx_mapped_index(ir_path, index)
            self.assertFalse(index.exists())

    def test_native_projection_applies_direction_to_the_complete_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(self._fixture()), encoding="utf-8")
            import_yosys_json(source, clocks=("clk",)).save(ir_path)
            connection = sqlite3.connect(ir_path)
            try:
                cell_seq = connection.execute(
                    "SELECT seq FROM cell_records WHERE id='lut'"
                ).fetchone()[0]
                input_port = connection.execute(
                    "SELECT seq FROM pin_names WHERE name='I0'"
                ).fetchone()[0]
                output_port = connection.execute(
                    "SELECT seq FROM pin_names WHERE name='O'"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE endpoint_records SET port_seq=?,bit=1 "
                    "WHERE instance_seq=? AND port_seq=?",
                    (input_port, cell_seq, output_port),
                )
                connection.commit()
            finally:
                connection.close()
            emit_xilinx_mapped_index(ir_path, index)
            with open_xilinx_mapped_index(index) as indexed:
                self.assertEqual(
                    list(indexed.iter_pins_by_net()),
                    [
                        (2, "ff", "C", 0, 1, "input"),
                        (3, "lut", "I0", 0, 2, "inout"),
                        (4, "ff", "D", 0, 1, "input"),
                        (4, "lut", "I0", 1, 2, "inout"),
                        (5, "ff", "Q", 0, 1, "output"),
                    ],
                )

    def test_physical_route_pin_ordinals_stream_without_name_index(self) -> None:
        mapped = {
            "creator": "fixture",
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {},
                "cells": {
                    "a_lut": {
                        "type": "LUT6_2",
                        "parameters": {"INIT": "0" * 64},
                        "attributes": {},
                        "port_directions": {
                            **{f"I{index}": "input" for index in range(6)},
                            "O5": "output", "O6": "output",
                        },
                        "connections": {
                            "I0": [2], "I1": [3],
                            **{f"I{index}": ["0"] for index in range(2, 6)},
                            "O5": [4], "O6": [5],
                        },
                    },
                    "z_ff": {
                        "type": "FDRE", "parameters": {}, "attributes": {},
                        "port_directions": {"D": "input", "Q": "output"},
                        "connections": {"D": [4], "Q": [6]},
                    },
                },
                "netnames": {
                    f"n{bit}": {"bits": [bit]} for bit in range(2, 7)
                },
            }},
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "mapping_profile": "test", "top": "top", "source": {},
            "policy": {}, "cascade_chains": [], "unplaced_constants": [],
            "clusters": [{
                "id": "cluster0", "kind": "slice",
                "site_templates": ["SLICEL"], "control_set": None,
                "assignments": [
                    {"instance": "a_lut", "cell_type": "LUT6_2", "bel": "A6LUT"},
                    {"instance": "z_ff", "cell_type": "FDRE", "bel": "AFF"},
                ],
            }],
            "summary": {"clusters": 1},
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "status": "pass",
            "part": "xcvu19p-test", "provider": "test", "policy": {},
            "source": {},
            "clusters": [{
                "cluster": "cluster0", "site": "SLICE_X0Y0",
                "site_type": "SLICEL", "x": 0, "y": 0,
                "fixed": False, "physical_region": None,
                "assignments": [
                    {
                        "instance": "a_lut", "cell_type": "LUT6_2",
                        "bel": "A6LUT", "placement_mode": "SLICEL",
                        "site": "SLICE_X0Y0",
                    },
                    {
                        "instance": "z_ff", "cell_type": "FDRE",
                        "bel": "AFF", "placement_mode": "SLICEL",
                        "site": "SLICE_X0Y0",
                    },
                ],
            }],
            "summary": {"clusters": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            handoff = root / "physical-handoff.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            emit_xilinx_mapped_index(ir_path, index)
            write_xilinx_physical_handoff(handoff, packed, placement)
            with open_xilinx_mapped_index(index) as indexed:
                rows = list(indexed.iter_physical_route_pins_by_net(
                    handoff, ordinal_offset=3
                ))
                endpoint_rows = list(
                    indexed.iter_physical_route_endpoints_by_net(
                        handoff, ordinal_offset=3
                    )
                )
                with self.assertRaisesRegex(ValidationError, "ordinal offset"):
                    list(indexed.iter_physical_route_pins_by_net(
                        handoff, ordinal_offset=-1
                    ))

        by_pin = {
            (row[1], row[2]): (row[7], row[8]) for row in rows
        }
        self.assertEqual(by_pin[("a_lut", "I0")], (3, "I0"))
        self.assertEqual(by_pin[("a_lut", "I1")], (4, "I1"))
        self.assertEqual(by_pin[("a_lut", "O5")], (3, "O"))
        self.assertEqual(by_pin[("a_lut", "O6")], (4, "O"))
        self.assertEqual(by_pin[("z_ff", "D")], (5, "D"))
        self.assertEqual(by_pin[("z_ff", "Q")], (5, "Q"))
        self.assertEqual(
            {(row[1], row[3]): row[2] for row in endpoint_rows},
            {
                ("a_lut", "I0"): "LUT6_2",
                ("a_lut", "I1"): "LUT6_2",
                ("a_lut", "O5"): "LUT6_2",
                ("a_lut", "O6"): "LUT6_2",
                ("z_ff", "D"): "FDRE",
                ("z_ff", "Q"): "FDRE",
            },
        )

    def test_v1_index_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)
                  WITHOUT ROWID;
                CREATE TABLE cells(
                  id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,type TEXT NOT NULL,
                  record_json TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE netnames(
                  id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,
                  record_json TEXT NOT NULL
                ) WITHOUT ROWID;
            """)
            connection.executemany(
                "INSERT INTO metadata VALUES (?,?)",
                (
                    ("schema", "emuflow.xilinx-mapped-index/v1"),
                    ("top_json", json.dumps("top")),
                    ("creator_json", json.dumps("fixture")),
                    ("ports_json", json.dumps({})),
                ),
            )
            cell = {
                "type": "LUT1", "parameters": {}, "attributes": {},
                "port_directions": {"O": "output"},
                "connections": {"O": [2]},
            }
            connection.execute(
                "INSERT INTO cells VALUES (?,?,?,?)",
                ("lut", 0, "LUT1", json.dumps(cell)),
            )
            connection.execute(
                "INSERT INTO netnames VALUES (?,?,?)",
                ("n", 0, json.dumps({"bits": [2]})),
            )
            connection.commit()
            connection.close()

            self.assertTrue(is_xilinx_mapped_index(path))
            with open_xilinx_mapped_index(path) as indexed:
                self.assertEqual(
                    indexed["modules"]["top"]["cells"]["lut"], cell
                )
                self.assertEqual(indexed.cell_type_counts(), {"LUT1": 1})
                self.assertEqual(
                    indexed.cell_type_and_parameters("lut"), ("LUT1", {})
                )

    def test_production_index_is_the_only_mapped_artifact(self) -> None:
        mapped = {
            "creator": "fixture",
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "y": {"direction": "output", "bits": [3]},
                },
                "cells": {"lut": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "attributes": {},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [2], "O": [3]},
                }},
                "netnames": {"a": {"bits": [2]}, "y": {"bits": [3]}},
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            absent_json = root / "mapped.json"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, index)
            with open_xilinx_mapped_index(index) as indexed:
                self.assertEqual(indexed.cell_type_counts(), {"LUT1": 1})
                self.assertEqual(
                    indexed.cell_type_and_parameters("lut"),
                    ("LUT1", {"INIT": "10"}),
                )
                self.assertEqual(
                    indexed["modules"]["top"]["cells"]["lut"]["type"],
                    "LUT1",
                )
                self.assertEqual(
                    list(indexed.iter_pins_by_net()),
                    [
                        (2, "lut", "I0", 0, 1, "input"),
                        (3, "lut", "O", 0, 1, "output"),
                    ],
                )
                self.assertEqual(
                    [name for name, _cell in indexed.iter_cells_by_type("LUT1")],
                    ["lut"],
                )
                self.assertEqual(list(indexed.iter_cells_by_type("LUT2")), [])
            connection = sqlite3.connect(index)
            try:
                schema = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema'"
                ).fetchone()[0]
                record = json.loads(connection.execute(
                    "SELECT record_json FROM cells WHERE id='lut'"
                ).fetchone()[0])
                pin_count = connection.execute(
                    "SELECT count(*) FROM cell_pins"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(schema, "emuflow.xilinx-mapped-index/v2")
            self.assertNotIn("connections", record)
            self.assertNotIn("port_directions", record)
            self.assertEqual(pin_count, 2)
            self.assertTrue(index.is_file())
            self.assertFalse(absent_json.exists())
            self.assertEqual(report["output"], str(index))
            self.assertEqual(
                report["audit"]["mapped_representation"], "sqlite-index"
            )
            self.assertIsNone(report["audit"]["mapped_json_sha256"])

    def test_compact_emuir_streams_to_indexed_mapped_netlist(self) -> None:
        mapped = {
            "creator": "fixture",
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "y": {"direction": "output", "bits": [3]},
                },
                "cells": {"lut": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "attributes": {},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [2], "O": [3]},
                }},
                "netnames": {
                    "a": {"bits": [2]}, "y": {"bits": [3]},
                },
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            output = root / "mapped.json"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            report = emit_xilinx_mapped_json(
                ir_path, output, index_path=index
            )
            emitted = json.loads(output.read_text(encoding="utf-8"))
            with open_xilinx_mapped_index(index) as indexed:
                self.assertEqual(
                    indexed["modules"]["top"]["cells"]["lut"],
                    emitted["modules"]["top"]["cells"]["lut"],
                )
                self.assertEqual(len(indexed["modules"]["top"]["cells"]), 1)
                self.assertEqual(
                    set(indexed["modules"]["top"]["netnames"]), {"a", "y"}
                )
        self.assertEqual(report["provider"],
                         "emuflow-emuir-to-xilinx-yosys-json-indexed-v1")
        self.assertEqual(report["cells"], 1)
        self.assertEqual(report["nets"], 2)

    def test_split_partition_preserves_undriven_output_bits_as_x(self) -> None:
        mapped = {
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "y": {"direction": "output", "bits": [3]},
                },
                "cells": {"lut": {
                    "type": "LUT1",
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [2], "O": [3]},
                }},
                "netnames": {
                    "a": {"bits": [2]}, "y": {"bits": [3]},
                },
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.json"
            output = root / "mapped.json"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            ir = import_yosys_json(source)
            value = ir.value
            output_port = next(
                port for port in value["ports"] if port["id"] == "y"
            )
            output_port["width"] = 2
            write_json(ir_path, value)
            report = emit_xilinx_mapped_json(ir_path, output)
            emitted = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            emitted["modules"]["top"]["ports"]["y"]["bits"], [3, "x"]
        )
        self.assertEqual(report["unconnected_output_bits"], 1)

    def test_split_partition_preserves_unused_input_bits_as_sinkless(self) -> None:
        mapped = {
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "y": {"direction": "output", "bits": [3]},
                },
                "cells": {"lut": {
                    "type": "LUT1",
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [2], "O": [3]},
                }},
                "netnames": {
                    "a": {"bits": [2]}, "y": {"bits": [3]},
                },
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.json"
            output = root / "mapped.json"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            ir = import_yosys_json(source)
            value = ir.value
            input_port = next(
                port for port in value["ports"] if port["id"] == "a"
            )
            input_port["width"] = 2
            write_json(ir_path, value)
            report = emit_xilinx_mapped_json(ir_path, output)
            emitted = json.loads(output.read_text(encoding="utf-8"))

        bits = emitted["modules"]["top"]["ports"]["a"]["bits"]
        self.assertEqual(bits[0], 2)
        self.assertIsInstance(bits[1], int)
        self.assertNotEqual(bits[1], 2)
        self.assertEqual(report["unconnected_input_bits"], 1)

    def test_native_lut6_2_is_preserved(self) -> None:
        inputs = list(range(2, 8))
        mapped = {
            "modules": {"top": {
                "attributes": {"top": "1"},
                "ports": {
                    "a": {"direction": "input", "bits": inputs},
                    "o5": {"direction": "output", "bits": [8]},
                    "o6": {"direction": "output", "bits": [9]},
                },
                "cells": {"lut": {
                    "type": "LUT6_2",
                    "parameters": {"INIT": "0" * 64},
                    "attributes": {},
                    "port_directions": {
                        **{f"I{index}": "input" for index in range(6)},
                        "O5": "output", "O6": "output",
                    },
                    "connections": {
                        **{f"I{index}": [inputs[index]] for index in range(6)},
                        "O5": [8], "O6": [9],
                    },
                }},
                "netnames": {
                    **{f"a{index}": {"bits": [bit]}
                       for index, bit in enumerate(inputs)},
                    "o5": {"bits": [8]}, "o6": {"bits": [9]},
                },
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.json"
            output = root / "mapped.json"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            write_json(ir_path, import_yosys_json(source).value)
            emit_xilinx_mapped_json(ir_path, output)
            emitted = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            emitted["modules"]["top"]["cells"]["lut"]["type"], "LUT6_2"
        )

    def test_emuir_roundtrip_preserves_cells_ports_and_nets(self) -> None:
        mapped = {
            "creator": "fixture",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "clk": {"direction": "input", "bits": [2]},
                        "a": {"direction": "input", "bits": [3]},
                        "y": {"direction": "output", "bits": [5]},
                        "zero": {"direction": "output", "bits": ["0"]},
                    },
                    "cells": {
                        "lut": {
                            "type": "LUT1",
                            "parameters": {"INIT": "10"},
                            "attributes": {},
                            "port_directions": {"I0": "input", "O": "output"},
                            "connections": {"I0": [3], "O": [4]},
                        },
                        "ff": {
                            "type": "FDRE",
                            "parameters": {"INIT": "0"},
                            "attributes": {},
                            "port_directions": {
                                "C": "input", "CE": "input", "D": "input",
                                "R": "input", "Q": "output",
                            },
                            "connections": {
                                "C": [2], "CE": ["1"], "D": [4],
                                "R": ["0"], "Q": [5],
                            },
                        },
                    },
                    "netnames": {
                        "clk": {"bits": [2]}, "a": {"bits": [3]},
                        "comb": {"bits": [4]}, "y": {"bits": [5]},
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.json"
            output = root / "mapped.json"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            ir = import_yosys_json(source, clocks=("clk",))
            write_json(ir_path, ir.value)
            report = emit_xilinx_mapped_json(ir_path, output)
            roundtrip = import_yosys_json(output, clocks=("clk",))
            self.assertEqual(report["cells"], 2)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["modules"]
                ["top"]["ports"]["zero"]["bits"],
                ["0"],
            )
            self.assertEqual(
                [(item["id"], item["type"]) for item in roundtrip.value["instances"]],
                [("ff", "FDRE"), ("lut", "LUT1")],
            )
            self.assertEqual(len(roundtrip.value["nets"]), 4)
            self.assertIn("assign \\zero  = 1'b0;", mapped_verilog(roundtrip))


if __name__ == "__main__":
    unittest.main()
