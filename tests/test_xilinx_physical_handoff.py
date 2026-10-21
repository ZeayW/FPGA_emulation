import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from emuflow.xilinx_physical_handoff import (
    open_xilinx_physical_handoff,
    validate_xilinx_physical_handoff,
    validate_xilinx_physical_handoff_pair,
    write_xilinx_physical_handoff,
)
from emuflow.xilinx_placement import XILINX_PLACEMENT_SCHEMA


def _contracts(identity: str = "cell0"):
    packed = {
        "schema": PACKED_SITE_NETLIST_SCHEMA,
        "status": "pass",
        "mapping_profile": "test",
        "top": "top",
        "source": {"mapped_json_sha256": "a" * 64},
        "policy": {"provider": "test"},
        "clusters": [{
            "id": "openparf:SLICEL_X0Y0", "kind": "slice",
            "site_templates": ["SLICEL"], "control_set": None,
            "assignments": [{
                "instance": identity, "cell_type": "LUT1", "bel": "A6LUT",
            }],
        }],
        "cascade_chains": [{"cell_type": "CARRY8", "instances": [identity]}],
        "unplaced_constants": ["gnd"],
        "summary": {"clusters": 1},
    }
    placement = {
        "schema": XILINX_PLACEMENT_SCHEMA,
        "status": "pass", "part": "xcvu19p-test", "provider": "test",
        "policy": {"test": True},
        "source": {"packed_sha256": None, "architecture_sha256": "b" * 64},
        "clusters": [{
            "cluster": "openparf:SLICEL_X0Y0", "site": "SLICEL_X0Y0",
            "site_type": "SLICEL", "x": 0, "y": 0, "fixed": False,
            "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            "assignments": [{
                "instance": identity, "cell_type": "LUT1", "bel": "A6LUT",
                "placement_mode": "SLICEL", "site": "SLICEL_X0Y0",
            }],
        }],
        "summary": {"clusters": 1},
    }
    return packed, placement


class XilinxPhysicalHandoffTest(unittest.TestCase):
    def test_round_trip_exposes_lazy_equivalent_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            packed, placement = _contracts()
            write_xilinx_physical_handoff(path, packed, placement)
            report = validate_xilinx_physical_handoff(path)
            actual_packed, actual_placement = open_xilinx_physical_handoff(path)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["assignments"], 1)
            self.assertNotIsInstance(actual_packed["clusters"], list)
            self.assertEqual(list(actual_packed["clusters"]), packed["clusters"])
            self.assertEqual(
                list(actual_placement["clusters"]), placement["clusters"]
            )
            self.assertEqual(actual_packed["cascade_chains"], packed["cascade_chains"])
            self.assertEqual(
                actual_placement["source"]["packed_sha256"], file_sha256(path)
            )

    def test_validated_digest_avoids_rehashing_published_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            write_xilinx_physical_handoff(path, *_contracts())
            digest = file_sha256(path)

            with mock.patch(
                "emuflow.xilinx_physical_handoff.file_sha256",
                side_effect=AssertionError("unexpected duplicate digest"),
            ):
                packed, placement = open_xilinx_physical_handoff(
                    path, validated_sha256=digest
                )

            self.assertEqual(
                packed["source"]["physical_handoff_sha256"], digest
            )
            self.assertEqual(placement["source"]["packed_sha256"], digest)

            with self.assertRaisesRegex(ValidationError, "digest is invalid"):
                open_xilinx_physical_handoff(path, validated_sha256="invalid")

    def test_unordered_compatibility_views_remain_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            first_packed, first_placement = _contracts("cell0")
            second_packed, second_placement = _contracts("cell1")
            second_packed["clusters"][0]["id"] = "openparf:SLICEL_X1Y0"
            second_placement["clusters"][0].update({
                "cluster": "openparf:SLICEL_X1Y0",
                "site": "SLICEL_X1Y0",
                "x": 1,
            })
            second_placement["clusters"][0]["assignments"][0]["site"] = (
                "SLICEL_X1Y0"
            )
            first_packed["clusters"].extend(second_packed["clusters"])
            first_placement["clusters"].extend(second_placement["clusters"])
            first_placement["clusters"].reverse()

            write_xilinx_physical_handoff(
                path, first_packed, first_placement
            )
            packed, placement = open_xilinx_physical_handoff(path)

            self.assertEqual(
                [cluster["id"] for cluster in packed["clusters"]],
                ["openparf:SLICEL_X0Y0", "openparf:SLICEL_X1Y0"],
            )
            self.assertEqual(
                [cluster["cluster"] for cluster in placement["clusters"]],
                ["openparf:SLICEL_X0Y0", "openparf:SLICEL_X1Y0"],
            )

    def test_corruption_and_truncation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sqlite"
            write_xilinx_physical_handoff(source, *_contracts())
            corrupt = root / "corrupt.sqlite"
            shutil.copyfile(source, corrupt)
            with corrupt.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"not-a-sqlite-db!")
            with self.assertRaises(ValidationError):
                validate_xilinx_physical_handoff(
                    corrupt, integrity_check=True
                )

            truncated = root / "truncated.sqlite"
            data = source.read_bytes()
            truncated.write_bytes(data[: max(64, len(data) // 2)])
            with self.assertRaises(ValidationError):
                validate_xilinx_physical_handoff(
                    truncated, integrity_check=True
                )

    def test_relocation_and_mixed_run_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "first" / "physical-handoff.sqlite"
            write_xilinx_physical_handoff(original, *_contracts("cell0"))
            relocated = root / "second" / original.name
            relocated.parent.mkdir()
            original.replace(relocated)
            packed, placement = open_xilinx_physical_handoff(relocated)
            self.assertEqual(
                list(packed["clusters"])[0]["assignments"][0]["instance"],
                "cell0",
            )
            self.assertEqual(
                placement["source"]["packed_sha256"], file_sha256(relocated)
            )

            packed_a, _ = _contracts("cell-a")
            _, placement_b = _contracts("cell-b")
            with self.assertRaisesRegex(ValidationError, "coverage disagrees"):
                write_xilinx_physical_handoff(
                    root / "mixed.sqlite", packed_a, placement_b
                )

            other = root / "other.sqlite"
            write_xilinx_physical_handoff(other, *_contracts("cell-other"))
            packed_first, _ = open_xilinx_physical_handoff(relocated)
            _, placement_other = open_xilinx_physical_handoff(other)
            with self.assertRaisesRegex(ValidationError, "runs disagree"):
                validate_xilinx_physical_handoff_pair(
                    packed_first, placement_other
                )


if __name__ == "__main__":
    unittest.main()
