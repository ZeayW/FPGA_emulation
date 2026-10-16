import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from emuflow.emuir_compact import write_compact_emuir, read_compact_emuir
from emuflow.errors import ValidationError
from emuflow.xilinx_bram_ports import bram_port_declarations, repair_compact_bram_ports
from emuflow.yosys import import_yosys_json

DECLARATIONS = Path(__file__).resolve().parents[1] / "engines/yosys/techlibs/xilinx/cells_xtra.v"


def fixture():
    return {"modules": {"top": {"cells": {
        "src": {"type": "LUT1", "port_directions": {"O": "output"},
                "connections": {"O": [1]}},
        "ram": {"type": "RAMB18E2", "parameters": {"WRITE_WIDTH_A": "10010"},
                "attributes": {"keep": 1},
                "port_directions": {"WEA": "input", "ADDRARDADDR": "input", "DOUTADOUT": "output"},
                "connections": {"WEA": [1, 1, 1, 1],
                                "ADDRARDADDR": ["0"] * 16, "DOUTADOUT": list(range(2, 18))}},
        "ff": {"type": "FDRE", "port_directions": {"D": "input"}, "connections": {"D": [2]}},
    }}}}


class XilinxBramPortRecoveryTest(unittest.TestCase):
    def test_public_declarations_match_half_and_full_widths(self):
        ports = bram_port_declarations(DECLARATIONS)
        self.assertEqual(ports["RAMB18E2"]["WEA"], ("input", 2))
        self.assertEqual(ports["RAMB18E2"]["ADDRARDADDR"], ("input", 14))
        self.assertEqual(ports["RAMB36E2"]["WEBWE"], ("input", 8))

    def test_recovery_preserves_ids_resources_and_true_input_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, source, output = root / "mapped.json", root / "source.sqlite", root / "out.sqlite"
            mapped.write_text(json.dumps(fixture()))
            original = import_yosys_json(mapped).value
            write_compact_emuir(source, original)
            source_bytes = source.read_bytes()
            # No whole EmuIR materialization is allowed in the repair path.
            with mock.patch("emuflow.emuir_compact.read_compact_emuir", side_effect=AssertionError("full graph")):
                report = repair_compact_bram_ports(source, output, DECLARATIONS)
            self.assertEqual(source.read_bytes(), source_bytes)
            repaired = read_compact_emuir(output)
            expected = copy.deepcopy(original)
            for instance in expected["instances"]:
                if instance["id"] == "ram":
                    instance["constant_connections"] = [x for x in instance["constant_connections"]
                                                        if x["port"] != "ADDRARDADDR" or x["bit"] < 14]
            for net in expected["nets"]:
                net["sinks"] = [x for x in net["sinks"]
                                if not (x["instance"] == "ram" and x["port"] == "WEA" and x["bit"] >= 2)]
                net["fanout"] = len(net["sinks"])
            self.assertEqual(repaired, expected)
            self.assertEqual(report["removed_endpoints"], 2)
            self.assertEqual(report["removed_constant_bits"], 2)
            self.assertIn("must-regenerate", report["downstream_timing"])
            # Verilog input-port sizing observes the same two low WE bits,
            # independently of both discarded high bits' values.
            for value in range(16):
                before = [(value >> bit) & 1 for bit in range(4)]
                self.assertEqual(sum(x << i for i, x in enumerate(before[:2])), value & 3)
            second = root / "second.sqlite"
            second_report = repair_compact_bram_ports(output, second, DECLARATIONS)
            self.assertEqual(second_report["removed_endpoints"], 0)
            self.assertEqual(second_report["removed_constant_bits"], 0)
            self.assertEqual(read_compact_emuir(second), repaired)

    def test_wrong_directions_or_unknown_ports_are_not_silently_removed(self):
        for port, direction in (("DOUTADOUT", "input"), ("UNKNOWN", "input")):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                value = fixture()
                ram = value["modules"]["top"]["cells"]["ram"]
                ram["port_directions"][port] = direction
                ram["connections"][port] = [3] * 17
                mapped, source, output = root / "mapped.json", root / "source.sqlite", root / "out.sqlite"
                mapped.write_text(json.dumps(value))
                write_compact_emuir(source, import_yosys_json(mapped).value)
                with self.assertRaisesRegex(ValidationError, "unsupported BRAM"):
                    repair_compact_bram_ports(source, output, DECLARATIONS)
                self.assertFalse(output.exists())
                self.assertFalse(list(root.glob(".bram-port-recovery-*")))

    def test_existing_output_and_mutating_source_are_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, source, output = root / "mapped.json", root / "source.sqlite", root / "out.sqlite"
            mapped.write_text(json.dumps(fixture()))
            write_compact_emuir(source, import_yosys_json(mapped).value)
            output.write_bytes(b"keep")
            with self.assertRaises(ValidationError):
                repair_compact_bram_ports(source, output, DECLARATIONS)
            self.assertEqual(output.read_bytes(), b"keep")
            output.unlink()
            real_copy = __import__("shutil").copyfile

            def changing_copy(a, b):
                real_copy(a, b)
                with source.open("ab") as stream:
                    stream.write(b"changed")

            with mock.patch("emuflow.xilinx_bram_ports.shutil.copyfile", side_effect=changing_copy):
                with self.assertRaisesRegex(ValidationError, "source changed"):
                    repair_compact_bram_ports(source, output, DECLARATIONS)
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".bram-port-recovery-*")))

    def test_unsigned_output_extension_becomes_constant_consumers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = fixture()
            module = value["modules"]["top"]
            module["ports"] = {"result": {"direction": "output", "bits": [18]}}
            module["cells"]["ram"]["connections"]["DOUTADOUT"] = list(range(2, 34))
            module["cells"]["ff"]["connections"]["D"] = [18]
            mapped, source, output = root / "mapped.json", root / "source.sqlite", root / "out.sqlite"
            mapped.write_text(json.dumps(value))
            original = import_yosys_json(mapped).value
            write_compact_emuir(source, original)
            report = repair_compact_bram_ports(source, output, DECLARATIONS)
            repaired = read_compact_emuir(output)
            self.assertEqual(report["zero_extended_output_nets"], 16)
            self.assertEqual(report["added_constant_inputs"], 1)
            self.assertEqual(report["top_constant_bits"], 1)
            ff = next(x for x in repaired["instances"] if x["id"] == "ff")
            self.assertIn({"port": "D", "bit": 0, "value": "0"}, ff["constant_connections"])
            self.assertEqual(repaired["ports"][0]["constant_connections"], [{"bit": 0, "value": "0"}])
            self.assertEqual([x["id"] for x in repaired["instances"]], [x["id"] for x in original["instances"]])
            self.assertEqual([x["id"] for x in repaired["nets"]], [x["id"] for x in original["nets"]])
            for net in repaired["nets"]:
                for endpoint in net["drivers"]:
                    self.assertFalse(endpoint["instance"] == "ram" and endpoint["port"] == "DOUTADOUT" and endpoint["bit"] >= 16)
            from emuflow.ir import EmuIR
            EmuIR(repaired).validate()
