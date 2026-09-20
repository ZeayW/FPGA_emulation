import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_placement import (
    plan_xilinx_single_slr,
    place_xilinx_clusters,
    validate_xilinx_single_slr_plan,
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
            sites.append({
                "name": f"DSP48E2_X1Y{y}", "type": "DSP48E2",
                "template": "DSP48E2", "x": 3, "y": y,
                "physical_region": {"slr": "SLR1", "clock_region": "X3Y0"},
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

    def test_bram_anchor_expands_to_exact_rapidwright_sites(self):
        architecture = {
            "schema": "emuflow.archdb/v1", "part": "xcvu19p-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": {
                "RAMB181": {
                    "bels": [bel("RAMB18E2_U", "RAMB18E2")],
                    "alternative_templates": ["RAMB180", "RAMB36"],
                },
                "RAMB180": {
                    "bels": [bel("RAMB18E2_L", "RAMB18E2")],
                    "alternative_templates": [],
                },
                "RAMB36": {
                    "bels": [bel("RAMB36E2", "RAMB36E2")],
                    "alternative_templates": [],
                },
            },
            "sites": [{
                "name": "RAMB18_X4Y241", "type": "RAMB181",
                "template": "RAMB181", "x": 4, "y": 241,
            }],
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "clusters": [{
                "id": "bram", "kind": "hard",
                "site_templates": ["RAMB180", "RAMB181"],
                "assignments": [
                    {"instance": "lo", "cell_type": "RAMB18E2", "bel": "RAMB18E2_L"},
                    {"instance": "hi", "cell_type": "RAMB18E2", "bel": "RAMB18E2_U"},
                ],
            }], "cascade_chains": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = root / "arch.json"
            packed_path = root / "packed.json"
            output = root / "place.json"
            arch.write_text(json.dumps(architecture), encoding="utf-8")
            packed_path.write_text(json.dumps(packed), encoding="utf-8")
            place_xilinx_clusters(packed_path, arch, output)
            value = json.loads(output.read_text(encoding="utf-8"))
            validate_xilinx_placement(packed_path, arch, output)
        sites = {
            item["instance"]: item["site"]
            for item in value["clusters"][0]["assignments"]
        }
        self.assertEqual(
            sites, {"lo": "RAMB18_X4Y240", "hi": "RAMB18_X4Y241"}
        )

    def test_single_slr_planner_uses_legal_guidance_not_slr_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, _constraints = self._write_inputs(root)
            guidance.write_text(json.dumps({
                "schema": "emuflow.xilinx-global-placement-guidance/v1",
                "clusters": [
                    {"cluster": "slice-a", "x": 1, "y": 0},
                    {"cluster": "slice-b", "x": 1, "y": 1},
                    {"cluster": "dsp-a", "x": 3, "y": 1},
                    {"cluster": "dsp-b", "x": 3, "y": 2},
                ],
            }), encoding="utf-8")
            first = root / "single-slr-first.json"
            second = root / "single-slr-second.json"
            result = plan_xilinx_single_slr(
                packed, arch, first, guidance_path=guidance
            )
            plan_xilinx_single_slr(
                packed, arch, second, guidance_path=guidance
            )
            checked = validate_xilinx_single_slr_plan(
                packed, arch, first, guidance_path=guidance
            )
            first_value = json.loads(first.read_text(encoding="utf-8"))
            second_value = json.loads(second.read_text(encoding="utf-8"))
        self.assertEqual(result["selected_slr"], "SLR1")
        self.assertEqual(checked["selected_slr"], "SLR1")
        self.assertEqual(first_value, second_value)
        self.assertEqual(
            {entry["slr"] for entry in first_value["clusters"]}, {"SLR1"}
        )
        self.assertEqual(
            [entry["status"] for entry in first_value["candidates"]],
            ["feasible", "feasible"],
        )

    def test_single_slr_planner_fails_when_no_slr_has_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, _packed, _guidance, _constraints = self._write_inputs(root)
            packed = root / "overfull.json"
            packed.write_text(json.dumps({
                "schema": "emuflow.packed-site-netlist/v1",
                "status": "pass",
                "clusters": [
                    {
                        "id": f"slice-{index}", "kind": "slice",
                        "site_templates": ["SLICEL"],
                        "assignments": [{
                            "instance": f"lut-{index}", "cell_type": "LUT6",
                            "bel": "A6LUT", "bel_candidates": ["A6LUT"],
                        }],
                    }
                    for index in range(3)
                ],
                "cascade_chains": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "no single SLR"):
                plan_xilinx_single_slr(
                    packed, arch, root / "impossible.json"
                )

    def test_single_slr_plan_validator_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, _constraints = self._write_inputs(root)
            output = root / "single-slr.json"
            plan_xilinx_single_slr(
                packed, arch, output, guidance_path=guidance
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            value["clusters"][0]["slr"] = (
                "SLR1" if value["selected_slr"] == "SLR0" else "SLR0"
            )
            output.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "mixes regions"):
                validate_xilinx_single_slr_plan(
                    packed, arch, output, guidance_path=guidance
                )


if __name__ == "__main__":
    unittest.main()
