import copy
import unittest

from emuflow.board_vcu118 import (
    VCU118_PART, build_vcu118_qsfp1_overlay,
    vcu118_board_services, vcu118_qsfp1_endpoint,
)
from emuflow.errors import ValidationError
from emuflow.platform import Platform


class Vcu118BindingTest(unittest.TestCase):
    def setUp(self):
        # Connectivity/capacity here is a unit fixture, not a hardware claim.
        self.document = {
            "schema": "emuflow.boarddb/v1",
            "platform": {"name": "vcu118_unit_fixture", "kind": "hardware"},
            "fpgas": [{"id": fpga, "part": VCU118_PART,
                        "utilization_limit": 0.75, "capacity": {"lut": 100}}
                       for fpga in ("a", "b")],
            "links": [{"id": "test_link", "endpoints": ["a", "b"],
                       "direction": "full_duplex", "capacity_sharing": "per_direction",
                       "mode": "serial", "data_lanes_per_direction": 4,
                       "payload_bits_per_lane_per_cycle": 64,
                       "fabric_clock_mhz": 50, "latency_cycles": 4,
                       "endpoint_bindings": [vcu118_qsfp1_endpoint(fpga)
                                             for fpga in ("a", "b")]}],
            "board_services": vcu118_board_services(),
        }
        # Deliberately fake sites: unit tests never assert vendor qualification.
        self.sites = {(fpga, lane): f"GTYE4_CHANNEL_X1Y{48 + lane}"
                      for fpga in ("a", "b") for lane in range(4)}

    def test_exact_board_pins_and_active_high_reset(self):
        overlay = build_vcu118_qsfp1_overlay(Platform.from_dict(self.document), self.sites)
        self.assertEqual(len(overlay["transceiver_sites"]), 8)
        self.assertEqual(overlay["reference_clocks"][0]["package_pins"],
                         {"p": "W9", "n": "W8"})
        self.assertEqual(overlay["resets"][0]["polarity"], "active_high")
        self.assertEqual(overlay["resets"][0]["package_pin"], "L19")

    def test_wrong_part_or_board_pin_rejected(self):
        for wrong_part in (True, False):
            document = copy.deepcopy(self.document)
            if wrong_part:
                document["fpgas"][0]["part"] = "xcvu13p-fhga2104-1-e"
            else:
                document["links"][0]["endpoint_bindings"][0]["lanes"][0][
                    "tx_package_pins"]["p"] = "A1"
            with self.assertRaises(ValidationError):
                build_vcu118_qsfp1_overlay(Platform.from_dict(document), self.sites)

    def test_missing_or_extraneous_sites_rejected(self):
        for extra in (True, False):
            sites = dict(self.sites)
            if extra:
                sites[("c", 0)] = "GTYE4_CHANNEL_X1Y48"
            else:
                del sites[("a", 0)]
            with self.assertRaises(ValidationError):
                build_vcu118_qsfp1_overlay(Platform.from_dict(self.document), sites)

    def test_endpoint_returns_fresh_data(self):
        endpoint = vcu118_qsfp1_endpoint("a")
        endpoint["lanes"][0]["tx_package_pins"]["p"] = "bad"
        self.assertEqual(vcu118_qsfp1_endpoint("a")["lanes"][0][
            "tx_package_pins"]["p"], "V7")
