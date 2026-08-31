import json
import tempfile
import unittest
from pathlib import Path

from emuflow.io import read_json, write_json
from emuflow.phase3_storage import (
    PACKED_ASSIGNMENT_SCHEMA,
    PACKED_CLUSTERS_SCHEMA,
    pack_phase3_assignment,
    pack_phase3_clusters,
)


def _large_artifacts(count: int = 1000):
    clusters = []
    cluster_assignment = {}
    instance_assignment = {}
    for index in range(count):
        cluster_id = f"c{index:06d}"
        instances = [f"top/logic_block_{index}/cell_{lane}" for lane in range(2)]
        clusters.append(
            {
                "id": cluster_id,
                "instances": instances,
                "resources": {"cells": 2, "lut": 1},
                "fixed_fpga": None,
                "groups": [],
            }
        )
        part = f"fpga{index % 4}"
        cluster_assignment[cluster_id] = part
        instance_assignment.update({instance: part for instance in instances})
    cluster_artifact = {
        "schema": "emuflow.clusters/v1",
        "design": "large",
        "clusters": clusters,
        "instances": count * 2,
        "policy": {"legal_cut_classes": ["register_output"]},
    }
    partitions = []
    for part in [f"fpga{index}" for index in range(4)]:
        ids = sorted(
            cluster_id
            for cluster_id, assigned in cluster_assignment.items()
            if assigned == part
        )
        partitions.append(
            {
                "fpga": part,
                "clusters": ids,
                "cluster_count": len(ids),
                "instance_count": len(ids) * 2,
                "resources": {"cells": len(ids) * 2, "lut": len(ids)},
                "effective_capacity": {"cells": 10000, "lut": 10000},
                "utilization": {"cells": len(ids) / 5000, "lut": len(ids) / 10000},
            }
        )
    assignment = {
        "schema": "emuflow.partition-assignment/v1",
        "design": "large",
        "platform": "four",
        "provider": "test",
        "seed": 1,
        "constraints": {"balance_tolerance": 0.25},
        "cluster_assignment": cluster_assignment,
        "instance_assignment": instance_assignment,
        "partitions": partitions,
        "cut_nets": [],
        "metrics": {"instances": count * 2, "clusters": count},
    }
    return cluster_artifact, assignment


class Phase3StorageTest(unittest.TestCase):
    def test_round_trip_is_lossless_and_materially_smaller(self) -> None:
        clusters, assignment = _large_artifacts()
        packed_clusters = pack_phase3_clusters(clusters)
        packed_assignment = pack_phase3_assignment(assignment, clusters)
        self.assertEqual(packed_clusters["schema"], PACKED_CLUSTERS_SCHEMA)
        self.assertEqual(packed_assignment["schema"], PACKED_ASSIGNMENT_SCHEMA)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(root / "clusters.json", packed_clusters, compact=True)
            write_json(root / "assignment.json", packed_assignment, compact=True)
            self.assertEqual(read_json(root / "clusters.json"), clusters)
            self.assertEqual(read_json(root / "assignment.json"), assignment)
            raw_clusters = len(
                json.dumps(clusters, sort_keys=True, separators=(",", ":"))
            )
            raw_assignment = len(
                json.dumps(assignment, sort_keys=True, separators=(",", ":"))
            )
            self.assertLess(
                (root / "clusters.json").stat().st_size, raw_clusters * 0.70
            )
            self.assertLess(
                (root / "assignment.json").stat().st_size,
                raw_assignment * 0.20,
            )

    def test_assignment_requires_its_exact_cluster_dependency(self) -> None:
        clusters, assignment = _large_artifacts(4)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "assignment.json",
                pack_phase3_assignment(assignment, clusters),
                compact=True,
            )
            with self.assertRaisesRegex(ValueError, "requires sibling"):
                read_json(root / "assignment.json")

    def test_unused_fpga_partition_round_trips(self) -> None:
        clusters, assignment = _large_artifacts(4)
        assignment["partitions"].append(
            {
                "fpga": "fpga4",
                "clusters": [],
                "cluster_count": 0,
                "instance_count": 0,
                "resources": {},
                "effective_capacity": {"cells": 10000, "lut": 10000},
                "utilization": {"cells": 0.0, "lut": 0.0},
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(
                root / "assignment.json",
                pack_phase3_assignment(assignment, clusters),
                compact=True,
            )
            self.assertEqual(read_json(root / "assignment.json"), assignment)

    def test_corrupt_cluster_vector_is_rejected(self) -> None:
        clusters, assignment = _large_artifacts(4)
        packed = pack_phase3_assignment(assignment, clusters)
        packed["cluster_parts"][0] = 99
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            with self.assertRaisesRegex(ValueError, "vector is malformed"):
                read_json(root / "assignment.json")

    def test_corrupt_compressed_semantic_contract_is_rejected(self) -> None:
        clusters, assignment = _large_artifacts(4)
        assignment["semantic_contract"] = {
            "schema": "emuflow.static-exact-semantic-contract/v1",
            "capture_requirements": [
                {"id": f"capture{index:06d}", "kind": "architectural-state"}
                for index in range(100)
            ],
        }
        packed = pack_phase3_assignment(assignment, clusters)
        packed["semantic_contract"]["data"] = "not-valid-base64!"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            with self.assertRaisesRegex(ValueError, "contract is corrupt"):
                read_json(root / "assignment.json")


if __name__ == "__main__":
    unittest.main()
