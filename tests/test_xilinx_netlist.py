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
from emuflow.xilinx_primitives import audit_xilinx_mapped_json
from emuflow.xilinx_packing import pack_xilinx_sites, validate_xilinx_packing
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture
from tests.test_xilinx_packing import add_carry, cell


class XilinxMappedNetlistTests(unittest.TestCase):
    def test_shared_slice_input_projection_keeps_exact_mux_and_ff_nets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "source.json"
            mapped.write_text(json.dumps({"modules": {"top": {"cells": {
                "mux": cell("MUXF7", {"I0": [1], "I1": [2], "S": [30], "O": [3]}),
                "ff": cell("FDPE", {"C": [4], "CE": ["1"], "PRE": ["0"],
                                    "D": [31], "Q": [5]}),
            }, "attributes": {"top": "1"}}}}))
            source, index = root / "source.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(source)
            emit_xilinx_mapped_index(source, index)
            with open_xilinx_mapped_index(index) as indexed:
                expected = {name: indexed.cells[name]["connections"]
                            for name in ("mux", "ff")}
                with mock.patch.object(
                    type(indexed.cells), "__getitem__",
                    side_effect=AssertionError("whole cell deserialization"),
                ):
                    view = indexed.cells.packing_records(["mux", "ff"])
                self.assertEqual(view["mux"]["connections"]["S"], expected["mux"]["S"])
                self.assertEqual(view["ff"]["connections"]["D"], expected["ff"]["D"])
                self.assertNotEqual(expected["mux"]["S"], expected["ff"]["D"])

    def test_indexed_packing_checker_projects_only_site_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, _arch = write_openparf_runtime_fixture(root, include_hard=True)
            value = json.loads(mapped.read_text())
            cells = value["modules"]["top"]["cells"]
            add_carry(cells, "carry0", ci=700000, co=700001, net_base=710000)
            add_carry(cells, "carry1", ci=700008, co=720001, net_base=730000)
            cells["mux_lut0"] = cell("LUT6", {"I0": [740000], "O": [740001]})
            cells["mux_lut1"] = cell("LUT6", {"I0": [740002], "O": [740003]})
            cells["mux"] = cell("MUXF7", {"I0": [740001], "I1": [740003], "S": [740004], "O": [740005]})
            # A repeated real slice shape, with payloads that the checker must
            # never deserialize as cell records. FF JSON types stay exact.
            for item in cells.values():
                item["attributes"] = {"diagnostic": "unused" * 1000}
                if item["type"] == "FDRE":
                    item["parameters"] = {"IS_C_INVERTED": False, "IS_R_INVERTED": "0"}
            cells["bram"]["parameters"] = {"INIT_00": "0" * 32768}
            mapped.write_text(json.dumps(value))
            source, index = root / "source.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(source)
            emit_xilinx_mapped_index(source, index)
            with open_xilinx_mapped_index(index) as indexed:
                # Use the exact canonical source for both representations.
                reference = {"modules": {indexed.top: {"cells": dict(indexed.cells.items())}}}
                packing = pack_xilinx_sites(index, packed, mapped_value=reference)
                expected = validate_xilinx_packing(index, packed, mapped_value=reference)
                projection = indexed.cells.packing_records(["ff_00", "bram", "lut_00"])
                self.assertIs(projection["ff_00"]["parameters"]["IS_C_INVERTED"], False)
                self.assertEqual(projection["bram"]["parameters"], {})
                self.assertEqual(projection["lut_00"]["connections"], {})
                with mock.patch.object(
                    type(indexed.cells), "__getitem__",
                    side_effect=AssertionError("complete cell record decoded"),
                ), mock.patch.object(
                    indexed.cells, "items", side_effect=AssertionError("whole netlist scan"),
                ), mock.patch.object(
                    indexed.cells, "items_of_types", side_effect=AssertionError("hard INIT decoded"),
                ):
                    actual = validate_xilinx_packing(index, packed, mapped_value=indexed)
                    self.assertEqual(actual, expected)
                    drivers = indexed.cells.packing_drivers()
                    self.assertIsNone(drivers.get("0"))
                    cluster = next(c for c in packing["clusters"] if c["kind"] == "slice")
                    cluster["assignments"][0]["cell_type"] = "FDRE"
                    with self.assertRaisesRegex(ValidationError, "does not match source"):
                        validate_xilinx_packing(index, packed, mapped_value=indexed, packed_value=packing)

    def test_repeated_packing_projection_is_bounded_by_one_site(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, _arch = write_openparf_runtime_fixture(root)
            value = json.loads(mapped.read_text())
            cells = value["modules"]["top"]["cells"]
            for i in range(64, 4096):
                cells[f"lut_{i:04d}"] = cell("LUT6", {"I0": [20000 + i - 1], "O": [10000 + i]})
                cells[f"ff_{i:04d}"] = cell("FDRE", {
                    "C": [1000000], "CE": ["1"], "R": ["0"],
                    "D": [10000 + i], "Q": [20000 + i],
                })
            mapped.write_text(json.dumps(value))
            source, index = root / "source.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(source)
            emit_xilinx_mapped_index(source, index)
            with open_xilinx_mapped_index(index) as indexed:
                reference = {"modules": {indexed.top: {"cells": dict(indexed.cells.items())}}}
                pack_xilinx_sites(index, packed, mapped_value=reference)
                with mock.patch.object(
                    type(indexed.cells), "__getitem__",
                    side_effect=AssertionError("complete cell record decoded"),
                ), mock.patch.object(
                    indexed.cells, "items", side_effect=AssertionError("whole-cell scan"),
                ), mock.patch.object(
                    indexed.cells, "packing_records", wraps=indexed.cells.packing_records,
                ) as projection:
                    check = validate_xilinx_packing(index, packed, mapped_value=indexed)
                self.assertEqual(check["cells"], 8192)
                self.assertLessEqual(max(len(call.args[0]) for call in projection.call_args_list), 24)
                self.assertEqual(sum(len(call.args[0]) for call in projection.call_args_list), 8192)

    def test_indexed_packing_driver_check_preserves_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, _arch = write_openparf_runtime_fixture(root)
            source, index = root / "source.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(source)
            emit_xilinx_mapped_index(source, index)
            with sqlite3.connect(index) as database:
                net = database.execute(
                    "SELECT net_bit FROM cell_pins WHERE direction='output' LIMIT 1"
                ).fetchone()[0]
                database.execute(
                    "UPDATE cell_pins SET net_bit=? WHERE direction='output'", (net,),
                )
            with open_xilinx_mapped_index(index) as indexed:
                with self.assertRaisesRegex(ValidationError, "multiple drivers"):
                    indexed.cells.packing_drivers()

    def test_indexed_primitive_audit_uses_sql_without_decoding_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, _architecture = write_openparf_runtime_fixture(
                root, include_hard=True,
            )
            compact, index = root / "mapped.emuir.sqlite", root / "mapped.sqlite"
            import_yosys_json(mapped).save(compact)
            emit_xilinx_mapped_index(compact, index)
            with open_xilinx_mapped_index(index) as indexed:
                reference = {"modules": {indexed.top: {
                    "cells": dict(indexed.cells.items()),
                }}}
                expected = audit_xilinx_mapped_json(index, source=reference)
                with mock.patch.object(
                    indexed.cells, "items", side_effect=AssertionError("full cell scan"),
                ), mock.patch.object(
                    indexed.cells, "values", side_effect=AssertionError("full cell scan"),
                ):
                    actual = audit_xilinx_mapped_json(index, source=indexed)
                self.assertEqual(actual, expected)
                original = reference["modules"][indexed.top]["cells"]["lut_00"]
            corruptions = [
                ("LUT6", "not-json"), ("LUT6", "[]"),
                ("LUT6", json.dumps({**original, "type": None})),
                ("LUT6", json.dumps({**original, "connections": []})),
                ("LUT6", json.dumps({**original, "port_directions": []})),
                ("LUT6", json.dumps({**original, "port_directions": {"O": None}})),
                ("LUT6", json.dumps({**original, "port_directions": {"O": "bad"}})),
                ("FDRE", json.dumps(original)),
                ("UNKNOWN", json.dumps({**original, "type": "UNKNOWN"})),
            ]
            for cell_type, record in corruptions:
                with self.subTest(cell_type=cell_type, record=record):
                    with sqlite3.connect(index) as database:
                        database.execute(
                            "UPDATE cells SET type=?,record_json=? WHERE id='lut_00'",
                            (cell_type, record),
                        )
                    with open_xilinx_mapped_index(index) as indexed:
                        with self.assertRaises(ValidationError):
                            audit_xilinx_mapped_json(index, source=indexed)

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
