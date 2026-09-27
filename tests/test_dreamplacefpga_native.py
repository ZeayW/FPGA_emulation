import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.dreamplacefpga_interchange import (
    build_dreamplacefpga_placement_candidate,
)
from emuflow.dreamplacefpga_native import (
    DREAMPLACEFPGA_RAPIDWRIGHT_BOUNDARY_SCHEMA,
    build_dreamplacefpga_rapidwright_boundary,
)
from emuflow.xilinx_packing import pack_xilinx_sites


class DreamplaceFPGANativeBoundaryTest(unittest.TestCase):
    def test_native_candidate_stays_blocked_at_rapidwright_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "mapped.json"
            packed = root / "packed.json"
            architecture = root / "architecture.json"
            physical = root / "placed.phys"
            candidate = root / "placement-candidate.json"
            boundary_path = root / "rapidwright-boundary.json"
            mapped.write_text(json.dumps({
                "modules": {"top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "din": {"direction": "input", "bits": [2]},
                        "dout": {"direction": "output", "bits": [4]},
                    },
                    "cells": {
                        "lut": {
                            "type": "LUT6", "parameters": {}, "attributes": {},
                            "port_directions": {"I": "input", "O": "output"},
                            "connections": {"I": [2], "O": [3]},
                        },
                        "ff": {
                            "type": "FDRE", "parameters": {}, "attributes": {},
                            "port_directions": {"D": "input", "Q": "output"},
                            "connections": {"D": [3], "Q": [4]},
                        },
                    },
                }},
            }), encoding="utf-8")
            architecture.write_text(json.dumps({
                "schema": "emuflow.archdb/v1",
                "part": "xcvu-test",
                "source": {"format": "unit-test/v1"},
                "policy": {"name": "unit-test"},
                "site_templates": {"SLICEL": {
                    "bels": [
                        {"name": "A6LUT", "type": "LUT6", "z": 0,
                         "compatible_cells": ["LUT6"]},
                        {"name": "AFF", "type": "FDRE", "z": 1,
                         "compatible_cells": ["FDRE"]},
                    ],
                    "alternative_templates": [],
                }},
                "sites": [{
                    "name": "SLICE_X0Y0", "type": "SLICEL",
                    "template": "SLICEL", "x": 0, "y": 0,
                    "physical_region": {
                        "slr": "SLR0", "clock_region": "X0Y0",
                    },
                }],
            }), encoding="utf-8")
            physical.write_bytes(b"sealed-fixture-phys")
            pack_xilinx_sites(mapped, packed)
            decoded = {
                "part": "xcvu-test",
                "placements": [
                    {"instance": "lut", "cell_type": "LUT6",
                     "site": "SLICE_X0Y0", "bel": "A6LUT",
                     "site_fixed": False, "bel_fixed": False},
                    {"instance": "ff", "cell_type": "FDRE",
                     "site": "SLICE_X0Y0", "bel": "AFF",
                     "site_fixed": False, "bel_fixed": False},
                ],
            }
            with mock.patch(
                "emuflow.dreamplacefpga_interchange."
                "read_dreamplacefpga_physical_placements",
                return_value=decoded,
            ):
                build_dreamplacefpga_placement_candidate(
                    mapped,
                    architecture,
                    physical,
                    candidate,
                    packed_path=packed,
                    decoded=decoded,
                )
                boundary = build_dreamplacefpga_rapidwright_boundary(
                    mapped,
                    packed,
                    architecture,
                    physical,
                    candidate,
                    boundary_path,
                )
        self.assertEqual(
            boundary["schema"], DREAMPLACEFPGA_RAPIDWRIGHT_BOUNDARY_SCHEMA
        )
        self.assertEqual(boundary["status"], "blocked")
        self.assertFalse(boundary["rapidwright_eligible"])
        self.assertFalse(boundary["fallback_used"])
        self.assertIn(
            "stages.detailed_placement:core_missing", boundary["blockers"]
        )
        self.assertTrue(
            boundary["validated_boundary"]["packed_clusters_preserved"]
        )


if __name__ == "__main__":
    unittest.main()
