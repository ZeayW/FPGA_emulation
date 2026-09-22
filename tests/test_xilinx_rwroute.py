import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_rwroute import (
    _validate_rapidwright_device_data,
    export_rwroute_input,
    validate_xilinx_route_db,
)
from emuflow.xilinx_timing import (
    build_xilinx_routed_timing,
    validate_xilinx_routed_timing,
)


SHA = "0" * 64


class XilinxRWRouteTest(unittest.TestCase):
    def test_device_data_provider_fails_closed_on_unpinned_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data/parts.db"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not-the-pinned-provider")
            with self.assertRaisesRegex(ValidationError, "pinned XCVU19P"):
                _validate_rapidwright_device_data(root)

    def _route(self):
        return {
            "schema": "emuflow.xilinx-route-db/v1", "status": "candidate",
            "provider": "rapidwright-rwroute-2026.1.0", "part": "xcvu19p-test",
            "source": {
                "mapped_sha256": SHA, "packed_sha256": SHA,
                "placement_sha256": SHA, "rwroute_input_sha256": SHA,
            },
            "cells": 3,
            "materialization": {
                "route_cells": 3,
                "physical_cells": 3,
                "transformed_dsp48e2_cells": 0,
                "router": (
                    "CUFR-HUS-non-timing-driven-uturn-enabled-adaptive-bbox"
                ),
            },
            "excluded_nets": [],
            "summary": {
                "candidate_nets": 1, "certificate_nets": 1,
                "static_nets": 0, "static_sinks": 0,
                "nets_with_pips": 1, "pips": 3, "excluded_nets": 0,
                "boundary_clock_nets": 0,
            },
            "timing": {
                "provider": "rapidwright-lightweight",
                "family": "UltraScalePlus", "units": "ps",
                "setup_route_delays": "available",
                "hold_analysis": "unavailable",
                "hard_block_clock_timing": "unqualified",
                "source_revision": "127f55cd704c277372697e699f1559e1cdc91f34",
                "source_data_sha256": {
                    "intersite_delay_terms.txt": "3b122837c4a1b5f3c212fc6353bee3a0854a204f2f69e2bc1cac4a2a1f9d7333",
                    "intrasite_delay_terms.txt": "ff08ce9041da649f2bf886d900f033dd23de8ad54eb4db55d37ce032dc0ec0a0",
                },
                "device_data_md5": {
                    "data/parts.db": "58dd6f20c37798322b6904a8a786a3de",
                    "data/devices/virtexuplus/xcvu19p_db.dat": "5ad01490fe442f360aa67d7dfe0fa1c3",
                },
                "logic_coefficients_ps": {
                    "ff_clock_to_q": 25.0, "carry_co": 10.0,
                    "lut_a1": 70.0, "lut_a2": 65.0,
                    "lut_a3": 60.0, "lut_a4": 55.0,
                    "lut_a5": 50.0, "lut_a6": 45.0,
                },
                "routed_endpoints": 2,
                "maximum_route_delay_ps": 27.5,
            },
            "nets": [{
                "net": "n1", "kind": "signal",
                "qualification": "ordinary-fabric-signal", "has_gap": False,
                "source_present": True, "sink_count": 2,
                "pins": [
                    {"site": "S0", "pin": "O", "is_output": True, "node": "A"},
                    {"site": "S1", "pin": "I", "is_output": False, "node": "B", "route_delay_ps": 12.0},
                    {"site": "S2", "pin": "I", "is_output": False, "node": "C", "route_delay_ps": 27.5},
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
            self.assertEqual(report["timed_endpoints"], 2)
            self.assertEqual(report["maximum_route_delay_ps"], 27.5)
            broken = self._route()
            broken["nets"][0]["pips"].pop()
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_xilinx_route_db(path)

    def test_checker_accepts_equivalent_alternate_physical_source(self):
        value = self._route()
        net = value["nets"][0]
        net["alternate_sources"] = [{
            "site": "S0", "pin": "OMUX", "is_output": True, "node": "A2",
        }]
        net["pips"][1]["start_node"] = "A2"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_xilinx_route_db(path)["nets"], 1)
            broken = copy.deepcopy(value)
            broken["nets"][0]["alternate_sources"][0]["is_output"] = False
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "alternate_sources"):
                validate_xilinx_route_db(path)

    def test_checker_accepts_device_tied_static_forest(self):
        value = self._route()
        value["nets"].append({
            "net": "GLOBAL_LOGIC1", "kind": "static_vcc",
            "qualification": "device-tied-static", "has_gap": False,
            "source_present": False, "sink_count": 2,
            "roots": ["VCC0", "VCC1"],
            "pins": [
                {"site": "S3", "pin": "A1", "is_output": False, "node": "D"},
                {"site": "S4", "pin": "CE", "is_output": False, "node": "E"},
            ],
            "pips": [
                {"tile": "T3", "start_wire": "W6", "end_wire": "W7", "start_node": "VCC0", "end_node": "D"},
                {"tile": "T4", "start_wire": "W8", "end_wire": "W9", "start_node": "VCC1", "end_node": "E"},
            ],
        })
        value["summary"].update({
            "certificate_nets": 2, "static_nets": 1, "static_sinks": 2,
            "nets_with_pips": 2, "pips": 5,
        })
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["static_nets"], 1)
            self.assertEqual(report["static_sinks"], 2)
            broken = copy.deepcopy(value)
            broken["nets"][1]["roots"] = ["NOT_CONNECTED"]
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "source-connected"):
                validate_xilinx_route_db(path)

    def test_checker_requires_explicit_clock_qualification(self):
        value = self._route()
        value["nets"][0]["kind"] = "clock"
        value["nets"][0]["qualification"] = "fabric-routed-clock"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_xilinx_route_db(path)["clock_nets"], 1)
            value["nets"][0]["qualification"] = "global-clock"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "clock qualification"):
                validate_xilinx_route_db(path)

    def test_checker_requires_explicit_boundary_clock_qualification(self):
        value = self._route()
        value["excluded_nets"] = [{
            "net": "nclk", "reason": "boundary_clock",
            "qualification": "ideal-boundary-clock",
        }]
        value["summary"]["excluded_nets"] = 1
        value["summary"]["boundary_clock_nets"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["boundary_clock_nets"], 1)
            value["excluded_nets"][0]["qualification"] = "unknown"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "clock qualification"):
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

    def test_checker_rejects_missing_or_tampered_timing(self):
        value = self._route()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            del value["nets"][0]["pins"][1]["route_delay_ps"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "route delay"):
                validate_xilinx_route_db(path)
            value = self._route()
            value["timing"]["maximum_route_delay_ps"] = 99.0
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "maximum route delay"):
                validate_xilinx_route_db(path)

    def test_routed_timing_binds_site_pins_to_logical_endpoints(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "sink_a": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
                "sink_b": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
            }}}
        }
        packed = {"schema": "emuflow.packed-site-netlist/v1", "top": "top"}
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [
                {"site": "S0", "assignments": [{"instance": "src", "bel": "A6LUT"}]},
                {"site": "S1", "assignments": [{"instance": "sink_a", "bel": "AFF"}]},
                {"site": "S2", "assignments": [{"instance": "sink_b", "bel": "AFF"}]},
            ],
        }
        route = self._route()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped_path, packed_path, placement_path = (
                root / "mapped.json", root / "packed.json", root / "placement.json"
            )
            for path, value in (
                (mapped_path, mapped), (packed_path, packed),
                (placement_path, placement),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
            for key, path in (
                ("mapped_sha256", mapped_path), ("packed_sha256", packed_path),
                ("placement_sha256", placement_path),
            ):
                route["source"][key] = hashlib.sha256(path.read_bytes()).hexdigest()
            route_path = root / "route.json"
            route_path.write_text(json.dumps(route), encoding="utf-8")
            output = root / "routed-timing.json"
            report = build_xilinx_routed_timing(
                mapped_path, packed_path, placement_path, route_path, output
            )
            checked = validate_xilinx_routed_timing(
                output, mapped_path=mapped_path, packed_path=packed_path,
                placement_path=placement_path, route_path=route_path,
            )
            value = json.loads(output.read_text())
        self.assertEqual(report["logical_endpoints"], 2)
        self.assertEqual(checked["physical_route_sinks"], 2)
        self.assertEqual(
            [item["route_delay_ns"] for item in value["endpoints"]],
            [0.012, 0.0275],
        )

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

    def test_exporter_qualifies_driverless_boundary_clock(self):
        mapped = {
            "modules": {"top": {"cells": {
                "ff": {
                    "type": "FDRE", "port_directions": {"C": "input"},
                    "connections": {"C": [1]},
                },
            }}}
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": [
                {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
            ]}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "ff", "bel": "AFF"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("mapped.json", "packed.json", "placement.json")]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            export_rwroute_input(*paths, output)
            text = output.read_text(encoding="utf-8")
        self.assertIn(
            "EXCLUDED\tn1\tboundary_clock\tideal-boundary-clock", text
        )

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

    def test_exporter_accounts_for_rapidwright_dsp48e2_transform(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "multiply": {
                    "type": "DSP48E2",
                    "port_directions": {"A": "input", "P": "output"},
                    "connections": {
                        "A": [1] + ["0"] * 29,
                        "P": [2] + ["0"] * 47,
                    },
                },
                "sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            }}}
        }
        assignments = [
            {"instance": "src", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "multiply", "cell_type": "DSP48E2", "bel": "DSP_ALU"},
            {"instance": "sink", "cell_type": "FDRE", "bel": "AFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "src", "bel": "A6LUT", "site": "SLICE_X0Y0"},
                {"instance": "multiply", "bel": "DSP_ALU", "site": "DSP48E2_X0Y0"},
                {"instance": "sink", "bel": "AFF", "site": "SLICE_X0Y1"},
            ]}],
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
        dsp_rows = [row for row in rows if row[0] == "CELL" and row[3] == "DSP48E2"]
        self.assertEqual(len(dsp_rows), 1)
        self.assertEqual(report["cells"], 3)
        self.assertEqual(report["logical_cells"], 3)
        self.assertEqual(report["transformed_dsp48e2_cells"], 1)
        self.assertEqual(report["physical_cells"], 10)

    def test_exporter_seals_ramb_mode_parameters_without_init_payloads(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "memory": {
                    "type": "RAMB36E2",
                    "parameters": {
                        "READ_WIDTH_A": "00001001",
                        "WRITE_WIDTH_B": "01001000",
                        "DOA_REG": "0",
                        "WRITE_MODE_B": "READ_FIRST",
                        "INIT_00": "1" * 256,
                    },
                    "port_directions": {"WEBWE": "input", "DOUTADOUT": "output"},
                    "connections": {
                        "WEBWE": [1] + ["0"] * 7,
                        "DOUTADOUT": [2] + ["0"] * 31,
                    },
                },
                "sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            }}}
        }
        assignments = [
            {"instance": "src", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "memory", "cell_type": "RAMB36E2", "bel": "RAMB36E2"},
            {"instance": "sink", "cell_type": "FDRE", "bel": "AFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "src", "bel": "A6LUT", "site": "SLICE_X0Y0"},
                {"instance": "memory", "bel": "RAMB36E2", "site": "RAMB36_X0Y0"},
                {"instance": "sink", "bel": "AFF", "site": "SLICE_X0Y1"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            export_rwroute_input(*paths, output)
            rows = [line.split("\t") for line in output.read_text().splitlines()]
        memory_safe = next(
            row[1] for row in rows if row[0] == "CELL" and row[2] == "memory"
        )
        parameters = {
            (row[2], row[3]) for row in rows
            if row[0] == "PARAM" and row[1] == memory_safe
        }
        self.assertIn(("READ_WIDTH_A", "9"), parameters)
        self.assertIn(("WRITE_WIDTH_B", "72"), parameters)
        self.assertIn(("DOA_REG", "0"), parameters)
        self.assertIn(("WRITE_MODE_B", "READ_FIRST"), parameters)
        self.assertFalse(any(row[0] == "PARAM" and row[2].startswith("INIT") for row in rows))


if __name__ == "__main__":
    unittest.main()
