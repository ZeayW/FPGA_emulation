import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from emuflow.fpga_interchange import (
    architecture_from_fpga_interchange_extract,
)
from emuflow.io import read_json, write_json
from emuflow.physical_regions import (
    merge_physical_regions,
    run_physical_region_merge,
    run_physical_region_rebind,
    validate_fpga_interchange_architecture_regions,
    validate_physical_region_sidecar,
)


ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "examples/phase2/fpga_interchange_extract_fixture.json"
SIDECAR = ROOT / "examples/phase2/physical_region_sidecar_fixture.json"


class PhysicalRegionSidecarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.architecture = architecture_from_fpga_interchange_extract(
            read_json(EXTRACT),
            part="xcvu3p-ffvc1517-2-e",
            input_path=EXTRACT,
            generator="fixture",
        )

    def test_exact_sidecar_is_merged_and_independently_checked(self) -> None:
        sidecar = read_json(SIDECAR)
        checked = validate_physical_region_sidecar(
            self.architecture, sidecar
        )
        merged = merge_physical_regions(
            self.architecture, sidecar, sidecar_path=SIDECAR
        )
        report = validate_fpga_interchange_architecture_regions(merged)
        self.assertEqual(checked["sites"], 3)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["sites_per_slr"], {"SLR0": 3})
        self.assertEqual(
            merged.site_named("DSP48E2_X0Y2")["physical_region"],
            {
                "slr": "SLR0",
                "clock_region": "X1Y0",
                "qualification": "test-fixture-only",
            },
        )
        self.assertRegex(
            merged.value["physical_region_model"]["overlay"]["sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_missing_site_assignment_is_rejected(self) -> None:
        sidecar = read_json(SIDECAR)
        sidecar["site_region_groups"][1]["sites"].remove("RAMB36_X0Y2")
        with self.assertRaisesRegex(Exception, "does not cover every"):
            validate_physical_region_sidecar(self.architecture, sidecar)

    def test_duplicate_site_assignment_is_rejected(self) -> None:
        sidecar = read_json(SIDECAR)
        sidecar["site_region_groups"][0]["sites"].append("DSP48E2_X0Y2")
        with self.assertRaisesRegex(Exception, "duplicate assignment"):
            validate_physical_region_sidecar(self.architecture, sidecar)

    def test_cross_slr_clock_region_is_rejected(self) -> None:
        sidecar = read_json(SIDECAR)
        sidecar["slrs"].append({"name": "SLR1", "index": 1})
        sidecar["site_region_groups"][0]["slr"] = "SLR1"
        with self.assertRaisesRegex(Exception, "different SLR"):
            validate_physical_region_sidecar(self.architecture, sidecar)

    def test_bound_architecture_hash_is_enforced(self) -> None:
        sidecar = read_json(SIDECAR)
        sidecar["source"]["architecture_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture_path = root / "architecture.json"
            sidecar_path = root / "regions.json"
            write_json(architecture_path, self.architecture.to_dict())
            write_json(sidecar_path, sidecar)
            with self.assertRaisesRegex(Exception, "different ArchitectureDB"):
                run_physical_region_merge(
                    architecture_path=architecture_path,
                    sidecar_path=sidecar_path,
                    output_path=root / "merged.json",
                )

    def test_merged_catalog_tampering_is_rejected(self) -> None:
        merged = merge_physical_regions(
            self.architecture, read_json(SIDECAR), sidecar_path=SIDECAR
        ).to_dict()
        merged["physical_regions"]["clock_regions"][0]["slr"] = "SLR9"
        with self.assertRaisesRegex(Exception, "unknown SLR"):
            validate_fpga_interchange_architecture_regions(
                type(self.architecture)(merged)
            )

    def test_sidecar_rebind_accepts_metadata_only_architecture_change(self) -> None:
        old = self.architecture.to_dict()
        new = copy.deepcopy(old)
        new["source"]["generator"] = "new importer metadata"
        new["routing_resource_counts"]["nodes"] += 1
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "old.json"
            new_path = root / "new.json"
            sidecar_path = root / "regions.json"
            rebound_path = root / "rebound.json"
            write_json(old_path, old)
            write_json(new_path, new)
            sidecar = read_json(SIDECAR)
            sidecar["source"]["architecture_sha256"] = hashlib.sha256(
                old_path.read_bytes()
            ).hexdigest()
            write_json(sidecar_path, sidecar)
            report = run_physical_region_rebind(
                old_architecture_path=old_path,
                new_architecture_path=new_path,
                sidecar_path=sidecar_path,
                output_path=rebound_path,
            )
            self.assertEqual(report["status"], "pass")
            rebound = read_json(rebound_path)
            self.assertEqual(
                rebound["source"]["architecture_sha256"],
                hashlib.sha256(new_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                rebound["source"]["architecture_rebind"]["qualification"],
                "physical-inventory-identical-v1",
            )

    def test_sidecar_rebind_rejects_physical_inventory_change(self) -> None:
        old = self.architecture.to_dict()
        new = copy.deepcopy(old)
        new["sites"][0]["x"] += 1
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "old.json"
            new_path = root / "new.json"
            sidecar_path = root / "regions.json"
            write_json(old_path, old)
            write_json(new_path, new)
            sidecar = read_json(SIDECAR)
            sidecar["source"]["architecture_sha256"] = hashlib.sha256(
                old_path.read_bytes()
            ).hexdigest()
            write_json(sidecar_path, sidecar)
            with self.assertRaisesRegex(Exception, "fields differ: sites"):
                run_physical_region_rebind(
                    old_architecture_path=old_path,
                    new_architecture_path=new_path,
                    sidecar_path=sidecar_path,
                    output_path=root / "rebound.json",
                )


if __name__ == "__main__":
    unittest.main()
