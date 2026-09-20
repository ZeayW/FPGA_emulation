import json
import tempfile
import unittest
from pathlib import Path

from emuflow.io import write_json
from emuflow.verilog import mapped_verilog
from emuflow.xilinx_netlist import emit_xilinx_mapped_json
from emuflow.yosys import import_yosys_json


class XilinxMappedNetlistTests(unittest.TestCase):
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
