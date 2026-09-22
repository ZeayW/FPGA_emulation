import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import test_xilinx_rwroute as fixtures
from emuflow.errors import ValidationError
from emuflow.xilinx_timing import build_xilinx_routed_timing


class StaticTimingTest(unittest.TestCase):
    def route(self, kind="static_vcc"):
        value = fixtures.XilinxRWRouteTest()._route()
        value["nets"].append({
            "net": "GLOBAL_LOGIC1" if kind == "static_vcc" else "GLOBAL_LOGIC0",
            "kind": kind, "qualification": "device-tied-static", "has_gap": False,
            "source_present": False, "sink_count": 1, "roots": ["CONST_ROOT"],
            "pins": [{"site": "S1", "pin": "CE", "is_output": False, "node": "CONST_SINK"}],
            "pips": [{"tile": "TC", "start_wire": "WC0", "end_wire": "WC1",
                      "start_node": "CONST_ROOT", "end_node": "CONST_SINK"}],
        })
        value["summary"].update(certificate_nets=2, static_nets=1,
                                static_sinks=1, nets_with_pips=2, pips=4)
        return value

    def build(self, route):
        cells = {
            "src": {"type": "LUT1", "port_directions": {"O": "output"}, "connections": {"O": [1]}},
            "sink_a": {"type": "FDRE", "port_directions": {"D": "input", "CE": "input"}, "connections": {"D": [1], "CE": ["1"]}},
            "sink_b": {"type": "FDRE", "port_directions": {"D": "input"}, "connections": {"D": [1]}},
        }
        if len(route["nets"]) > 1 and route["nets"][1]["kind"] == "static_gnd":
            cells["sink_a"]["connections"]["CE"] = ["0"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = {
                "mapped": {"modules": {"top": {"cells": cells}}},
                "packed": {"schema": "emuflow.packed-site-netlist/v1", "top": "top"},
                "placement": {"schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
                    "clusters": [{"site": "S" + str(i), "assignments": [{"instance": n, "bel": "A6LUT" if i == 0 else "AFF"}]}
                                 for i, n in enumerate(cells)]},
            }
            for name, value in inputs.items():
                path = root / (name + ".json")
                path.write_text(json.dumps(value))
                route["source"][name + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            route_path = root / "route.json"
            route_path.write_text(json.dumps(route))
            output = root / "timing.json"
            report = build_xilinx_routed_timing(root/"mapped.json", root/"packed.json", root/"placement.json", route_path, output)
            return report, json.loads(output.read_text())

    def test_constants_preserve_signal_endpoint_delays(self):
        baseline = fixtures.XilinxRWRouteTest()._route()
        _, before = self.build(baseline)
        for kind in ("static_vcc", "static_gnd"):
            with self.subTest(kind=kind):
                report, after = self.build(self.route(kind))
                self.assertEqual(after["endpoints"], before["endpoints"])
                self.assertEqual(report["logical_endpoints"], 2)
                self.assertEqual(report["physical_route_sinks"], 2)

    def test_disconnected_constant_is_rejected(self):
        route = self.route()
        route["nets"][1]["roots"] = ["DISCONNECTED"]
        with self.assertRaisesRegex(ValidationError, "source-connected"):
            self.build(route)

    def test_constant_resource_conflict_is_rejected(self):
        route = self.route()
        route["nets"][1]["pips"][0].update(tile="T0", start_wire="W0", end_wire="W1")
        with self.assertRaisesRegex(ValidationError, "conflict"):
            self.build(route)

    def test_malformed_signal_is_not_ignored(self):
        route = self.route()
        route["nets"][0]["net"] = "unexpected_signal"
        with self.assertRaisesRegex(ValidationError, "mapped bit"):
            self.build(route)

    def test_mapped_bit_cannot_be_hidden_as_constant(self):
        route = self.route()
        route["nets"][1]["net"] = "n999"
        with self.assertRaisesRegex(ValidationError, "static route identity"):
            self.build(route)


if __name__ == "__main__":
    unittest.main()

