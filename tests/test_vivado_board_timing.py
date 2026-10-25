import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.vivado_board_timing import (
    validate_vivado_board_timing_report,
)


ROOT = Path(__file__).resolve().parents[1]


class VivadoBoardTimingTest(unittest.TestCase):
    def test_hierarchical_exporters_preserve_partition_board_and_fixed_pad_modes(self):
        boundary = (ROOT / "scripts/vivado/export_boundary_timing.tcl").read_text(
            encoding="utf-8"
        )
        logic = (
            ROOT / "scripts/vivado/export_logic_segment_timing.tcl"
        ).read_text(encoding="utf-8")
        self.assertIn("$argc < 3 || $argc > 5", boundary)
        self.assertIn("$argc < 3 || $argc > 5", logic)
        for script in (boundary, logic):
            self.assertIn("hierarchy_prefix", script)
            self.assertIn("emuflow_load_fixed_io_map", script)
        self.assertIn("get_pins -quiet -hier [list $port_bit]", boundary)
        self.assertIn(
            "emuflow_resolve_object $start_kind $start_name $hierarchy_prefix",
            logic,
        )
        self.assertIn(
            "-through $start_object -through $end_object",
            logic,
        )

    def test_report_keeps_board_link_latency_outside_signoff(self):
        report = {
            "schema": "emuflow.vivado-board-timing/v1",
            "status": "pass",
            "qualification": "routed-board-stage-timing-plus-link-model-only",
            "design": "counter",
            "platform": "board",
            "fpgas": [
                {
                    "fpga": "fpga0",
                    "status": "pass",
                    "boundary_endpoints": 4,
                    "logic_segments": 2,
                }
            ],
            "board_link_timing": {
                "status": "modeled-not-measured",
                "final_system_signoff": False,
            },
            "system_timing": {
                "status": "pass",
                "timing_paths": 3,
                "runtime_wns_ns": 1.25,
            },
        }
        summary = validate_vivado_board_timing_report(report)
        self.assertEqual(summary["boundary_endpoints"], 4)
        self.assertFalse(summary["final_system_signoff"])
        report["qualification"] = (
            "routed-board-maxima-plus-interface-measurements-"
            "link-model-only"
        )
        self.assertEqual(
            validate_vivado_board_timing_report(report)["fpgas"], 1
        )
        report["board_link_timing"]["final_system_signoff"] = True
        with self.assertRaisesRegex(ValidationError, "board link"):
            validate_vivado_board_timing_report(report)


if __name__ == "__main__":
    unittest.main()
