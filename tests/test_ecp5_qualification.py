import unittest
from emuflow.ecp5_qualification import ECP5_85F_CAPACITY, qualify_ecp5_endpoint_report
from emuflow.errors import ValidationError


def report():
    return {"utilization": {k: {"available": v, "used": 0}
                            for k, v in ECP5_85F_CAPACITY.items()},
            "fmax": {"clk": {"achieved": 80.0, "constraint": 25.0}}}


class ECP5QualificationTests(unittest.TestCase):
    def test_scope_not_global(self):
        result = qualify_ecp5_endpoint_report(report())
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["global_timing_qualified"])
        self.assertFalse(result["external_timing_qualified"])

    def test_wrong_device_or_missing_resource(self):
        for name in ECP5_85F_CAPACITY:
            d = report(); del d["utilization"][name]
            with self.assertRaises(ValidationError): qualify_ecp5_endpoint_report(d)
            d = report(); d["utilization"][name]["available"] += 1
            with self.assertRaises(ValidationError): qualify_ecp5_endpoint_report(d)

    def test_limit_counts_transport_too(self):
        for name, capacity in ECP5_85F_CAPACITY.items():
            d = report(); d["utilization"][name]["used"] = int(capacity * .75)
            qualify_ecp5_endpoint_report(d)
            d["utilization"][name]["used"] += 1
            with self.assertRaises(ValidationError): qualify_ecp5_endpoint_report(d)

    def test_malformed_counts(self):
        for value in (-1, True, .1, "1", 90000):
            d = report(); d["utilization"]["TRELLIS_FF"]["used"] = value
            with self.assertRaises(ValidationError): qualify_ecp5_endpoint_report(d)

    def test_missing_and_failed_clocks(self):
        for clocks in ({}, None, {"a": {"achieved": 24, "constraint": 25}},
                       {"a": {"achieved": 80, "constraint": 1}},
                       {"a": {"achieved": float("nan"), "constraint": 25}}):
            d = report(); d["fmax"] = clocks
            with self.assertRaises(ValidationError): qualify_ecp5_endpoint_report(d)

    def test_all_reported_clocks_checked(self):
        d = report(); d["fmax"]["other"] = {"achieved": 10, "constraint": 25}
        with self.assertRaises(ValidationError): qualify_ecp5_endpoint_report(d)
