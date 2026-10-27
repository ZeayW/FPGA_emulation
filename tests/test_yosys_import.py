import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from emuflow.common.io import read_json, write_json
from emuflow.common.errors import ImportError
from emuflow.ir.ir import EmuIR
from emuflow.frontend.yosys import import_yosys_json, import_yosys_json_indexed


ROOT = Path(__file__).resolve().parents[1]


class YosysImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json",
            top="counter",
            clocks=["clk"],
        )

    def test_resource_classification(self) -> None:
        totals = self.ir.resource_totals()
        self.assertEqual(totals.lut, 4)
        self.assertEqual(totals.ff, 4)
        self.assertEqual(totals.other, 0)

    def test_cut_classification(self) -> None:
        classes = {net["cut_class"] for net in self.ir.value["nets"]}
        self.assertIn("clock", classes)
        self.assertIn("reset", classes)
        self.assertIn("register_output", classes)
        self.assertIn("register_input", classes)
        register_inputs = {
            net["id"]
            for net in self.ir.value["nets"]
            if net["cut_class"] == "register_input"
        }
        self.assertEqual(
            register_inputs,
            {"next_q[0]", "next_q[1]", "next_q[2]", "next_q[3]"},
        )
        net_ids = {net["id"] for net in self.ir.value["nets"]}
        self.assertTrue({"q[0]", "q[1]", "q[2]", "q[3]"}.issubset(net_ids))

    def test_stats(self) -> None:
        stats = self.ir.stats()
        self.assertEqual(stats["instances"], 8)
        self.assertEqual(stats["resource_totals"], {"ff": 4, "lut": 4})
        self.assertEqual(stats["clocks"], 1)

    def test_scopeinfo_metadata_is_not_a_physical_instance(self) -> None:
        source = read_json(ROOT / "examples/yosys/counter.json")
        source["modules"]["counter"]["cells"]["debug_scope"] = {
            "type": "$scopeinfo",
            "parameters": {"TYPE": "module"},
            "attributes": {"module": "counter"},
            "port_directions": {},
            "connections": {},
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "counter-with-scopeinfo.json"
            write_json(path, source)
            ir = import_yosys_json(path, top="counter", clocks=["clk"])

        self.assertNotIn(
            "$scopeinfo", {instance["type"] for instance in ir.value["instances"]}
        )
        self.assertEqual(
            ir.resource_totals().to_dict(include_zeros=False),
            {"ff": 4, "lut": 4},
        )

    def test_synchronous_controls_are_transport_safe_but_async_controls_are_not(self) -> None:
        source = {
            "creator": "test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "clk": {"direction": "input", "bits": [2]},
                        "control": {"direction": "input", "bits": [3]},
                    },
                    "cells": {
                        "control_lut": {
                            "type": "LUT1",
                            "parameters": {"INIT": "2"},
                            "attributes": {},
                            "port_directions": {"I0": "input", "O": "output"},
                            "connections": {"I0": [3], "O": [4]},
                        },
                        "sync_reset_ff": {
                            "type": "FDRE",
                            "parameters": {},
                            "attributes": {},
                            "port_directions": {
                                "C": "input", "CE": "input", "D": "input",
                                "Q": "output", "R": "input",
                            },
                            "connections": {"C": [2], "D": ["0"], "Q": [5], "R": [4]},
                        },
                        "sync_set_ff": {
                            "type": "FDSE",
                            "parameters": {},
                            "attributes": {},
                            "port_directions": {
                                "C": "input", "CE": "input", "D": "input",
                                "Q": "output", "S": "input",
                            },
                            "connections": {"C": [2], "D": ["0"], "Q": [6], "S": [4]},
                        },
                        "async_clear_ff": {
                            "type": "FDCE",
                            "parameters": {},
                            "attributes": {},
                            "port_directions": {
                                "C": "input", "CE": "input", "CLR": "input",
                                "D": "input", "Q": "output",
                            },
                            "connections": {"C": [2], "CLR": [7], "D": ["0"], "Q": [8]},
                        },
                        "async_preset_ff": {
                            "type": "FDPE",
                            "parameters": {},
                            "attributes": {},
                            "port_directions": {
                                "C": "input", "CE": "input", "PRE": "input",
                                "D": "input", "Q": "output",
                            },
                            "connections": {"C": [2], "PRE": [7], "D": ["0"], "Q": [9]},
                        },
                        "async_control_lut": {
                            "type": "LUT1",
                            "parameters": {"INIT": "2"},
                            "attributes": {},
                            "port_directions": {"I0": "input", "O": "output"},
                            "connections": {"I0": [3], "O": [7]},
                        },
                    },
                    "netnames": {
                        "clk": {"hide_name": 0, "bits": [2]},
                        "control": {"hide_name": 0, "bits": [3]},
                        "sync_control": {"hide_name": 0, "bits": [4]},
                        "q_reset": {"hide_name": 0, "bits": [5]},
                        "q_set": {"hide_name": 0, "bits": [6]},
                        "async_control": {"hide_name": 0, "bits": [7]},
                        "q_clear": {"hide_name": 0, "bits": [8]},
                        "q_preset": {"hide_name": 0, "bits": [9]},
                    },
                }
            },
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "controls.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            ir = import_yosys_json(path, top="top", clocks=["clk"])

        classes = {net["id"]: net["cut_class"] for net in ir.value["nets"]}
        self.assertEqual(classes["sync_control"], "register_input")
        self.assertEqual(classes["async_control"], "combinational")

    def test_explicit_clock_contract_overrides_name_inference(self) -> None:
        source = read_json(ROOT / "examples/yosys/counter.json")
        module = source["modules"]["counter"]
        next_bit = max(
            bit
            for port in module["ports"].values()
            for bit in port["bits"]
            if isinstance(bit, int)
        ) + 1
        module["ports"]["global_clk_ovr_on"] = {
            "direction": "input", "bits": [next_bit]
        }
        module["ports"]["tmc2slcg_disable_clock_gating"] = {
            "direction": "input", "bits": [next_bit + 1]
        }
        module["netnames"]["global_clk_ovr_on"] = {
            "hide_name": 0, "bits": [next_bit]
        }
        module["netnames"]["tmc2slcg_disable_clock_gating"] = {
            "hide_name": 0, "bits": [next_bit + 1]
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "clock-controls.json"
            indexed_path = root / "clock-controls.emuir.sqlite"
            write_json(source_path, source)
            in_memory = import_yosys_json(
                source_path, top="counter", clocks=["clk"]
            )
            import_yosys_json_indexed(
                source_path,
                indexed_path,
                top="counter",
                clocks=["clk"],
            )
            indexed = EmuIR.load(indexed_path)

        for ir in (in_memory, indexed):
            self.assertEqual(
                [clock["id"] for clock in ir.value["clocks"]], ["clk"]
            )
            ports = {port["id"]: port for port in ir.value["ports"]}
            self.assertFalse(ports["global_clk_ovr_on"]["clock"])
            self.assertFalse(
                ports["tmc2slcg_disable_clock_gating"]["clock"]
            )

    def test_explicit_clock_contract_rejects_unknown_port(self) -> None:
        with self.assertRaisesRegex(ImportError, "absent from the top module"):
            import_yosys_json(
                ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["missing_clk"],
            )


if __name__ == "__main__":
    unittest.main()
