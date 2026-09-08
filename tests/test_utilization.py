import copy
import unittest
import tempfile
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.platform import FpgaNode, Platform
from emuflow.utilization import build_utilization_report, compare_loading, loading_policy, packed_logic_resources


class UtilizationTest(unittest.TestCase):
    def setUp(self):
        self.platform = Platform("test", "virtual", "", (
            FpgaNode("a", "device", 0.8, {"lut": 100, "ff": 200}),
            FpgaNode("b", "device", 0.8, {"lut": 300, "ff": 600}),
        ), ())
        self.phase3 = {"partitions": [
            {"fpga": "a", "resources": {"lut": 60, "ff": 20}},
            {"fpga": "b", "resources": {"lut": 180, "ff": 60}},
        ], "validation": {"requested_balance_percent": 10, "effective_balance_percent": 20,
                           "balance_auto_relaxed": True}}

    def test_weighted_resources_and_headroom(self):
        r = build_utilization_report(self.platform, self.phase3)
        self.assertEqual(r["phase3_dut"]["total"]["lut"]["utilization"], .6)
        self.assertEqual(r["phase3_dut"]["total"]["lut"]["effective_utilization"], .75)
        self.assertEqual(r["qualification"]["class"], "target")
        self.assertTrue(r["balance"]["balance_auto_relaxed"])
        self.assertIsNone(r["phase7_final"]["total"]["lut"]["used"])

    def test_unknown_not_zero(self):
        self.phase3["partitions"].pop()
        r = build_utilization_report(self.platform, self.phase3)
        self.assertEqual(r["qualification"]["class"], "unknown")
        self.assertFalse(r["qualification"]["load_floor_met"])

    def test_unused_fpga_in_denominator(self):
        self.phase3["partitions"][1]["resources"] = {}
        r = build_utilization_report(self.platform, self.phase3)
        self.assertEqual(r["phase3_dut"]["total"]["lut"]["utilization"], .15)
        self.assertEqual(r["qualification"]["class"], "low-load")
        self.assertTrue(r["qualification"]["functional_legality_unchanged"])

    def test_physical_requires_explicit_measurement(self):
        physical = {"fpgas": [{"fpga": "a", "physical_cells": 1000, "resources": {"lut": 70}},
                              {"fpga": "b", "resources": {"lut": 200}}]}
        r = build_utilization_report(self.platform, self.phase3, physical)
        self.assertEqual(r["phase7_final"]["total"]["lut"]["used"], 270)
        self.assertIsNone(r["phase7_final"]["total"]["ff"]["used"])

    def test_compare_capacity_balance_policy(self):
        r = build_utilization_report(self.platform, self.phase3)
        self.assertEqual(compare_loading([r, r])["status"], "pass")
        for key in ("fpgas", "requested_balance", "policy"):
            other = copy.deepcopy(r)
            other["comparison_contract"][key] = None
            with self.assertRaises(ValidationError):
                compare_loading([r, other])

    def test_invalid_inputs(self):
        for value in ({"minimum": float("nan")}, {"target_min": .9}, {"principal_resources": []}):
            with self.assertRaises(ValidationError):
                loading_policy(value)
        self.phase3["partitions"].append(self.phase3["partitions"][0])
        with self.assertRaises(ValidationError):
            build_utilization_report(self.platform, self.phase3)

    def test_packed_counts_ignore_open_and_unknown_hard_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            arch = Path(directory) / "arch.xml"
            net = Path(directory) / "design.net"
            arch.write_text('<architecture><pb_type name="lut" blif_model=".names"/>'
                            '<pb_type name="ff" blif_model=".latch"/></architecture>')
            net.write_text('<block name="top" instance="top[0]">'
                           '<block name="x" instance="lut[0]"/>'
                           '<block name="y" instance="ff[0]"/>'
                           '<block name="open" instance="lut[1]"/>'
                           '<block name="ram" instance="memory[0]"/></block>')
            self.assertEqual(packed_logic_resources(arch, net), {"lut": 1, "ff": 1})
