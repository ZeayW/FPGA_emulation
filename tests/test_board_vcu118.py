import copy
import unittest

from emuflow.board_vcu118 import (
    VCU118_PART, build_vcu118_qsfp1_overlay,
    vcu118_board_services, vcu118_qsfp1_endpoint,
    build_vcu118_pair_boarddb,
)
from emuflow.errors import ValidationError
from emuflow.board_support import validate_board_support_overlay
from emuflow.serial_wrapper import serial_board_service_xdc
from emuflow.platform import Platform


class Vcu118BindingTest(unittest.TestCase):
    def test_fixed_pair_and_explicit_unmeasured_latency(self):
        document = build_vcu118_pair_boarddb(latency_cycles=12)
        platform = Platform.from_dict(document)
        self.assertEqual(len(platform.fpgas), 2)
        self.assertEqual(len(platform.links), 1)
        self.assertEqual(platform.links[0].transport_bits_per_cycle_per_direction, 256)
        self.assertEqual(document["fpgas"][0]["capacity"]["lut"], 1_182_240)
        self.assertEqual(document["fpgas"][0]["utilization_limit"], 0.75)
        self.assertNotIn("io", document["fpgas"][0]["capacity"])
        self.assertEqual(document["provenance"]["transport"]["latency_basis"],
                         "caller_supplied_not_a_hardware_bound")
        self.assertEqual(document["provenance"]["cable"]["model"], "QSFP-H40G-CU1M-BB")
        document["fpgas"][0]["capacity"]["lut"] = 1
        self.assertEqual(document["fpgas"][1]["capacity"]["lut"], 1_182_240)

    def test_candidate_never_guesses_a_latency(self):
        for value in (0, -1, True, 1.5, "12", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                build_vcu118_pair_boarddb(latency_cycles=value)
        with self.assertRaises(TypeError):
            build_vcu118_pair_boarddb()

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
        self.assertEqual(len(overlay["static_outputs"]), 6)
        self.assertEqual({item["package_pin"]: item["value"]
                          for item in overlay["static_outputs"]},
                         {"AM21": 0, "BA22": 1, "AN21": 0})
        xdc = serial_board_service_xdc({"board_services": {
            "reference_clocks": [], "resets": [],
            "static_outputs": [item for item in overlay["static_outputs"] if item["fpga"] == "a"]}})
        self.assertIn("PACKAGE_PIN BA22 [get_ports {board_static_a_qsfp1_resetl}]", xdc)
        self.assertIn("IOSTANDARD LVCMOS18", xdc)

    def test_static_control_rejects_pin_collision_and_invalid_values(self):
        platform = Platform.from_dict(self.document)
        for field, value in (("package_pin", "L19"), ("package_pin", "V7"),
                             ("value", True), ("value", 2), ("id", "a;exit")):
            overlay = build_vcu118_qsfp1_overlay(platform, self.sites)
            overlay["static_outputs"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                validate_board_support_overlay(overlay, platform)

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
