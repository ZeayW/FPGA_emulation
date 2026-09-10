import unittest
from emuflow.ecp5_qualification import ECP5_85F_CAPACITY, qualify_ecp5_endpoint_report, qualify_snapshot_clock_coverage
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

    def clock_model(self):
        d=report(); d["utilization"]["TRELLIS_FF"]["used"]=1
        m={"ports":{"clk_25mhz":{"direction":"input","bits":[1]}},
           "netnames":{"clk":{"bits":[3]}},"cells":{
               "pad":{"type":"TRELLIS_IO","parameters":{"DIR":"INPUT"},"connections":{"B":[1],"O":[2]}},
               "buf":{"type":"DCCA","connections":{"CLKI":[2],"CLKO":[3],"CE":[]}},
               "state":{"type":"TRELLIS_FF","parameters":{"CLKMUX":"CLK"},"connections":{"CLK":[3]}}}}
        return d,{"modules":{"top":m}}

    def test_actual_ff_clock_coverage_is_not_cdc_or_global_timing(self):
        d,r=self.clock_model(); result=qualify_snapshot_clock_coverage(r,d)
        self.assertEqual(result["ff_count"],1)
        self.assertFalse(result["cdc_qualified"])
        self.assertFalse(result["timing_path_coverage_qualified"])

    def test_missing_or_wrong_clock_connectivity_fails(self):
        for kind in ("other_clock","inverted","gated","wrong_pad","count","hardblock"):
            d,r=self.clock_model(); m=r["modules"]["top"]
            if kind=="other_clock": m["cells"]["state"]["connections"]["CLK"]=[4]
            if kind=="inverted": m["cells"]["state"]["parameters"]["CLKMUX"]="INV"
            if kind=="gated": m["cells"]["buf"]["connections"]["CE"]=[7]
            if kind=="wrong_pad": m["cells"]["pad"]["connections"]["B"]=[7]
            if kind=="count": d["utilization"]["TRELLIS_FF"]["used"]=2
            if kind=="hardblock": m["cells"]["ram"]={"type":"DP16KD"}
            with self.subTest(kind=kind),self.assertRaises(ValidationError): qualify_snapshot_clock_coverage(r,d)
