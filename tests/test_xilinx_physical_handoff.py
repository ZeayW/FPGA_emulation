from tests.physical_handoff_facts import physical_handoff_facts
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.physical.xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from emuflow.physical.xilinx_physical_handoff import (
    XilinxPhysicalHandoffBuilder,
    open_xilinx_physical_handoff,
    validate_xilinx_physical_handoff,
    validate_xilinx_physical_handoff_pair,
)
from emuflow.physical.xilinx_placement import XILINX_PLACEMENT_SCHEMA


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
    @staticmethod
    def _bridge_cluster(index: int):
        site = f"SLICEL_X{index}Y0"
        return {
            "id": f"openparf:{site}", "kind": "slice",
            "site_templates": ["SLICEL"], "control_set": None,
            "assignments": [{
                "instance": f"cell{index}", "cell_type": "LUT1",
                "bel": "A6LUT", "_placement_mode": "SLICEL",
                "_physical_site": site, "_placement_ordinal": 0,
            }],
            "_placement": {
                "site": site, "site_type": "SLICEL", "x": index,
                "y": 0, "fixed": False, "physical_region": None,
            },
        }

    def test_round_trip_exposes_lazy_equivalent_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical-handoff.sqlite"
            packed, placement = _contracts()
            physical_handoff_facts(path, packed, placement)
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
            physical_handoff_facts(path, *_contracts())
            digest = file_sha256(path)

            with mock.patch(
                "emuflow.physical.xilinx_physical_handoff.file_sha256",
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
            first_packed["clusters"].reverse()
            first_placement["clusters"].extend(second_placement["clusters"])

            physical_handoff_facts(
                path, first_packed, first_placement
            )
            packed, placement = open_xilinx_physical_handoff(path)

            self.assertEqual(
                [cluster["id"] for cluster in packed["clusters"]],
                ["openparf:SLICEL_X1Y0", "openparf:SLICEL_X0Y0"],
            )
            self.assertEqual(
                [cluster["cluster"] for cluster in placement["clusters"]],
                ["openparf:SLICEL_X1Y0", "openparf:SLICEL_X0Y0"],
            )

    def test_corruption_and_truncation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sqlite"
            physical_handoff_facts(source, *_contracts())
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
            physical_handoff_facts(original, *_contracts("cell0"))
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

            other = root / "other.sqlite"
            physical_handoff_facts(other, *_contracts("cell-other"))
            packed_first, _ = open_xilinx_physical_handoff(relocated)
            _, placement_other = open_xilinx_physical_handoff(other)
            with self.assertRaisesRegex(ValidationError, "runs disagree"):
                validate_xilinx_physical_handoff_pair(
                    packed_first, placement_other
                )

    def test_incremental_builder_streams_large_repeated_clusters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "physical-handoff.sqlite"
            packed, placement = _contracts()
            packed["clusters"] = ()
            packed["cascade_chains"] = []
            packed["unplaced_constants"] = []
            placement["clusters"] = ()
            cluster_count = 2_048

            with XilinxPhysicalHandoffBuilder(path) as builder:
                for index in range(cluster_count - 1, -1, -1):
                    builder.append_bridge_cluster(self._bridge_cluster(index))
                builder.seal(
                    packed, placement, cascades=[], constants=[]
                )
                report = validate_xilinx_physical_handoff(
                    builder.temporary_path
                )
                self.assertFalse(path.exists())
                builder.publish()

            actual_packed, actual_placement = open_xilinx_physical_handoff(path)
            self.assertEqual(report["packed_clusters"], cluster_count)
            self.assertEqual(report["assignments"], cluster_count)
            self.assertEqual(len(actual_packed["clusters"]), cluster_count)
            self.assertEqual(
                actual_packed["clusters"][0]["id"],
                "openparf:SLICEL_X0Y0",
            )
            self.assertEqual(
                actual_placement["clusters"][0]["cluster"],
                "openparf:SLICEL_X0Y0",
            )

    def test_incremental_builder_failure_removes_unpublished_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "physical-handoff.sqlite"
            with mock.patch(
                "emuflow.physical.xilinx_physical_handoff._BUILDER_COMMIT_ROWS", 1
            ):
                with self.assertRaisesRegex(
                    ValidationError, "failed to append"
                ):
                    with XilinxPhysicalHandoffBuilder(path) as builder:
                        builder.append_bridge_cluster(self._bridge_cluster(0))
                        builder.append_bridge_cluster(self._bridge_cluster(0))

            self.assertFalse(path.exists())
            self.assertEqual(list(root.glob(f".{path.name}.*.tmp")), [])

    def test_incremental_builder_compares_source_ownership_in_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "physical-handoff.sqlite"
            packed, placement = _contracts()
            packed["clusters"] = ()
            packed["cascade_chains"] = []
            packed["unplaced_constants"] = []
            placement["clusters"] = ()
            with XilinxPhysicalHandoffBuilder(path) as builder:
                cluster = self._bridge_cluster(0)
                cluster["assignments"][0]["_source_cluster"] = "source:0"
                builder.append_source_assignment("cell0", "source:0")
                builder.append_bridge_cluster(cluster)
                builder.validate_source_ownership(1)
                builder.seal(packed, placement, cascades=[], constants=[])
                builder.publish()
            self.assertTrue(path.is_file())
            self.assertEqual(list(root.glob(f".{path.name}.source.*.tmp")), [])

    def test_incremental_builder_rejects_source_ownership_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "physical-handoff.sqlite"
            with self.assertRaisesRegex(
                ValidationError, "source cell ownership is invalid"
            ):
                with XilinxPhysicalHandoffBuilder(path) as builder:
                    cluster = self._bridge_cluster(0)
                    cluster["assignments"][0]["_source_cluster"] = "source:other"
                    builder.append_source_assignment("cell0", "source:0")
                    builder.append_bridge_cluster(cluster)
                    builder.validate_source_ownership(1)
            self.assertFalse(path.exists())
            self.assertEqual(list(root.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
