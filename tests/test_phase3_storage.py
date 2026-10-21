import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.io import read_json, write_json
from emuflow.phase3_storage import (
    COMPRESSED_CLUSTERS_SCHEMA,
    PACKED_ASSIGNMENT_SCHEMA,
    PACKED_CLUSTERS_SCHEMA,
    compress_phase3_clusters,
    pack_phase3_assignment,
    pack_phase3_clusters,
    read_phase3_assignment_view,
    read_phase3_cut_timing_view,
    write_compressed_phase3_clusters,
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

    def test_direct_compressed_cluster_writer_is_byte_exact_and_atomic(self) -> None:
        clusters, _ = _large_artifacts()
        clusters["design"] = "large-设计-µ"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference = root / "reference.json"
            direct = root / "direct.json"
            write_json(
                reference,
                compress_phase3_clusters(clusters),
                compact=True,
            )
            write_compressed_phase3_clusters(direct, clusters)
            self.assertEqual(direct.read_bytes(), reference.read_bytes())
            self.assertEqual(read_json(direct), clusters)

            prior = direct.read_bytes()
            with mock.patch(
                "emuflow.phase3_storage.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    write_compressed_phase3_clusters(direct, clusters)
            self.assertEqual(direct.read_bytes(), prior)
            self.assertEqual(
                list(root.glob(f".{direct.name}.*.tmp")),
                [],
            )

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

    def test_previous_v2_assignment_storage_remains_readable(self) -> None:
        clusters, assignment = _large_artifacts(4)
        packed = pack_phase3_assignment(assignment, clusters)
        packed["schema"] = "emuflow.phase3-assignment-storage/v2"
        packed.pop("semantic_contract_index", None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
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
            indexed = read_phase3_assignment_view(
                root / "assignment.json",
                include_semantic_contract=False,
            )
            self.assertTrue(indexed["semantic_contract_indexed"])
            self.assertEqual(
                indexed["semantic_contract_schema"],
                "emuflow.static-exact-combinational-cut/v3",
            )
            self.assertNotIn("semantic_contract", indexed)
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
                    CREATE TABLE metadata(
                      key TEXT PRIMARY KEY,
                      value TEXT NOT NULL
                    );
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
                connection.execute(
                    "INSERT INTO metadata VALUES (?,?)",
                    (
                        "source_instance_count",
                        json.dumps(len(assignment["instance_assignment"])),
                    ),
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
            self.assertEqual(len(indexed), 8)
            self.assertIsNone(indexed.get("absent"))
            indexed.close()

    def test_cut_timing_view_uses_only_indexed_relations(self) -> None:
        clusters, assignment = _large_artifacts(4)
        assignment["cut_nets"] = [
            {
                "net": "n0",
                "cut_class": "combinational",
                "source_fpgas": ["fpga0"],
                "sink_fpgas": ["fpga1"],
            },
            {
                "net": "n1",
                "cut_class": "register_input",
                "source_fpgas": ["fpga1"],
                "sink_fpgas": ["fpga0"],
            },
        ]
        assignment["semantic_contract_sha256"] = "a" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            packed = pack_phase3_assignment(assignment, clusters)
            packed["semantic_contract"] = {
                "codec": "zlib-level-1-base64-canonical-json",
                "uncompressed_bytes": 1,
                "data": "invalid-on-purpose",
            }
            write_json(root / "assignment.json", packed, compact=True)
            with sqlite3.connect(root / "assignment.sqlite") as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT NOT NULL);
                    CREATE TABLE copies(source_instance_seq INTEGER,part_index INTEGER,copy_id TEXT,is_replica INTEGER);
                    CREATE TABLE cut_sinks(net_id TEXT,source_part_index INTEGER,sink_part_index INTEGER);
                    CREATE TABLE exact_cut_nodes(net_id TEXT PRIMARY KEY);
                    CREATE TABLE exact_cut_sources(net_id TEXT,ordinal INTEGER,segment_id TEXT);
                    CREATE TABLE exact_logic_segments(source_cut_net TEXT,part_index INTEGER,segment_id TEXT);
                    """
                )
                metadata = {
                    "schema": "emuflow.phase3-assignment-sqlite/v1",
                    "design": "large",
                    "platform": "four",
                    "provider": "test",
                    "seed": 1,
                    "constraints": {"balance_tolerance": 0.25},
                    "source_instance_count": 8,
                    "semantic_contract_sha256": "a" * 64,
                }
                connection.executemany(
                    "INSERT INTO metadata VALUES (?,?)",
                    ((key, json.dumps(value)) for key, value in metadata.items()),
                )
                connection.executemany(
                    "INSERT INTO parts VALUES (?,?)",
                    [(0, "fpga0"), (1, "fpga1")],
                )
                connection.executemany(
                    "INSERT INTO copies VALUES (?,?,?,0)",
                    [(0, 0, "i0"), (1, 1, "i1")],
                )
                connection.executemany(
                    "INSERT INTO cut_sinks VALUES (?,?,?)",
                    [("n0", 0, 1), ("n1", 1, 0)],
                )
                connection.executemany(
                    "INSERT INTO exact_cut_nodes VALUES (?)",
                    [("n0",), ("n1",)],
                )
                connection.executemany(
                    "INSERT INTO exact_cut_sources VALUES (?,?,?)",
                    [("n0", 0, "segment000000"), ("n1", 0, "segment000001")],
                )
                connection.executemany(
                    "INSERT INTO exact_logic_segments VALUES (?,?,?)",
                    [
                        ("n0", 1, "segment000001"),
                        ("n0", 1, "segment000002"),
                        ("n1", 0, "segment000003"),
                    ],
                )
            view = read_phase3_cut_timing_view(
                root / "assignment.json",
                include_instance_assignment=True,
            )
            self.assertEqual(
                view["cut_nets"],
                [
                    {
                        "net": "n0",
                        "source_fpgas": ["fpga0"],
                        "sink_fpgas": ["fpga1"],
                    },
                    {
                        "net": "n1",
                        "source_fpgas": ["fpga1"],
                        "sink_fpgas": ["fpga0"],
                    },
                ],
            )
            self.assertEqual(view["semantic_contract_sha256"], "a" * 64)
            self.assertNotIn("semantic_contract", view)
            self.assertNotIn("semantic_contract_index", view)
            indexed = view["instance_assignment"]
            self.assertEqual(indexed["i1"], "fpga1")
            indexed.close()
            route_view = read_phase3_assignment_view(
                root / "assignment.json",
                include_semantic_contract=False,
            )
            self.assertEqual(
                [
                    (
                        item["net"],
                        item["dependency_level"],
                        item["combinational_dependency_depth"],
                        item["predecessor_cut_nets"],
                    )
                    for item in route_view["cut_nets"]
                ],
                [
                    ("n0", 1, 1, []),
                    ("n1", 2, 1, ["n0"]),
                ],
            )


if __name__ == "__main__":
    unittest.main()
