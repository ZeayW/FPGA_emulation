import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, write_json
from emuflow.verilog import mapped_verilog
from emuflow.xilinx_netlist import (
    emit_xilinx_mapped_index,
    emit_xilinx_mapped_json,
    open_xilinx_mapped_index,
)
from emuflow.yosys import import_yosys_json
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


class XilinxMappedNetlistTests(unittest.TestCase):
    def test_native_sql_index_matches_reference_without_python_cell_lowering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, _architecture = write_openparf_runtime_fixture(
                root, include_hard=True,
            )
            value = json.loads(mapped.read_text())
            value["modules"]["top"]["cells"]["lut_00"]["attributes"] = {
                "unicode": "路径\\\"\n", "nested": [None, True, 1.0, 2e-9],
                "large_integer": 123456789012345678901234567890,
            }
            mapped.write_text(json.dumps(value))
            source, native, reference = (
                root / "source.emuir.sqlite", root / "native.sqlite", root / "reference.sqlite"
            )
            import_yosys_json(mapped).save(source)
            with mock.patch(
                "emuflow.xilinx_netlist._lower_instance",
                side_effect=AssertionError("native mapped cells passed through Python"),
            ), mock.patch(
                "emuflow.xilinx_netlist._decode",
                side_effect=AssertionError("native mapped payload was decoded in Python"),
            ), mock.patch(
                "emuflow.xilinx_netlist._RowsForOwner",
                side_effect=AssertionError("native endpoint stream entered Python"),
            ), mock.patch(
                "emuflow.xilinx_netlist.file_sha256",
                wraps=file_sha256,
            ) as digest:
                native_report = emit_xilinx_mapped_index(source, native)
            self.assertEqual(
                sum(Path(call.args[0]) == native for call in digest.call_args_list), 1,
            )
            reference_report = emit_xilinx_mapped_json(
                source, root / "reference.json", index_path=reference,
            )
            for field in ("cells", "nets", "ports", "unconnected_input_bits", "unconnected_output_bits"):
                self.assertEqual(native_report[field], reference_report[field], field)
            with sqlite3.connect(native) as a, sqlite3.connect(reference) as b:
                for table in ("metadata", "cells", "cell_pins", "netnames"):
                    self.assertEqual(
                        a.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall(),
                        b.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall(), table,
                    )

    def test_generic_lut_and_ff_still_receive_explicit_primitive_lowering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "generic.json"
            mapped.write_text(json.dumps({"modules": {"top": {
                "attributes": {"top": "1"},
                "cells": {
                    "lut": {
                        "type": "$lut", "parameters": {"WIDTH": "10", "LUT": "0110"},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [2, 3], "Y": [4]},
                    },
                    "ff": {
                        "type": "$_DFF_N_", "parameters": {},
                        "port_directions": {"C": "input", "D": "input", "Q": "output"},
                        "connections": {"C": [5], "D": [4], "Q": [6]},
                    },
                },
            }}}))
            source, index = root / "source.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(source)
            with mock.patch(
                "emuflow.xilinx_netlist._populate_native_xilinx_index",
                side_effect=AssertionError("generic cells cannot bypass lowering"),
            ):
                emit_xilinx_mapped_index(source, index)
            with open_xilinx_mapped_index(index) as indexed:
                cells = indexed["modules"]["top"]["cells"]
                self.assertEqual(cells["lut"]["type"], "LUT2")
                self.assertEqual(set(cells["lut"]["connections"]), {"I0", "I1", "O"})
                self.assertEqual(cells["ff"]["type"], "FDRE")
                self.assertEqual(cells["ff"]["parameters"]["IS_C_INVERTED"], "1")
                self.assertEqual(cells["ff"]["connections"]["CE"], ["1"])
                self.assertEqual(cells["ff"]["connections"]["R"], ["0"])

    def test_native_sql_rejects_conflicting_or_noncontiguous_pins_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, _architecture = write_openparf_runtime_fixture(root)
            source, output = root / "source.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(source)
            emit_xilinx_mapped_index(source, output)
            original = output.read_bytes()
            with sqlite3.connect(source) as c:
                cell, port, net, role = c.execute(
                    "SELECT instance_seq,port_seq,net_seq,role FROM endpoint_records "
                    "WHERE instance_seq IS NOT NULL AND role=1 AND bit=0 LIMIT 1"
                ).fetchone()
                c.execute(
                    "INSERT INTO endpoint_records VALUES(?,?,?,?,?,?,?)",
                    (net, role, 99999, cell, port, 1, 0),
                )
                c.execute(
                    "UPDATE endpoint_records SET bit=2 WHERE ordinal=99999"
                )
            with self.assertRaisesRegex(ValidationError, "not contiguous"):
                emit_xilinx_mapped_index(source, output)
            self.assertEqual(output.read_bytes(), original)
            with sqlite3.connect(source) as c:
                c.execute("DELETE FROM endpoint_records WHERE ordinal=99999")
                next_net = c.execute(
                    "SELECT seq FROM net_records WHERE seq<>? LIMIT 1", (net,)
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO endpoint_records VALUES(?,?,?,?,?,?,?)",
                    (next_net, role, 99999, cell, port, 0, 0),
                )
            with self.assertRaisesRegex(ValidationError, "multiple nets"):
                emit_xilinx_mapped_index(source, output)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(root.glob(".mapped.sqlite.*.tmp")), [])

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
                self.assertEqual(
                    indexed["modules"]["top"]["cells"]["lut"]["type"],
                    "LUT1",
                )
            with sqlite3.connect(index) as database:
                self.assertEqual(
                    database.execute(
                        "SELECT port,bit_index,pin_name,direction,net_bit,constant "
                        "FROM cell_pins ORDER BY cell_seq,pin_seq"
                    ).fetchall(),
                    [
                        ("I0", 0, "I0", "input", 2, None),
                        ("O", 0, "O", "output", 3, None),
                    ],
                )
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
