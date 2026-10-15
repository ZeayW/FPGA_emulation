import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.io import read_json, write_json
from emuflow.phase3_storage import (
    COMPRESSED_CLUSTERS_SCHEMA,
    PACKED_ASSIGNMENT_SCHEMA,
    PACKED_CLUSTERS_SCHEMA,
    compress_phase3_clusters,
    pack_phase3_assignment,
    pack_phase3_clusters,
    read_phase3_assignment_view,
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
    def test_compressed_clusters_round_trip_without_column_copy(self) -> None:
        clusters, _ = _large_artifacts()
        compressed = compress_phase3_clusters(clusters)
        self.assertEqual(compressed["schema"], COMPRESSED_CLUSTERS_SCHEMA)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "clusters.json"
            write_json(path, compressed, compact=True)
            self.assertEqual(read_json(path), clusters)

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
        packed["cluster_parts"]["data"] = "not-valid-base64!"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            with self.assertRaisesRegex(ValueError, "index is corrupt"):
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

    def test_downstream_view_does_not_expand_sibling_clusters(self) -> None:
        clusters, assignment = _large_artifacts(4)
        assignment["semantic_contract"] = {
            "schema": "emuflow.static-exact-semantic-contract/v1",
            "mode": "static-exact-combinational",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "assignment.json",
                pack_phase3_assignment(assignment, clusters),
                compact=True,
            )
            view = read_phase3_assignment_view(root / "assignment.json")
            self.assertEqual(view["cut_nets"], assignment["cut_nets"])
            self.assertEqual(
                view["semantic_contract"], assignment["semantic_contract"]
            )
            self.assertNotIn("cluster_assignment", view)
            self.assertNotIn("instance_assignment", view)
            cluster_view = read_phase3_assignment_view(
                root / "assignment.json",
                include_cluster_assignment=True,
            )["cluster_assignment"]
            self.assertEqual(cluster_view["c000003"], "fpga3")
            self.assertEqual(len(cluster_view), 4)

    def test_downstream_view_uses_lazy_sqlite_instance_lookup(self) -> None:
        clusters, assignment = _large_artifacts(4)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "assignment.json",
                pack_phase3_assignment(assignment, clusters),
                compact=True,
            )
            with sqlite3.connect(root / "assignment.sqlite") as connection:
                connection.executescript(
                    """
                    CREATE TABLE parts(
                      part_index INTEGER PRIMARY KEY,
                      fpga TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE copies(
                      source_instance_seq INTEGER NOT NULL,
                      part_index INTEGER NOT NULL,
                      copy_id TEXT NOT NULL,
                      is_replica INTEGER NOT NULL
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO parts VALUES (?,?)",
                    enumerate(["fpga0", "fpga1", "fpga2", "fpga3"]),
                )
                connection.executemany(
                    "INSERT INTO copies VALUES (?,?,?,0)",
                    (
                        (index, index // 2 % 4, instance)
                        for index, instance in enumerate(
                            assignment["instance_assignment"]
                        )
                    ),
                )
            view = read_phase3_assignment_view(
                root / "assignment.json",
                include_instance_assignment=True,
            )
            indexed = view["instance_assignment"]
            self.assertEqual(
                indexed.get("top/logic_block_3/cell_1"), "fpga3"
            )
            self.assertIsNone(indexed.get("absent"))
            indexed.close()


if __name__ == "__main__":
    unittest.main()
