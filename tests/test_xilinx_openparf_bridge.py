import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_openparf_atomic import (
    export_xilinx_openparf_atomic,
    load_xilinx_openparf_atomic_sites,
    validate_xilinx_openparf_atomic_placement,
)
from emuflow.xilinx_openparf_bridge import (
    materialize_xilinx_openparf_atomic_contract,
)
from emuflow.xilinx_packing import validate_xilinx_packing
from emuflow.xilinx_placement import validate_xilinx_placement
from emuflow.xilinx_rwroute import export_rwroute_input
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


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
    certificate_path.write_text(
        json.dumps(certificate, sort_keys=True), encoding="utf-8"
    )
    return mapped, architecture, certificate_path


class XilinxOpenparfBridgeTest(unittest.TestCase):
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
            value = json.loads(certificate.read_text())
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
            value = json.loads(certificate.read_text())
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
            value = json.loads(certificate.read_text())
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
