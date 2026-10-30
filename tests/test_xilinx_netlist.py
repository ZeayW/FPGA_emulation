from tests.physical_handoff_fixture import write_handoff_fixture
import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.common.errors import ValidationError
from emuflow.physical.xilinx_netlist import (
    emit_xilinx_mapped_index,
    is_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from tests.yosys_reference import import_yosys_json


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

    def test_native_production_index_preserves_mixed_cell_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(self._fixture()), encoding="utf-8")
            import_yosys_json(source, clocks=("clk",)).save(ir_path)
            source_hash = hashlib.sha256(ir_path.read_bytes()).hexdigest()
            source_mtime = ir_path.stat().st_mtime_ns
            report = emit_xilinx_mapped_index(ir_path, index)
            self.assertEqual(report["index_builder"], "native-primitive-projection-v1")
            with open_xilinx_mapped_index(index) as indexed:
                module = indexed["modules"]["top"]
                self.assertEqual(indexed.cell_type_counts(), {"FDRE": 1, "LUT1": 1})
                for name, expected in self._fixture()["modules"]["top"]["cells"].items():
                    self.assertEqual(module["cells"][name], {"hide_name": 0, **expected})
                self.assertEqual(module["ports"], self._fixture()["modules"]["top"]["ports"])
                self.assertEqual(set(module["netnames"]), {"clk", "a", "comb", "y"})
                for name, bit in (("clk", 2), ("a", 3), ("comb", 4), ("y", 5)):
                    self.assertEqual(module["netnames"][name]["bits"], [bit])
            self.assertEqual(hashlib.sha256(ir_path.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(ir_path.stat().st_mtime_ns, source_mtime)

    def test_native_index_preserves_sparse_identities_and_escaped_unicode_names(self) -> None:
        mapped = self._fixture()
        cells = mapped["modules"]["top"]["cells"]
        lut_name, ff_name = "\\层/LUT [0]", "\\层/FF α"
        cells[lut_name] = cells.pop("lut")
        cells[ff_name] = cells.pop("ff")
        netnames = mapped["modules"]["top"]["netnames"]
        netnames["\\层/组合 α"] = netnames.pop("comb")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(mapped, ensure_ascii=False), encoding="utf-8")
            ir = import_yosys_json(source, clocks=("clk",))
            next(port for port in ir.value["ports"] if port["id"] == "a")["width"] = 2
            ir.save(ir_path)
            connection = sqlite3.connect(ir_path)
            try:
                connection.executescript("""
                    UPDATE net_records SET seq=seq*10+10;
                    UPDATE endpoint_records SET net_seq=net_seq*10+10;
                    UPDATE alias_records SET net_seq=net_seq*10+10;
                    UPDATE cell_records SET seq=seq*10+11;
                    UPDATE endpoint_records SET instance_seq=instance_seq*10+11
                      WHERE instance_seq IS NOT NULL;
                """)
                connection.commit()
            finally:
                connection.close()
            report = emit_xilinx_mapped_index(ir_path, index)
            self.assertEqual(report["index_builder"], "native-primitive-projection-v1")
            self.assertEqual(report["unconnected_input_bits"], 1)
            with open_xilinx_mapped_index(index) as indexed:
                module = indexed["modules"]["top"]
                self.assertEqual(set(module["cells"]), {lut_name, ff_name})
                self.assertEqual(module["cells"][lut_name]["connections"], {
                    "I0": [22], "O": [32],
                })
                self.assertEqual(module["cells"][ff_name]["connections"], {
                    "C": [12], "CE": ["1"], "D": [32], "R": ["0"], "Q": [42],
                })
                self.assertEqual(module["netnames"]["\\层/组合 α"]["bits"], [32])
                self.assertEqual(module["ports"]["a"]["bits"], [22, 43])
                self.assertEqual(module["ports"]["y"]["bits"], [42])
                self.assertNotIn(43, {pin[0] for pin in indexed.iter_pins_by_net()})

    def test_native_index_preserves_bram_macro_parameters_and_pin_vectors(self) -> None:
        mapped = self._fixture()
        module = mapped["modules"]["top"]
        memory_bits = list(range(6, 22))
        macro = {
            "type": "RAMB18E2",
            "parameters": {"READ_WIDTH_A": "00000000000000000000000000010010"},
            "attributes": {"keep": "1"},
            "port_directions": {
                "CLKARDCLK": "input", "ENARDEN": "input",
                "ADDRARDADDR": "input", "DOADO": "output",
            },
            "connections": {
                "CLKARDCLK": [2], "ENARDEN": ["1"],
                "ADDRARDADDR": ["0"] * 14, "DOADO": memory_bits,
            },
        }
        module["cells"]["memory"] = macro
        module["ports"]["memory_data"] = {"direction": "output", "bits": memory_bits}
        module["netnames"]["memory_data"] = {"bits": memory_bits}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            ir_path = root / "source.emuir.sqlite"
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source, clocks=("clk",)).save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, index)
            self.assertEqual(report["index_builder"], "native-primitive-projection-v1")
            self.assertEqual(report["audit"]["hard_block_cells"], {"RAMB18E2": 1})
            with open_xilinx_mapped_index(index) as indexed:
                module = indexed["modules"]["top"]
                self.assertEqual(module["cells"]["memory"], {"hide_name": 0, **macro})
                self.assertEqual(module["ports"]["memory_data"]["bits"], memory_bits)
                self.assertEqual(indexed.cell_type_counts(), {"FDRE": 1, "LUT1": 1, "RAMB18E2": 1})

    def test_generic_lut_uses_native_lowering(self) -> None:
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
        self.assertEqual(report["index_builder"], "native-primitive-lowering-v1")

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
            write_handoff_fixture(handoff, packed, placement)
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
            self.assertEqual(report["output"], str(index.resolve()))
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
            index = root / "mapped.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, index)
            with open_xilinx_mapped_index(index) as indexed:
                self.assertEqual(
                    indexed["modules"]["top"]["cells"]["lut"],
                    {"hide_name": 0, **mapped["modules"]["top"]["cells"]["lut"]},
                )
                self.assertEqual(len(indexed["modules"]["top"]["cells"]), 1)
                self.assertEqual(
                    set(indexed["modules"]["top"]["netnames"]), {"a", "y"}
                )
        self.assertEqual(report["provider"],
                         "emuflow-emuir-to-xilinx-sqlite-index-v2")
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
            ir_path = root / "source.emuir.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            ir = import_yosys_json(source)
            value = ir.value
            output_port = next(
                port for port in value["ports"] if port["id"] == "y"
            )
            output_port["width"] = 2
            ir.save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, root / "mapped.sqlite")
            with open_xilinx_mapped_index(root / "mapped.sqlite") as indexed:
                ports = indexed.ports

        self.assertEqual(
            ports["y"]["bits"], [3, "x"]
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
            ir_path = root / "source.emuir.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            ir = import_yosys_json(source)
            value = ir.value
            input_port = next(
                port for port in value["ports"] if port["id"] == "a"
            )
            input_port["width"] = 2
            ir.save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, root / "mapped.sqlite")
            with open_xilinx_mapped_index(root / "mapped.sqlite") as indexed:
                ports = indexed.ports

        bits = ports["a"]["bits"]
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
            ir_path = root / "source.emuir.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            emit_xilinx_mapped_index(ir_path, root / "mapped.sqlite")
            with open_xilinx_mapped_index(root / "mapped.sqlite") as indexed:
                self.assertEqual(
                    indexed["modules"]["top"]["cells"]["lut"],
                    {"hide_name": 0, **mapped["modules"]["top"]["cells"]["lut"]},
                )

    def test_index_preserves_cells_constants_ports_and_nets(self) -> None:
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
            ir_path = root / "source.emuir.sqlite"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            ir = import_yosys_json(source, clocks=("clk",))
            ir.save(ir_path)
            report = emit_xilinx_mapped_index(ir_path, root / "mapped.sqlite")
            with open_xilinx_mapped_index(root / "mapped.sqlite") as indexed:
                module = indexed["modules"]["top"]
                self.assertEqual(module["ports"]["zero"]["bits"], ["0"])
                self.assertEqual(
                    [(name, cell["type"]) for name, cell in module["cells"].items()],
                    [("ff", "FDRE"), ("lut", "LUT1")],
                )
                self.assertEqual(module["cells"]["ff"]["connections"], {
                    "C": [2], "CE": ["1"], "D": [4], "R": ["0"], "Q": [5],
                })
                self.assertEqual(len(module["netnames"]), 4)
            self.assertEqual(report["cells"], 2)


if __name__ == "__main__":
    unittest.main()
