import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_placement import (
    place_xilinx_clusters,
    validate_xilinx_placement,
)


def bel(name, cell_type, z=0):
    return {
        "name": name,
        "type": cell_type,
        "z": z,
        "compatible_cells": [cell_type],
    }


class XilinxPlacementTest(unittest.TestCase):
    def _architecture(self):
        templates = {
            "SLICEL": {
                "bels": [bel("A6LUT", "LUT6"), bel("B6LUT", "LUT6", 1)],
                "alternative_templates": [],
            },
            "DSP48E2": {
                "bels": [bel("DSP48E2", "DSP48E2")],
                "alternative_templates": [],
            },
        }
        sites = []
        for x in range(2):
            for y in range(2):
                sites.append({
                    "name": f"SLICE_X{x}Y{y}", "type": "SLICEL",
                    "template": "SLICEL", "x": x, "y": y,
                    "physical_region": {
                        "slr": f"SLR{x}", "clock_region": f"X{x}Y0"
                    },
                })
        for y in range(3):
            sites.append({
                "name": f"DSP48E2_X0Y{y}", "type": "DSP48E2",
                "template": "DSP48E2", "x": 2, "y": y,
                "physical_region": {"slr": "SLR0", "clock_region": "X2Y0"},
            })
        return {
            "schema": "emuflow.archdb/v1", "part": "xcvu19p-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": templates, "sites": sites,
        }

    def _packed(self):
        def cluster(cluster_id, kind, instance, cell_type, template, bel_name):
            return {
                "id": cluster_id, "kind": kind,
                "site_templates": [template],
                "assignments": [{
                    "instance": instance, "cell_type": cell_type,
                    "bel": bel_name, "bel_candidates": [bel_name],
                }],
            }
        return {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "clusters": [
                cluster("slice-a", "slice", "lut_a", "LUT6", "SLICEL", "A6LUT"),
                cluster("slice-b", "slice", "lut_b", "LUT6", "SLICEL", "A6LUT"),
                cluster("dsp-a", "dsp", "dsp_a", "DSP48E2", "DSP48E2", "DSP48E2"),
                cluster("dsp-b", "dsp", "dsp_b", "DSP48E2", "DSP48E2", "DSP48E2"),
            ],
            "cascade_chains": [{
                "kind": "DSP48E2", "instances": ["dsp_a", "dsp_b"],
                "links": [{"source": "dsp_a", "sink": "dsp_b"}],
            }],
        }

    def _write_inputs(self, root):
        arch = root / "arch.json"
        packed = root / "packed.json"
        guidance = root / "guidance.json"
        constraints = root / "constraints.json"
        arch.write_text(json.dumps(self._architecture()), encoding="utf-8")
        packed.write_text(json.dumps(self._packed()), encoding="utf-8")
        guidance.write_text(json.dumps({
            "schema": "emuflow.xilinx-global-placement-guidance/v1",
            "clusters": [
                {"cluster": "slice-a", "x": 0, "y": 1},
                {"cluster": "slice-b", "x": 0, "y": 1},
                {"cluster": "dsp-a", "x": 2, "y": 1},
                {"cluster": "dsp-b", "x": 2, "y": 2},
            ],
        }), encoding="utf-8")
        constraints.write_text(json.dumps({
            "schema": "emuflow.xilinx-placement-constraints/v1",
            "clusters": [
                {"cluster": "slice-a", "site": "SLICE_X0Y1"},
                {"cluster": "slice-b", "slr": "SLR1", "clock_region": "X1Y0"},
            ],
        }), encoding="utf-8")
        return arch, packed, guidance, constraints

    def test_exact_legalizer_and_checker_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, constraints = self._write_inputs(root)
            first, second = root / "first.json", root / "second.json"
            result = place_xilinx_clusters(
                packed, arch, first,
                guidance_path=guidance, constraints_path=constraints,
            )
            place_xilinx_clusters(
                packed, arch, second,
                guidance_path=guidance, constraints_path=constraints,
            )
            checked = validate_xilinx_placement(
                packed, arch, first, constraints_path=constraints
            )
            first_value = json.loads(first.read_text(encoding="utf-8"))
            second_value = json.loads(second.read_text(encoding="utf-8"))
        self.assertEqual(result["summary"]["cascade_chains"], 1)
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(first_value, second_value)
        placed = {item["cluster"]: item["site"] for item in first_value["clusters"]}
        self.assertEqual(placed["slice-a"], "SLICE_X0Y1")
        self.assertTrue(placed["slice-b"].startswith("SLICE_X1"))
        self.assertEqual(placed["dsp-b"], "DSP48E2_X0Y2")
        self.assertEqual(placed["dsp-a"], "DSP48E2_X0Y1")

    def test_independent_checker_rejects_site_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, constraints = self._write_inputs(root)
            output = root / "placement.json"
            place_xilinx_clusters(
                packed, arch, output,
                guidance_path=guidance, constraints_path=constraints,
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            value["clusters"][1]["site"] = value["clusters"][0]["site"]
            value["clusters"][1]["x"] = value["clusters"][0]["x"]
            value["clusters"][1]["y"] = value["clusters"][0]["y"]
            value["clusters"][1]["site_type"] = value["clusters"][0]["site_type"]
            value["clusters"][1]["assignments"] = value["clusters"][0]["assignments"]
            output.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "overlap"):
                validate_xilinx_placement(
                    packed, arch, output, constraints_path=constraints
                )


if __name__ == "__main__":
    unittest.main()
