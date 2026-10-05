import json
import hashlib
import copy
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.architecture import ArchitectureDB
from emuflow.xilinx_openparf_atomic import (
    OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
    OPENPARF_ATOMIC_PROVIDER,
    export_xilinx_openparf_atomic,
    load_xilinx_openparf_atomic_placement_clusters,
    load_xilinx_openparf_atomic_sites,
    validate_xilinx_openparf_atomic_placement,
)
from emuflow.xilinx_openparf_bridge import (
    _validate_certificate,
    materialize_xilinx_openparf_atomic_contract,
)
from emuflow.xilinx_packing import validate_xilinx_packing
from emuflow.xilinx_placement import validate_xilinx_placement
from emuflow.xilinx_rwroute import export_rwroute_input
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


def _inline_certificate(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    value["clusters"] = load_xilinx_openparf_atomic_placement_clusters(
        path, value
    )
    value.pop("cluster_storage", None)
    return value


def _native_certificate(
    root: Path, *, include_hard: bool, split_ff_control_sets: bool = False
):
    mapped, source_packed, architecture = write_openparf_runtime_fixture(
        root, include_hard=include_hard
    )
    if split_ff_control_sets:
        mapped_value = json.loads(mapped.read_text())
        mapped_value["modules"]["top"]["cells"]["ff_03"][
            "connections"
        ]["C"] = [1_000_001]
        mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
    export_dir = root / "openparf"
    export_xilinx_openparf_atomic(
        mapped, source_packed, architecture, export_dir
    )
    name_map = json.loads((export_dir / "name_map.json").read_text())
    sites = load_xilinx_openparf_atomic_sites(export_dir / "name_map.json")
    slices = [item for item in sites if "LUT" in item["resources"]]
    hard_sites = {
        resource: next(
            item for item in sites if resource in item["resources"]
        )
        for resource in ("DSP48E2", "RAMB36E2", "URAM288")
        if any(resource in item["resources"] for item in sites)
    }
    indexes = {"LUT": 0, "FF": 0}
    rows = []
    for atom in name_map["atoms"]:
        resource = atom["resource"]
        if resource in indexes:
            index = indexes[resource]
            indexes[resource] += 1
            site = slices[index // 4]
            z = 2 * (index % 4) + (1 if resource == "LUT" else 0)
            if split_ff_control_sets and atom["instance"] == "ff_03":
                z = 8
        else:
            site = hard_sites[resource]
            z = 0
        rows.append(
            f"{atom['openparf']} {site['dense_x']} {site['dense_y']} {z}"
        )
    native_placement = export_dir / "native.pl"
    native_placement.write_text("\n".join(rows) + "\n", encoding="utf-8")
    certificate_path = root / "atomic-certificate.json"
    certificate = validate_xilinx_openparf_atomic_placement(
        native_placement, export_dir / "name_map.json", mapped,
        architecture, certificate_path,
    )
    certificate["runtime_validation"] = "native-openparf"
    persisted = json.loads(certificate_path.read_text(encoding="utf-8"))
    persisted["runtime_validation"] = "native-openparf"
    certificate_path.write_text(
        json.dumps(persisted, sort_keys=True), encoding="utf-8"
    )
    return mapped, architecture, certificate_path


class XilinxOpenparfBridgeTest(unittest.TestCase):
    def test_bram_tile_group_certificate_is_independently_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture_path = root / "architecture.json"
            architecture_path.write_text(json.dumps({
                "schema": "emuflow.archdb/v1", "part": "xcvu19p-test",
                "source": {"format": "unit-test/v1"},
                "policy": {"name": "unit-test"},
                "site_templates": {
                    "RAMB180": {
                        "bels": [{
                            "name": "RAMB18E2_L", "type": "RAMB18E2",
                            "z": 0, "compatible_cells": ["RAMB18E2"],
                        }], "alternative_templates": [],
                    },
                    "RAMB36": {
                        "bels": [{
                            "name": "RAMB36E2", "type": "RAMB36E2",
                            "z": 0, "compatible_cells": ["RAMB36E2"],
                        }], "alternative_templates": [],
                    },
                    "RAMB181": {
                        "bels": [
                            {
                                "name": "RAMB18E2_L", "type": "RAMB18E2",
                                "z": 0, "compatible_cells": ["RAMB18E2"],
                                "placement_mode": "RAMB180",
                            },
                            {
                                "name": "RAMB18E2_U", "type": "RAMB18E2",
                                "z": 1, "compatible_cells": ["RAMB18E2"],
                                "placement_mode": "RAMB181",
                            },
                            {
                                "name": "RAMB36E2", "type": "RAMB36E2",
                                "z": 0, "compatible_cells": ["RAMB36E2"],
                                "placement_mode": "RAMB36",
                            },
                        ],
                        "alternative_templates": ["RAMB180", "RAMB36"],
                    },
                },
                "sites": [{
                    "name": "RAMB18_X0Y1", "type": "RAMB181",
                    "x": 4, "y": 1,
                    "bels": [
                        {
                            "name": "RAMB18E2_L", "type": "RAMB18E2",
                            "z": 0, "compatible_cells": ["RAMB18E2"],
                            "placement_mode": "RAMB180",
                        },
                        {
                            "name": "RAMB18E2_U", "type": "RAMB18E2",
                            "z": 1, "compatible_cells": ["RAMB18E2"],
                            "placement_mode": "RAMB181",
                        },
                        {
                            "name": "RAMB36E2", "type": "RAMB36E2",
                            "z": 0, "compatible_cells": ["RAMB36E2"],
                            "placement_mode": "RAMB36",
                        },
                    ],
                }],
            }), encoding="utf-8")
            architecture = ArchitectureDB.load(architecture_path)
            mapped = {
                "modules": {"top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "lo": {"type": "RAMB18E2"},
                        "hi": {"type": "RAMB18E2"},
                    },
                }},
            }
            tile = "BRAM_X0Y0"
            native_group = {
                "anchor": "RAMB18_X0Y1", "tile": tile,
                "lower": {
                    "site": "RAMB18_X0Y0", "site_type": "RAMB180",
                    "bel": "RAMB18E2_L",
                },
                "upper": {
                    "site": "RAMB18_X0Y1", "site_type": "RAMB181",
                    "bel": "RAMB18E2_U",
                },
                "whole": {
                    "site": "RAMB36_X0Y0", "site_type": "RAMB36",
                    "bel": "RAMB36E2",
                },
            }

            def assignment(instance, role):
                view = native_group[role]
                return {
                    "instance": instance, "cell_type": "RAMB18E2",
                    "bel": view["bel"], "physical_site": view["site"],
                    "placement_mode": view["site_type"],
                    "source_cluster": "source:bram",
                    "bram_tile_group": {
                        "anchor": native_group["anchor"],
                        "claims": [f"bram:{tile}:{role}"],
                        "role": role, "tile": tile,
                    },
                }

            certificate = {
                "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                "status": "pass", "part": architecture.part,
                "provider": OPENPARF_ATOMIC_PROVIDER,
                "runtime_validation": "native-openparf",
                "clusters": [{
                    "cluster": "openparf:RAMB18_X0Y1",
                    "site": "RAMB18_X0Y1", "site_type": "RAMB181",
                    "x": 4, "y": 1,
                    "assignments": [
                        assignment("lo", "lower"), assignment("hi", "upper"),
                    ],
                }],
                "summary": {
                    "atoms": 2, "occupied_sites": 1, "luts": 0, "ffs": 0,
                    "hard_resources": {"RAMB18E2": 2},
                },
            }
            native_groups = {native_group["anchor"]: native_group}
            _validate_certificate(
                mapped, certificate, architecture, "top",
                native_bram_groups=native_groups,
            )

            for path, replacement in (
                (("bram_tile_group", "role"), "upper"),
                (("bram_tile_group", "claims"), [f"bram:{tile}:upper"]),
                (("bram_tile_group", "tile"), "BRAM_WRONG"),
                (("physical_site",), "RAMB18_X0Y1"),
            ):
                with self.subTest(path=path):
                    broken = copy.deepcopy(certificate)
                    target = broken["clusters"][0]["assignments"][0]
                    if len(path) == 1:
                        target[path[0]] = replacement
                    else:
                        target[path[0]][path[1]] = replacement
                    with self.assertRaisesRegex(
                        ValidationError, "BRAM tile group disagrees"
                    ):
                        _validate_certificate(
                            mapped, broken, architecture, "top",
                            native_bram_groups=native_groups,
                        )

            conflicting = copy.deepcopy(certificate)
            conflicting_mapped = copy.deepcopy(mapped)
            conflicting_mapped["modules"]["top"]["cells"]["whole"] = {
                "type": "RAMB36E2"
            }
            whole = native_group["whole"]
            conflicting["clusters"][0]["assignments"].append({
                "instance": "whole", "cell_type": "RAMB36E2",
                "bel": whole["bel"], "physical_site": whole["site"],
                "placement_mode": whole["site_type"],
                "source_cluster": "source:bram",
                "bram_tile_group": {
                    "anchor": native_group["anchor"],
                    "claims": [
                        f"bram:{tile}:lower", f"bram:{tile}:upper",
                    ],
                    "role": "whole", "tile": tile,
                },
            })
            conflicting["summary"] = {
                "atoms": 3, "occupied_sites": 1, "luts": 0, "ffs": 0,
                "hard_resources": {"RAMB18E2": 2, "RAMB36E2": 1},
            }
            with self.assertRaisesRegex(
                ValidationError, "hard-resource site is not singleton"
            ):
                _validate_certificate(
                    conflicting_mapped, conflicting, architecture, "top",
                    native_bram_groups=native_groups,
                )

    def test_native_legal_multi_control_set_slice_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False, split_ff_control_sets=True
            )
            packed = root / "native-packed.json"
            placement = root / "native-placement.json"
            materialize_xilinx_openparf_atomic_contract(
                mapped, architecture, certificate, packed, placement
            )
            check = validate_xilinx_packing(
                mapped, packed, architecture_path=architecture
            )
            self.assertEqual(check["status"], "pass")
            value = json.loads(packed.read_text())
            cluster = next(
                item for item in value["clusters"]
                if {assignment["instance"] for assignment in item["assignments"]}
                >= {"ff_00", "ff_03"}
            )
            self.assertIsNone(cluster["control_set"])
            self.assertEqual(len(cluster["control_sets"]), 2)

    def test_mixed_atomic_result_converts_and_feeds_rwroute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=True
            )
            mapped_before = mapped.read_bytes()
            packed = root / "native-packed.json"
            placement = root / "native-placement.json"
            report = materialize_xilinx_openparf_atomic_contract(
                mapped, architecture, certificate, packed, placement
            )

            self.assertEqual(mapped.read_bytes(), mapped_before)
            self.assertEqual(report["placed_cells"], 131)
            self.assertEqual(report["clusters"], 19)
            self.assertEqual(report["runtime_validation"], "native-openparf")
            self.assertEqual(
                validate_xilinx_packing(
                    mapped, packed, architecture_path=architecture
                )["cells"],
                131,
            )
            self.assertEqual(
                validate_xilinx_placement(packed, architecture, placement)["cells"],
                131,
            )

            packed_value = json.loads(packed.read_text())
            placement_value = json.loads(placement.read_text())
            packed_instances = [
                assignment["instance"]
                for cluster in packed_value["clusters"]
                for assignment in cluster["assignments"]
            ]
            placed_instances = [
                assignment["instance"]
                for cluster in placement_value["clusters"]
                for assignment in cluster["assignments"]
            ]
            self.assertEqual(len(packed_instances), len(set(packed_instances)))
            self.assertEqual(set(packed_instances), set(placed_instances))
            self.assertEqual(
                {cluster["id"][len("openparf:"):]
                 for cluster in packed_value["clusters"]},
                {cluster["site"] for cluster in placement_value["clusters"]},
            )
            self.assertEqual(
                placement_value["provider"],
                "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1",
            )
            rwroute = root / "rwroute.tsv"
            rwroute_report = export_rwroute_input(
                mapped, packed, placement, rwroute
            )
            self.assertEqual(rwroute_report["logical_cells"], 131)
            self.assertEqual(rwroute_report["physical_cells"], 138)
            self.assertTrue(rwroute.is_file())

    def test_missing_atom_fails_without_publishing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            value = _inline_certificate(certificate)
            value["clusters"][0]["assignments"].pop()
            certificate.write_text(json.dumps(value), encoding="utf-8")
            packed = root / "converted-packed.json"
            placement = root / "converted-placement.json"
            with self.assertRaisesRegex(ValidationError, "coverage is incomplete"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate, packed, placement
                )
            self.assertFalse(packed.exists())
            self.assertFalse(placement.exists())

    def test_bel_tampering_and_unsupported_primitive_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            value = _inline_certificate(certificate)
            assignment = value["clusters"][0]["assignments"][0]
            assignment["bel"] = "NOT_A_BEL"
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "not uniquely compatible"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "bad-packed.json", root / "bad-placement.json",
                )

            mapped_value = json.loads(mapped.read_text())
            mapped_value["modules"]["top"]["cells"]["lut_00"]["type"] = "BUFGCE"
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            value["source"]["mapped_sha256"] = hashlib.sha256(
                mapped.read_bytes()
            ).hexdigest()
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "does not support primitives"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "unsupported-packed.json",
                    root / "unsupported-placement.json",
                )

    def test_mapped_connectivity_tampering_breaks_the_source_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            mapped_value = json.loads(mapped.read_text())
            mapped_value["modules"]["top"]["cells"]["lut_00"][
                "connections"
            ]["I0"] = [999_999]
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "source identity"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "packed.json", root / "placement.json",
                )

    def test_duplicate_site_group_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=False
            )
            value = _inline_certificate(certificate)
            value["clusters"].append(dict(value["clusters"][0]))
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate physical site"):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "packed.json", root / "placement.json",
                )

    def test_cascade_certificate_requires_source_packing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, certificate = _native_certificate(
                root, include_hard=True
            )
            value = json.loads(certificate.read_text())
            value["summary"]["native_hardblock_edges"] = 1
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "requires its source packing"
            ):
                materialize_xilinx_openparf_atomic_contract(
                    mapped, architecture, certificate,
                    root / "packed.json", root / "placement.json",
                )


if __name__ == "__main__":
    unittest.main()
