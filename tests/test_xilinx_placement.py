import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
                "cell_type": "DSP48E2", "instances": ["dsp_a", "dsp_b"],
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

    def test_openparf_cascade_checker_uses_sealed_native_adjacency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, _constraints = self._write_inputs(root)
            placement = root / "placement.json"
            place_xilinx_clusters(packed, arch, placement, guidance_path=guidance)
            value = json.loads(placement.read_text(encoding="utf-8"))
            architecture = json.loads(arch.read_text(encoding="utf-8"))
            sites = {site["name"]: site for site in architecture["sites"]}
            desired = {
                "dsp-a": "DSP48E2_X0Y0",
                "dsp-b": "DSP48E2_X0Y2",
            }
            for entry in value["clusters"]:
                site_name = desired.get(entry["cluster"])
                if site_name is None:
                    continue
                site = sites[site_name]
                entry.update({
                    "site": site_name,
                    "site_type": site["type"],
                    "x": site["x"],
                    "y": site["y"],
                    "physical_region": site["physical_region"],
                })
                for assignment in entry["assignments"]:
                    assignment["site"] = site_name
            native_path = root / "native.json"
            provider_path = root / "provider.json"
            native_path.write_text("{}", encoding="utf-8")
            provider_path.write_text("{}", encoding="utf-8")
            value["provider"] = (
                "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1"
            )
            value["policy"] = {
                "clock_region_site_utilization_limit": 0.75,
                "capacity_rounding": "ceil-with-one-site-minimum",
                "packing": "native-openparf-atomic-site-groups-v1",
                "placement_certificate": "emuflow.openparf-atomic-placement/v1",
            }
            value["source"].update({
                "guidance_sha256": None,
                "native_constraints_sha256": hashlib.sha256(
                    native_path.read_bytes()
                ).hexdigest(),
                "provider_manifest_sha256": hashlib.sha256(
                    provider_path.read_bytes()
                ).hexdigest(),
            })
            placement.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "requires native"):
                validate_xilinx_placement(packed, arch, placement)
            native = {"payload": {"dedicated_adjacency": [{
                "kind": "DSP_CASCADE",
                "chains": [["DSP48E2_X0Y0", "DSP48E2_X0Y2"]],
            }]}}
            with mock.patch(
                "emuflow.xilinx_placement.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ):
                checked = validate_xilinx_placement(
                    packed, arch, placement,
                    native_constraints_path=native_path,
                    provider_manifest_path=provider_path,
                )
            self.assertEqual(checked["status"], "pass")

    def test_global_allowed_slr_window_is_compact_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, _constraints = self._write_inputs(root)
            constraints = root / "window.json"
            constraints.write_text(json.dumps({
                "schema": "emuflow.xilinx-placement-constraints/v1",
                "global": {"allowed_slrs": ["SLR1"]},
                "clusters": [],
            }), encoding="utf-8")
            output = root / "placement.json"
            place_xilinx_clusters(
                packed, arch, output,
                guidance_path=guidance, constraints_path=constraints,
            )
            checked = validate_xilinx_placement(
                packed, arch, output, constraints_path=constraints
            )
            value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(
            {entry["physical_region"]["slr"] for entry in value["clusters"]},
            {"SLR1"},
        )

    def test_guidance_distance_uses_physical_tile_grid(self):
        architecture = {
            "schema": "emuflow.archdb/v1", "part": "physical-grid-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": {
                "SLICEL": {
                    "bels": [bel("A6LUT", "LUT6")],
                    "alternative_templates": [],
                },
            },
            "sites": [
                {
                    "name": "SLICE_X0Y0", "type": "SLICEL",
                    "template": "SLICEL", "x": 0, "y": 0,
                    "tile": {"grid_col": 100, "grid_row": 0,
                             "site_index": 0},
                },
                {
                    "name": "SLICE_X1Y0", "type": "SLICEL",
                    "template": "SLICEL", "x": 27, "y": 0,
                    "tile": {"grid_col": 1, "grid_row": 0,
                             "site_index": 0},
                },
            ],
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "clusters": [{
                "id": "logic", "kind": "slice",
                "site_templates": ["SLICEL"],
                "assignments": [{
                    "instance": "lut", "cell_type": "LUT6",
                    "bel": "A6LUT", "bel_candidates": ["A6LUT"],
                }],
            }],
            "cascade_chains": [],
        }
        guidance = {
            "schema": "emuflow.xilinx-global-placement-guidance/v1",
            "clusters": [{"cluster": "logic", "x": 2, "y": 0}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch_path, packed_path = root / "arch.json", root / "packed.json"
            guidance_path, output = root / "guidance.json", root / "placed.json"
            arch_path.write_text(json.dumps(architecture), encoding="utf-8")
            packed_path.write_text(json.dumps(packed), encoding="utf-8")
            guidance_path.write_text(json.dumps(guidance), encoding="utf-8")
            result = place_xilinx_clusters(
                packed_path, arch_path, output, guidance_path=guidance_path
            )
        self.assertEqual(result["clusters"][0]["site"], "SLICE_X1Y0")
        self.assertEqual(result["summary"]["mean_guidance_displacement"], 1)

    def test_indexed_cascade_search_matches_exhaustive_reference(self):
        sites = []
        site_by_name = {}
        for physical_x in range(5):
            for physical_y in range(30):
                site = {
                    "name": f"DSP48E2_X{physical_x}Y{physical_y}",
                    "type": "DSP48E2", "template": "DSP48E2",
                    "x": 1000 - physical_x * 7,
                    "y": 500 - physical_y * 2 + physical_y // 7,
                    "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
                }
                sites.append(site)
                site_by_name[site["name"]] = site
        architecture = {
            "schema": "emuflow.archdb/v1", "part": "xcvu19p-index-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": {
                "DSP48E2": {
                    "bels": [bel("DSP48E2", "DSP48E2")],
                    "alternative_templates": [],
                },
            },
            "sites": sites,
        }
        clusters = []
        cascade_chains = []
        guidance_entries = []
        chain_ids = []
        for chain_index, length in enumerate((4, 2, 3, 4, 2, 3, 2, 4)):
            ids = []
            instances = []
            for offset in range(length):
                cluster_id = f"chain-{chain_index:02d}-{offset}"
                instance = f"dsp_{chain_index:02d}_{offset}"
                ids.append(cluster_id)
                instances.append(instance)
                clusters.append({
                    "id": cluster_id, "kind": "dsp",
                    "site_templates": ["DSP48E2"],
                    "assignments": [{
                        "instance": instance, "cell_type": "DSP48E2",
                        "bel": "DSP48E2", "bel_candidates": ["DSP48E2"],
                    }],
                })
                guidance_entries.append({
                    "cluster": cluster_id,
                    "x": 1000 - ((chain_index * 3 + offset) % 5) * 7 + 0.25,
                    "y": 500 - ((chain_index * 4 + offset * 3) % 25) * 2 + 0.5,
                })
            chain_ids.append(ids)
            cascade_chains.append({
                "kind": "DSP48E2", "instances": instances, "links": [],
            })
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "clusters": clusters, "cascade_chains": cascade_chains,
        }
        guidance_value = {
            "schema": "emuflow.xilinx-global-placement-guidance/v1",
            "clusters": guidance_entries,
        }
        guidance = {
            item["cluster"]: (item["x"], item["y"])
            for item in guidance_entries
        }

        # This is the original exhaustive rule: process chains in their stable
        # order and select the minimum (Manhattan cost, site-name tuple).
        all_names = sorted(site_by_name)
        used = set()
        expected = {}
        for chain in sorted(chain_ids, key=lambda item: (len(all_names), item)):
            best = None
            for first_name in all_names:
                prefix, physical_y = first_name.rsplit("Y", 1)
                names = tuple(
                    f"{prefix}Y{int(physical_y) + offset}"
                    for offset in range(len(chain))
                )
                if any(name not in site_by_name or name in used for name in names):
                    continue
                cost = sum(
                    abs(site_by_name[name]["x"] - guidance[cluster_id][0])
                    + abs(site_by_name[name]["y"] - guidance[cluster_id][1])
                    for cluster_id, name in zip(chain, names)
                )
                key = (cost, names)
                if best is None or key < best:
                    best = key
            self.assertIsNotNone(best)
            for cluster_id, name in zip(chain, best[1]):
                expected[cluster_id] = name
                used.add(name)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch_path = root / "arch.json"
            packed_path = root / "packed.json"
            guidance_path = root / "guidance.json"
            output = root / "placement.json"
            arch_path.write_text(json.dumps(architecture), encoding="utf-8")
            packed_path.write_text(json.dumps(packed), encoding="utf-8")
            guidance_path.write_text(json.dumps(guidance_value), encoding="utf-8")
            place_xilinx_clusters(
                packed_path, arch_path, output, guidance_path=guidance_path
            )
            value = json.loads(output.read_text(encoding="utf-8"))
        actual = {item["cluster"]: item["site"] for item in value["clusters"]}
        self.assertEqual(actual, expected)

    def test_cascade_chain_cannot_cross_an_slr_boundary(self):
        architecture = {
            "schema": "emuflow.archdb/v1", "part": "xcvu19p-slr-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": {
                "DSP48E2": {
                    "bels": [bel("DSP48E2", "DSP48E2")],
                    "alternative_templates": [],
                },
            },
            "sites": [
                {
                    "name": f"DSP48E2_X0Y{y}", "type": "DSP48E2",
                    "template": "DSP48E2", "x": 0, "y": y,
                    "physical_region": {
                        "slr": f"SLR{y}", "clock_region": f"X0Y{y}",
                    },
                }
                for y in range(2)
            ],
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "clusters": [
                {
                    "id": f"dsp-{index}", "kind": "dsp",
                    "site_templates": ["DSP48E2"],
                    "assignments": [{
                        "instance": f"dsp_{index}", "cell_type": "DSP48E2",
                        "bel": "DSP48E2", "bel_candidates": ["DSP48E2"],
                    }],
                }
                for index in range(2)
            ],
            "cascade_chains": [{
                "kind": "DSP48E2", "instances": ["dsp_0", "dsp_1"],
                "links": [{"source": "dsp_0", "sink": "dsp_1"}],
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch_path = root / "arch.json"
            packed_path = root / "packed.json"
            output = root / "placement.json"
            arch_path.write_text(json.dumps(architecture), encoding="utf-8")
            packed_path.write_text(json.dumps(packed), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cascade windows"):
                place_xilinx_clusters(packed_path, arch_path, output)

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

    def test_clock_region_headroom_spreads_guidance_and_is_validated(self):
        architecture = {
            "schema": "emuflow.archdb/v1", "part": "xcvu19p-density-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": {
                "SLICEL": {
                    "bels": [bel("A6LUT", "LUT6")],
                    "alternative_templates": [],
                },
            },
            "sites": [
                {
                    "name": f"SLICE_X{x}Y{y}", "type": "SLICEL",
                    "template": "SLICEL", "x": x, "y": y,
                    "physical_region": {
                        "slr": "SLR0",
                        "clock_region": "X0Y0" if x == 0 else "X1Y0",
                    },
                }
                for x in range(2)
                for y in range(8)
            ],
        }
        clusters = [
            {
                "id": f"slice-{index}", "kind": "slice",
                "site_templates": ["SLICEL"],
                "assignments": [{
                    "instance": f"lut-{index}", "cell_type": "LUT6",
                    "bel": "A6LUT", "bel_candidates": ["A6LUT"],
                }],
            }
            for index in range(7)
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "clusters": clusters, "cascade_chains": [],
        }
        guidance = {
            "schema": "emuflow.xilinx-global-placement-guidance/v1",
            "clusters": [
                {"cluster": cluster["id"], "x": 0, "y": index}
                for index, cluster in enumerate(clusters)
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch_path = root / "arch.json"
            packed_path = root / "packed.json"
            guidance_path = root / "guidance.json"
            output = root / "placement.json"
            arch_path.write_text(json.dumps(architecture), encoding="utf-8")
            packed_path.write_text(json.dumps(packed), encoding="utf-8")
            guidance_path.write_text(json.dumps(guidance), encoding="utf-8")
            result = place_xilinx_clusters(
                packed_path, arch_path, output, guidance_path=guidance_path
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            validate_xilinx_placement(packed_path, arch_path, output)

            placed_by_region = {"X0Y0": [], "X1Y0": []}
            for entry in value["clusters"]:
                placed_by_region[entry["physical_region"]["clock_region"]].append(
                    entry
                )
            self.assertEqual(
                {key: len(entries) for key, entries in placed_by_region.items()},
                {"X0Y0": 6, "X1Y0": 1},
            )
            self.assertEqual(
                result["summary"]["maximum_clock_region_site_utilization"],
                0.75,
            )
            self.assertEqual(
                result["summary"]["maximum_clock_region_site_reservation"],
                1.0,
            )

            # Move the spill cluster into the only unused site in the already
            # full clock region. It remains a legal, non-overlapping SLICEL,
            # so only the independent routability policy should reject it.
            spill = placed_by_region["X1Y0"][0]
            used = {entry["site"] for entry in placed_by_region["X0Y0"]}
            replacement = next(
                site for site in architecture["sites"]
                if site["physical_region"]["clock_region"] == "X0Y0"
                and site["name"] not in used
            )
            spill.update({
                "site": replacement["name"],
                "site_type": replacement["type"],
                "x": replacement["x"], "y": replacement["y"],
                "physical_region": replacement["physical_region"],
            })
            output.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "clock-region site utilization limit"
            ):
                validate_xilinx_placement(packed_path, arch_path, output)

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
            first_placement = root / "single-slr-first-placement.json"
            second_placement = root / "single-slr-second-placement.json"
            result = plan_xilinx_single_slr(
                packed, arch, first, first_placement, guidance_path=guidance
            )
            plan_xilinx_single_slr(
                packed, arch, second, second_placement, guidance_path=guidance
            )
            checked = validate_xilinx_single_slr_plan(
                packed, arch, first, first_placement, guidance_path=guidance
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
            ["capacity-feasible", "selected"],
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
                    packed, arch, root / "impossible.json",
                    root / "impossible-placement.json",
                )

    def test_single_slr_plan_validator_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, _constraints = self._write_inputs(root)
            output = root / "single-slr.json"
            placement = root / "single-slr-placement.json"
            plan_xilinx_single_slr(
                packed, arch, output, placement, guidance_path=guidance
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            value["clusters"][0]["slr"] = (
                "SLR1" if value["selected_slr"] == "SLR0" else "SLR0"
            )
            output.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "mixes regions"):
                validate_xilinx_single_slr_plan(
                    packed, arch, output, placement, guidance_path=guidance
                )

    def test_single_slr_planner_honors_required_region(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch, packed, guidance, _constraints = self._write_inputs(root)
            result = plan_xilinx_single_slr(
                packed, arch, root / "plan.json", root / "placement.json",
                guidance_path=guidance, required_slr="SLR1",
            )
            self.assertEqual(result["selected_slr"], "SLR1")
            self.assertEqual(result["policy"]["selection"], "required-slr")
            with self.assertRaisesRegex(ValidationError, "not present"):
                plan_xilinx_single_slr(
                    packed, arch, root / "bad.json", root / "bad-placement.json",
                    guidance_path=guidance, required_slr="SLR9",
                )


if __name__ == "__main__":
    unittest.main()
