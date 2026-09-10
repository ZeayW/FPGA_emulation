import unittest

from emuflow.board_ulx3s import (
    ulx3s_pair_profile, ulx3s_endpoint_lpf,
    require_ulx3s_full_flow_qualification,
)
from emuflow.errors import ValidationError


class ULX3SBoardTests(unittest.TestCase):
    def test_fixed_open_platform(self):
        p = ulx3s_pair_profile()
        self.assertEqual(len(p["boards"]), 2)
        self.assertFalse(p["tools"]["commercial_tools_required"])
        self.assertEqual(p["tools"]["place_route"], "nextpnr-ecp5")
        for b in p["boards"]:
            self.assertEqual(b["utilization_limit"], .75)
            self.assertEqual(len({v["site"] for v in b["pins"].values()}), 4)

    def test_harness_direction(self):
        p = ulx3s_pair_profile()
        pins = {f'{b["id"]}.{k}': v for b in p["boards"] for k, v in b["pins"].items()}
        for tx, rx in p["harness"]["signals"]:
            self.assertEqual(pins[tx]["direction"], "output")
            self.assertEqual(pins[rx]["direction"], "input")
        self.assertFalse(p["harness"]["power_rails_connected"])
        self.assertTrue(p["harness"]["common_ground_required"])

    def test_no_invented_clock_or_latency(self):
        p = ulx3s_pair_profile()
        self.assertEqual(p["clocks"]["relationship"], "asynchronous")
        self.assertIsNone(p["clocks"]["external_delay_bound_ns"])
        self.assertFalse(any(p["qualification"].values()))
        with self.assertRaises(ValidationError):
            require_ulx3s_full_flow_qualification()

    def test_constraints(self):
        lpf = ulx3s_endpoint_lpf("board0")
        self.assertIn('"clk_25mhz" SITE "G2"', lpf)
        self.assertIn('"link_tx" SITE "B11"', lpf)
        self.assertIn('"link_rx" SITE "C11"', lpf)
        self.assertNotIn("BLOCK ASYNCPATHS", lpf)
        self.assertNotIn("BLOCK RESETPATHS", lpf)
        with self.assertRaises(ValidationError):
            ulx3s_endpoint_lpf("board2")

    def test_returned_data_is_not_shared(self):
        a = ulx3s_pair_profile()
        a["boards"][0]["pins"]["link_tx"]["site"] = "INVALID"
        self.assertEqual(a["boards"][1]["pins"]["link_tx"]["site"], "B11")
        self.assertEqual(ulx3s_pair_profile()["boards"][0]["pins"]["link_tx"]["site"], "B11")
