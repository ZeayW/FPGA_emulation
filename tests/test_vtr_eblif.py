import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.ir import EmuIR
from emuflow.indexed_physical_ir import IndexedPhysicalIR
from emuflow.logic_segment_timing import _instance_pin_inventory, _vpr_atom_pin
from emuflow.vtr_eblif import emit_vtr_eblif, validate_vtr_eblif_report


def _endpoint(instance, port, bit=0):
    return {"instance": instance, "port": port, "bit": bit}


def _instance(name, cell_type, parameters=None, constants=None):
    return {
        "id": name,
        "name": name,
        "type": cell_type,
        "resources": {},
        "parameters": parameters or {},
        "attributes": {},
        "constant_connections": constants or [],
    }


class VtrEblifTest(unittest.TestCase):
    def test_compact_sparse_owner_ids_keep_pin_and_atom_identity(self):
        value = {
            "schema": "emuflow.emuir/v1",
            "design": {"name": "sparse", "top": "sparse", "source_format": "test"},
            "ports": [{"id": name, "name": name, "direction": direction, "width": 1,
                       "clock": False, "reset": False}
                      for name, direction in (("a", "input"), ("y", "output"))],
            "instances": [_instance("first", "LUT1", {"INIT": "10"}),
                          _instance("second", "LUT1", {"INIT": "01"})],
            "nets": [
                {"id": "a", "drivers": [_endpoint(None, "a")],
                 "sinks": [_endpoint("first", "I0")], "cut_class": "primary_input"},
                {"id": "middle", "drivers": [_endpoint("first", "O")],
                 "sinks": [_endpoint("second", "I0")], "cut_class": "combinational"},
                {"id": "y", "drivers": [_endpoint("second", "O")],
                 "sinks": [_endpoint(None, "y")], "cut_class": "combinational"},
            ],
            "clocks": [], "warnings": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for offset in (0, 1):
                with self.subTest(first_owner=offset):
                    source = root / f"sparse-{offset}.emuir.sqlite"
                    output = root / f"sparse-{offset}.eblif"
                    EmuIR(value).save(source)
                    emit_vtr_eblif(source, output)
                    dense_text = output.read_text()
                    # Phase-7 merge can use one-based or noncontiguous integer
                    # owners; changing these storage keys preserves the circuit.
                    with sqlite3.connect(source) as connection:
                        connection.execute("UPDATE cell_records SET seq=-seq-1")
                        connection.execute("UPDATE cell_records SET seq=(-seq-1)*2+?", (offset,))
                        connection.execute("UPDATE endpoint_records SET instance_seq=instance_seq*2+? WHERE instance_seq IS NOT NULL", (offset,))
                    report = emit_vtr_eblif(source, output)
                    expected = dense_text.replace(".cname i1\n", f".cname i{2+offset}\n")
                    expected = expected.replace(".cname i0\n", f".cname i{offset}\n")
                    self.assertEqual(output.read_text(), expected)
                    self.assertEqual(report["source_instances"], 2)
                    self.assertEqual(report["dangling_outputs"], 0)
                    with IndexedPhysicalIR(source) as indexed:
                        self.assertEqual(indexed.instance_indices["second"], 2 + offset)
                    with sqlite3.connect(source) as connection:
                        connection.execute("UPDATE endpoint_records SET instance_seq=999 WHERE instance_seq=?", (2 + offset,))
                    with self.assertRaisesRegex(ValidationError, "endpoint owner"):
                        emit_vtr_eblif(source, output)

    def test_top_port_aliases_may_share_a_packed_io_block(self) -> None:
        report = {
            "schema": "emuflow.vtr-eblif-report/v1",
            "status": "pass",
            "source_instances": 0,
            "source_inventory": {},
            "memory_atom_expansion": 0,
            "ff_control_luts": 0,
            "emitted_atoms": 0,
            "source_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "top_ports": [
                {
                    "port": "alias_a",
                    "bit": 0,
                    "direction": "input",
                    "net": "shared_net",
                    "source_net": "shared_net",
                    "packed_block": "shared_net",
                },
                {
                    "port": "alias_b",
                    "bit": 0,
                    "direction": "input",
                    "net": "shared_net",
                    "source_net": "shared_net",
                    "packed_block": "shared_net",
                },
            ],
        }

        self.assertEqual(validate_vtr_eblif_report(report)["top_ports"], 2)

    def test_lowering_preserves_logic_and_expands_word_memory(self) -> None:
        instances = [
            _instance("lut", "LUT2", {"INIT": "1000"}),
            _instance(
                "ff",
                "FDRE",
                {"INIT": "0"},
                [
                    {"port": "CE", "bit": 0, "value": "1"},
                    {"port": "R", "bit": 0, "value": "0"},
                ],
            ),
            _instance(
                "mul",
                "VTR_MULTIPLY",
                {"A_WIDTH": 1, "B_WIDTH": 1, "Y_WIDTH": 1},
            ),
            _instance(
                "ram",
                "VTR_SP_RAM",
                {"ADDR_WIDTH": 1, "DATA_WIDTH": 2, "DEPTH": 2},
            ),
        ]
        nets = [
            {
                "id": "a",
                "name": "a",
                "drivers": [_endpoint(None, "a")],
                "sinks": [
                    _endpoint("lut", "I0"),
                    _endpoint("mul", "a"),
                    _endpoint("ram", "addr"),
                ],
                "fanout": 3,
                "cut_class": "primary_input",
            },
            {
                "id": "b",
                "name": "b",
                "drivers": [_endpoint(None, "b")],
                "sinks": [
                    _endpoint("lut", "I1"),
                    _endpoint("mul", "b"),
                    _endpoint("ram", "data", 0),
                    _endpoint("ram", "data", 1),
                ],
                "fanout": 4,
                "cut_class": "primary_input",
            },
            {
                "id": "lut_o",
                "name": "lut_o",
                "drivers": [_endpoint("lut", "O")],
                "sinks": [_endpoint("ff", "D")],
                "fanout": 1,
                "cut_class": "combinational",
            },
            {
                "id": "clk",
                "name": "clk",
                "drivers": [_endpoint(None, "clk")],
                "sinks": [
                    _endpoint("ff", "C"),
                    _endpoint("ram", "clk"),
                ],
                "fanout": 2,
                "cut_class": "clock",
            },
            {
                "id": "we",
                "name": "we",
                "drivers": [_endpoint(None, "we")],
                "sinks": [_endpoint("ram", "we")],
                "fanout": 1,
                "cut_class": "primary_input",
            },
            {
                "id": "q",
                "name": "q",
                "drivers": [_endpoint("ff", "Q")],
                "sinks": [_endpoint(None, "q"), _endpoint(None, "q_alias")],
                "fanout": 2,
                "cut_class": "register_output",
            },
        ]
        value = {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "partition",
                "top": "partition",
                "source_format": "test",
            },
            "ports": [
                {
                    "id": name,
                    "name": name,
                    "direction": direction,
                    "width": 1,
                    "clock": name == "clk",
                    "reset": False,
                }
                for name, direction in (
                    ("a", "input"),
                    ("b", "input"),
                    ("clk", "input"),
                    ("we", "input"),
                    ("q", "output"),
                    ("q_alias", "output"),
                )
            ],
            "instances": instances,
            "nets": nets,
            "clocks": [
                {
                    "id": "clk",
                    "name": "clk",
                    "source_port": "clk",
                    "period_ns": None,
                }
            ],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "partition.json"
            output = root / "partition.eblif"
            write_json(source, value)
            report = emit_vtr_eblif(source, output, root / "report.json")
            text = output.read_text(encoding="utf-8")
            compact_source = root / "partition.emuir.sqlite"
            EmuIR(value).save(compact_source)
            compact_output = root / "partition-compact.eblif"
            with mock.patch(
                "emuflow.vtr_eblif.EmuIR.load",
                side_effect=AssertionError("whole-graph loading forbidden"),
            ):
                compact_report = emit_vtr_eblif(
                    compact_source,
                    compact_output,
                    root / "compact-report.json",
                )
            with IndexedPhysicalIR(compact_source) as indexed:
                self.assertEqual(indexed.instance_indices["ff"], 1)
                self.assertEqual(indexed.net_indices["q"], 5)
                self.assertNotIn("owned-by-another-partition", indexed.nets)
                self.assertIn(("Q", 0), _instance_pin_inventory(indexed)["ff"])
                self.assertEqual(
                    _vpr_atom_pin(
                        indexed,
                        indexed.instance_indices,
                        _endpoint("ff", "Q"),
                        indexed.instances,
                    ),
                    "i1.Q[0]",
                )
            self.assertEqual(compact_output.read_text(encoding="utf-8"), text)
            for field in (
                "source_instances",
                "source_nets",
                "source_inventory",
                "clock_nets",
                "top_ports",
                "ff_control_luts",
                "output_alias_luts",
                "memory_atom_expansion",
                "emitted_atoms",
                "hard_block_atoms",
                "dangling_outputs",
            ):
                self.assertEqual(compact_report[field], report[field])

        self.assertEqual(report["source_instances"], 4)
        self.assertEqual(report["memory_atom_expansion"], 1)
        self.assertEqual(report["ff_control_luts"], 1)
        self.assertEqual(report["emitted_atoms"], 8)
        self.assertEqual(report["output_alias_luts"], 2)
        self.assertEqual(text.count(".subckt single_port_ram "), 2)
        self.assertEqual(text.count(".subckt multiply "), 1)
        self.assertIn(".latch ", text)
        output_ports = [
            item for item in report["top_ports"]
            if item["direction"] == "output"
        ]
        self.assertEqual(len(output_ports), 2)
        self.assertEqual(len({item["packed_block"] for item in output_ports}), 2)
        self.assertEqual({item["source_net"] for item in output_ports}, {"n5"})
        for item in output_ports:
            self.assertIn(f".names n5 {item['net']}\n1 1", text)
        self.assertEqual(report["validation"]["status"], "pass")

    def test_unsupported_primitive_is_rejected(self) -> None:
        value = {
            "schema": "emuflow.emuir/v1",
            "design": {"name": "bad", "top": "bad", "source_format": "test"},
            "ports": [],
            "instances": [_instance("bad", "CARRY8")],
            "nets": [],
            "clocks": [],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bad.json"
            write_json(source, value)
            with self.assertRaisesRegex(ValidationError, "CARRY8"):
                emit_vtr_eblif(source, Path(temporary) / "bad.eblif")


if __name__ == "__main__":
    unittest.main()
