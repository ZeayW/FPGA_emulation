import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_rwroute import export_rwroute_input, validate_xilinx_route_db


SHA = "0" * 64


class XilinxRWRouteTest(unittest.TestCase):
    def _route(self):
        return {
            "schema": "emuflow.xilinx-route-db/v1", "status": "candidate",
            "provider": "rapidwright-rwroute-2026.1.0", "part": "xcvu19p-test",
            "source": {
                "mapped_sha256": SHA, "packed_sha256": SHA,
                "placement_sha256": SHA, "rwroute_input_sha256": SHA,
            },
            "cells": 3, "excluded_nets": [], "summary": {},
            "nets": [{
                "net": "n1", "kind": "signal", "has_gap": False,
                "pins": [
                    {"site": "S0", "pin": "O", "is_output": True, "node": "A"},
                    {"site": "S1", "pin": "I", "is_output": False, "node": "B"},
                    {"site": "S2", "pin": "I", "is_output": False, "node": "C"},
                ],
                "pips": [
                    {"tile": "T0", "start_wire": "W0", "end_wire": "W1", "start_node": "A", "end_node": "X"},
                    {"tile": "T1", "start_wire": "W2", "end_wire": "W3", "start_node": "X", "end_node": "B"},
                    {"tile": "T2", "start_wire": "W4", "end_wire": "W5", "start_node": "X", "end_node": "C"},
                ],
            }],
        }

    def test_checker_accepts_connected_tree_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(self._route()), encoding="utf-8")
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["nets"], 1)
            broken = self._route()
            broken["nets"][0]["pips"].pop()
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_xilinx_route_db(path)

    def test_checker_rejects_cross_net_resource_conflict(self):
        value = self._route()
        other = copy.deepcopy(value["nets"][0])
        other["net"] = "n2"
        value["nets"].append(other)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_xilinx_route_db(path)

    def test_exporter_excludes_intra_site_net(self):
        mapped = {
            "modules": {"top": {"cells": {
                "a": {"type": "LUT1", "port_directions": {"O": "output"}, "connections": {"O": [1]}},
                "b": {"type": "FDRE", "port_directions": {"D": "input"}, "connections": {"D": [1]}},
            }}}
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": [
                {"instance": "a", "cell_type": "LUT1", "bel": "A6LUT"},
                {"instance": "b", "cell_type": "FDRE", "bel": "AFF"},
            ]}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "a", "bel": "A6LUT"},
                {"instance": "b", "bel": "AFF"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("mapped.json", "packed.json", "placement.json")]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            report = export_rwroute_input(*paths, output)
            text = output.read_text(encoding="utf-8")
        self.assertEqual(report["routable_nets"], 0)
        self.assertIn("EXCLUDED\tn1\tintra_site", text)

    def test_exporter_physically_expands_lut6_2_for_rapidwright(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src_di": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "src_s": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [2]},
                },
                "adapter": {
                    "type": "LUT6_2",
                    "port_directions": {
                        "I0": "input", "I1": "input", "I2": "input",
                        "I3": "input", "I4": "input", "I5": "input",
                        "O5": "output", "O6": "output",
                    },
                    "connections": {
                        "I0": [1], "I1": [2], "I2": ["0"],
                        "I3": ["0"], "I4": ["0"], "I5": ["1"],
                        "O5": [3], "O6": [4],
                    },
                },
                "sink_di": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [3]},
                },
                "sink_s": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [4]},
                },
            }}}
        }
        assignments = [
            {"instance": "src_di", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "src_s", "cell_type": "LUT1", "bel": "B6LUT"},
            {"instance": "adapter", "cell_type": "LUT6_2", "bel": "C6LUT"},
            {"instance": "sink_di", "cell_type": "FDRE", "bel": "AFF"},
            {"instance": "sink_s", "cell_type": "FDRE", "bel": "BFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        sites = {
            "src_di": "SLICE_X0Y0", "src_s": "SLICE_X0Y1",
            "adapter": "SLICE_X0Y2", "sink_di": "SLICE_X0Y3",
            "sink_s": "SLICE_X0Y4",
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{
                "site": "SLICE_X0Y2",
                "assignments": [
                    {"instance": item["instance"], "bel": item["bel"],
                     "site": sites[item["instance"]]}
                    for item in assignments
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            report = export_rwroute_input(*paths, output)
            rows = [line.split("\t") for line in output.read_text().splitlines()]
        cell_rows = {row[2]: row for row in rows if row[0] == "CELL"}
        o5 = cell_rows["adapter$physical_o5"]
        o6 = cell_rows["adapter$physical_o6"]
        self.assertEqual((o5[3], o5[5]), ("LUT5", "C5LUT"))
        self.assertEqual((o6[3], o6[5]), ("LUT6", "C6LUT"))
        pin_rows = {(row[1], row[2], row[3], row[4]) for row in rows if row[0] == "PIN"}
        self.assertIn(("n1", o5[1], "I0", "sink"), pin_rows)
        self.assertIn(("n2", o6[1], "I1", "sink"), pin_rows)
        self.assertIn(("n3", o5[1], "O", "driver"), pin_rows)
        self.assertIn(("n4", o6[1], "O", "driver"), pin_rows)
        self.assertEqual(report["logical_cells"], 5)
        self.assertEqual(report["cells"], 6)
        self.assertEqual(report["expanded_lut6_2_cells"], 1)


if __name__ == "__main__":
    unittest.main()
