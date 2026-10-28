"""Independent checks of the maintained full-device placement policy."""
from tests.openparf_region_reference import _select_xilinx_slr_window
import tempfile
import unittest
from pathlib import Path


class XilinxPlacementRegionPolicyTest(unittest.TestCase):
    def test_slr_window_uses_complete_device_with_headroom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = root / "arch.json"
            packed = root / "packed.json"
            sites = []
            for index in range(4):
                for x in range(4):
                    sites.append({
                        "name": f"SLICE_X{x}Y{index}", "type": "SLICEL",
                        "template": "SLICEL",
                        "x": x, "y": index,
                        "tile": {"grid_col": x, "grid_row": index * 10},
                        "physical_region": {"slr": f"SLR{index}"},
                    })
            arch.write_text(__import__("json").dumps({
                "schema": "emuflow.archdb/v1", "part": "test",
                "source": {"format": "test/v1"}, "policy": {"name": "test"},
                "site_templates": {"SLICEL": {
                    "bels": [{
                        "name": "A6LUT", "type": "LUT6", "z": 0,
                        "compatible_cells": ["LUT6"],
                    }],
                    "alternative_templates": [],
                }},
                "sites": sites,
            }), encoding="utf-8")
            packed.write_text(__import__("json").dumps({
                "schema": "emuflow.packed-site-netlist/v1",
                "clusters": [
                    {"id": f"c{i}", "kind": "slice", "assignments": []}
                    for i in range(5)
                ],
            }), encoding="utf-8")
            self.assertEqual(
                _select_xilinx_slr_window(packed, arch),
                ("SLR0", "SLR1", "SLR2", "SLR3"),
            )

    def test_sparse_partition_still_uses_complete_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = root / "arch.json"
            packed = root / "packed.json"
            sites = []
            for index in range(4):
                for x in range(4):
                    sites.append({
                        "name": f"SLICE_X{x}Y{index}", "type": "SLICEL",
                        "template": "SLICEL",
                        "x": x, "y": index,
                        "tile": {"grid_col": x, "grid_row": index * 10},
                        "physical_region": {"slr": f"SLR{index}"},
                    })
            arch.write_text(__import__("json").dumps({
                "schema": "emuflow.archdb/v1", "part": "test",
                "source": {"format": "test/v1"}, "policy": {"name": "test"},
                "site_templates": {"SLICEL": {
                    "bels": [{
                        "name": "A6LUT", "type": "LUT6", "z": 0,
                        "compatible_cells": ["LUT6"],
                    }],
                    "alternative_templates": [],
                }},
                "sites": sites,
            }), encoding="utf-8")
            packed.write_text(__import__("json").dumps({
                "schema": "emuflow.packed-site-netlist/v1",
                "clusters": [
                    {"id": "c0", "kind": "slice", "assignments": []}
                ],
            }), encoding="utf-8")
            self.assertEqual(
                _select_xilinx_slr_window(packed, arch),
                ("SLR0", "SLR1", "SLR2", "SLR3"),
            )

    def test_atomic_slr_window_counts_slice_site_equivalents_not_atoms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = root / "arch.json"
            packed = root / "atomic.json"
            sites = [
                {
                    "name": f"SLICE_X{x}Y{slr}", "type": "SLICEL",
                    "template": "SLICEL", "x": x, "y": slr,
                    "tile": {"grid_col": x, "grid_row": slr * 10},
                    "physical_region": {"slr": f"SLR{slr}"},
                }
                for slr in range(3) for x in range(16)
            ]
            arch.write_text(__import__("json").dumps({
                "schema": "emuflow.archdb/v1", "part": "test",
                "source": {"format": "test/v1"}, "policy": {"name": "test"},
                "site_templates": {"SLICEL": {
                    "bels": [{
                        "name": "A6LUT", "type": "LUT6", "z": 0,
                        "compatible_cells": ["LUT6"],
                    }],
                    "alternative_templates": [],
                }},
                "sites": sites,
            }), encoding="utf-8")
            clusters = [
                {
                    "id": f"lut-{index}", "kind": "slice",
                    "assignments": [{
                        "instance": f"lut-{index}", "cell_type": "LUT6",
                    }],
                }
                for index in range(64)
            ] + [
                {
                    "id": f"ff-{index}", "kind": "slice",
                    "assignments": [{
                        "instance": f"ff-{index}", "cell_type": "FDRE",
                    }],
                }
                for index in range(64)
            ]
            packed.write_text(__import__("json").dumps({
                "schema": "emuflow.openparf-atomic-source/v1",
                "clusters": clusters,
            }), encoding="utf-8")
            # Capacity admission still counts slice-site equivalents, while
            # the legal placement region remains the complete device.
            self.assertEqual(
                _select_xilinx_slr_window(packed, arch),
                ("SLR0", "SLR1", "SLR2"),
            )
