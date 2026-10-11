import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from emuflow.ir import EmuIR
from emuflow.phase1 import run_phase1
from emuflow.xilinx_primitives import normalize_xilinx_indexed_staging


ROOT = Path(__file__).resolve().parents[1]


class Phase1Test(unittest.TestCase):
    def test_xilinx_stream_normalizes_without_graph_json_artifact(self) -> None:
        value = {
            "creator": "stream-test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "a": {"direction": "input", "bits": [2]},
                        "y": {"direction": "output", "bits": [3]},
                    },
                    "netnames": {
                        "a": {"bits": [2]},
                        "y": {"bits": [3]},
                    },
                    "cells": {
                        "inv": {
                            "type": "INV",
                            "port_directions": {
                                "I": "input", "O": "output",
                            },
                            "connections": {"I": [2], "O": [3]},
                        }
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            report = run_phase1(
                yosys_json=Path("<yosys-stdout>"),
                platform_path=(
                    ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
                ),
                output_dir=output,
                top="top",
                indexed_staging_transform=normalize_xilinx_indexed_staging,
                yosys_stream=StringIO(json.dumps(value)),
            )
            ir = EmuIR.load(output / "design.emuir.sqlite").value
            large_json = [
                path
                for path in output.iterdir()
                if path.suffix == ".json" and path.stat().st_size > 10000
            ]
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["indexed_staging_transform"]["output_representation"],
            "indexed-cell-records",
        )
        self.assertEqual(ir["instances"][0]["type"], "LUT1")
        self.assertEqual(large_json, [])

    def test_pipeline_writes_valid_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            report = run_phase1(
                yosys_json=ROOT / "examples/yosys/counter.json",
                platform_path=(
                    ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
                ),
                output_dir=output,
                top="counter",
                clocks=["clk"],
            )
            self.assertEqual(report["status"], "pass")
            self.assertTrue(all(report["fits_on_fpga"].values()))
            self.assertTrue(report["fits_on_platform"])
            self.assertEqual(report["fit_scope"], "single_fpga")
            self.assertEqual(
                report["clock_topology"]["fabric_logic_clock_nets"], 0
            )
            emuir = output / "design.emuir.sqlite"
            self.assertTrue(emuir.is_file())
            self.assertEqual(EmuIR.load(emuir).value["design"]["name"], "counter")
            for filename in (
                "platform.normalized.json",
                "phase1_report.json",
            ):
                self.assertTrue((output / filename).is_file())
                with (output / filename).open("r", encoding="utf-8") as stream:
                    self.assertIsInstance(json.load(stream), dict)

    def test_aggregate_multi_fpga_capacity_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            platform = {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "two_tiny_fpgas",
                    "kind": "virtual",
                },
                "fpgas": [
                    {
                        "id": fpga_id,
                        "part": "test",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 3, "ff": 3},
                    }
                    for fpga_id in ("fpga0", "fpga1")
                ],
                "links": [
                    {
                        "id": "link",
                        "endpoints": ["fpga0", "fpga1"],
                        "direction": "full_duplex",
                        "mode": "abstract",
                        "data_lanes_per_direction": 1,
                        "fabric_clock_mhz": 1,
                        "latency_cycles": 0,
                    }
                ],
            }
            platform_path = root / "platform.json"
            platform_path.write_text(
                json.dumps(platform),
                encoding="utf-8",
            )
            report = run_phase1(
                yosys_json=ROOT / "examples/yosys/counter.json",
                platform_path=platform_path,
                output_dir=root / "phase1",
                top="counter",
                clocks=["clk"],
            )
            self.assertEqual(report["status"], "pass")
            self.assertFalse(any(report["fits_on_fpga"].values()))
            self.assertTrue(report["fits_on_platform"])
            self.assertEqual(report["fit_scope"], "aggregate_platform")

    def test_fabric_logic_clock_can_be_a_strict_phase1_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            yosys = json.loads(
                (ROOT / "examples/yosys/counter.json").read_text(
                    encoding="utf-8"
                )
            )
            counter = yosys["modules"]["counter"]
            counter["cells"]["q_reg[0]"]["connections"]["C"] = [8]
            yosys_path = root / "fabric-clock.json"
            yosys_path.write_text(json.dumps(yosys), encoding="utf-8")

            report = run_phase1(
                yosys_json=yosys_path,
                platform_path=(
                    ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
                ),
                output_dir=root / "phase1",
                top="counter",
                clocks=["clk"],
                require_no_fabric_clock=True,
            )
            self.assertEqual(report["status"], "clock_topology_error")
            topology = report["clock_topology"]
            self.assertEqual(topology["fabric_logic_clock_nets"], 1)
            self.assertEqual(topology["fabric_logic_clocked_ffs"], 1)
            self.assertEqual(topology["maximum_fabric_clock_fanout"], 1)

    def test_vtr_dff_clock_topology_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            yosys = json.loads(
                (ROOT / "examples/yosys/counter.json").read_text(
                    encoding="utf-8"
                )
            )
            counter = yosys["modules"]["counter"]
            for cell in counter["cells"].values():
                if cell["type"].startswith("FD"):
                    cell["type"] = "$_DFF_P_"
                    cell["connections"] = {
                        "C": cell["connections"]["C"],
                        "D": cell["connections"]["D"],
                        "Q": cell["connections"]["Q"],
                    }
                    cell["port_directions"] = {
                        "C": "input",
                        "D": "input",
                        "Q": "output",
                    }
            yosys_path = root / "vtr-dff.json"
            yosys_path.write_text(json.dumps(yosys), encoding="utf-8")

            report = run_phase1(
                yosys_json=yosys_path,
                platform_path=(
                    ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
                ),
                output_dir=root / "phase1",
                top="counter",
                clocks=["clk"],
                require_no_fabric_clock=True,
            )
            self.assertEqual(report["status"], "pass")
            topology = report["clock_topology"]
            self.assertEqual(topology["ff_clock_nets"], 1)
            self.assertEqual(topology["fabric_logic_clock_nets"], 0)

            # The same VTR DFF clocked by a generic Yosys LUT is unsafe.
            counter["cells"]["fabric_clock_lut"] = {
                "hide_name": 0,
                "type": "$lut",
                "parameters": {"LUT": "10", "WIDTH": "1"},
                "port_directions": {"A": "input", "Y": "output"},
                "connections": {"A": [2], "Y": [8]},
            }
            first_dff = next(
                cell
                for cell in counter["cells"].values()
                if cell["type"].startswith("$_DFF_")
            )
            first_dff["connections"]["C"] = [8]
            yosys_path.write_text(json.dumps(yosys), encoding="utf-8")
            unsafe = run_phase1(
                yosys_json=yosys_path,
                platform_path=(
                    ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
                ),
                output_dir=root / "unsafe-phase1",
                top="counter",
                clocks=["clk"],
                require_no_fabric_clock=True,
            )
            self.assertEqual(unsafe["status"], "clock_topology_error")
            self.assertEqual(
                unsafe["clock_topology"]["fabric_logic_clock_nets"], 1
            )


if __name__ == "__main__":
    unittest.main()
