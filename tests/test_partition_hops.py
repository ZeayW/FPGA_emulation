import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.partition import (
    _partition_hop_distances,
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
    validate_partition_artifacts,
)
from emuflow.partition_hops import refine_partition_hops
from emuflow.phase3 import run_phase3
from emuflow.platform import Platform
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


def _line_platform() -> Platform:
    return Platform.from_dict(
        {
            "schema": "emuflow.boarddb/v1",
            "platform": {"name": "academic_line4", "kind": "virtual"},
            "fpgas": [
                {
                    "id": f"F{index}",
                    "part": "vtr-academic",
                    "utilization_limit": 1.0,
                    "capacity": {"lut": 32, "ff": 32},
                }
                for index in range(4)
            ],
            "links": [
                {
                    "id": f"L{index}{index + 1}",
                    "endpoints": [f"F{index}", f"F{index + 1}"],
                    "direction": "full_duplex",
                    "mode": "abstract",
                    "data_lanes_per_direction": 8,
                    "fabric_clock_mhz": 50,
                    "latency_cycles": 1,
                }
                for index in range(3)
            ],
        }
    )


class HopPartitionRefinementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            raise unittest.SkipTest("a C++17 compiler is required")
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.executable = (
            Path(cls.temporary_directory.name)
            / "emuflow_hop_partition_refiner"
        )
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                str(ROOT / "src/native/hop_partition_refiner.cpp"),
                "-o",
                str(cls.executable),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json",
            top="counter",
            clocks=["clk"],
        )
        self.platform = _line_platform()

    def _artifacts(self, fixed=False):
        raw_constraints = {
            "schema": "emuflow.partition-constraints/v1",
            "min_used_fpgas": 4,
            "balance_tolerance": 1.0,
        }
        if fixed:
            raw_constraints["fixed"] = [
                {"instance": "q_reg[0]", "fpga": "F0"},
                {"instance": "next_lut[0]", "fpga": "F3"},
            ]
        constraints = normalize_partition_constraints(
            raw_constraints, self.ir, self.platform
        )
        clusters = build_clusters(self.ir, constraints)
        cluster_by_instance = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        # Two cells per FPGA, but q_reg[0] -> next_lut[0] crosses three links.
        placement = {
            "q_reg[0]": "F0",
            "q_reg[1]": "F0",
            "q_reg[2]": "F1",
            "q_reg[3]": "F1",
            "next_lut[1]": "F2",
            "next_lut[2]": "F2",
            "next_lut[0]": "F3",
            "next_lut[3]": "F3",
        }
        cluster_assignment = {
            cluster_by_instance[instance]: fpga
            for instance, fpga in placement.items()
        }
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_assignment,
            provider="test-provider",
            seed=7,
        )
        return constraints, clusters, assignment

    def _route_constraints(self, root: Path) -> Path:
        path = root / "route_constraints.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "max_route_hops": 1,
                    "frame_slots": 32,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_native_fm_removes_hop_violations_and_is_reproducible(self) -> None:
        constraints, clusters, assignment = self._artifacts()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            route_constraints = self._route_constraints(root)
            refined, report = refine_partition_hops(
                self.ir,
                self.platform,
                clusters,
                constraints,
                assignment,
                root / "first",
                route_constraints_path=route_constraints,
                executable=str(self.executable),
            )
            repeated, repeated_report = refine_partition_hops(
                self.ir,
                self.platform,
                clusters,
                constraints,
                assignment,
                root / "second",
                route_constraints_path=route_constraints,
                executable=str(self.executable),
            )
        self.assertGreater(report["before"]["violating_net_sink_pairs"], 0)
        self.assertEqual(report["after"]["violating_net_sink_pairs"], 0)
        self.assertGreater(len(report["moves"]), 0)
        self.assertEqual(
            refined["cluster_assignment"], repeated["cluster_assignment"]
        )
        self.assertEqual(report["moves"], repeated_report["moves"])
        self.assertEqual(
            validate_partition_artifacts(
                self.ir, self.platform, clusters, refined
            )["status"],
            "pass",
        )

    def test_native_noop_preserves_materialized_assignment_contract(self) -> None:
        constraints, clusters, assignment = self._artifacts()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            route_constraints = root / "route_constraints.json"
            route_constraints.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.system-route-constraints/v1",
                        "max_route_hops": 3,
                        "frame_slots": 32,
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "emuflow.partition_hops.build_partition_assignment",
                side_effect=AssertionError(
                    "no-op hop proof rebuilt the assignment contract"
                ),
            ):
                refined, report = refine_partition_hops(
                    self.ir,
                    self.platform,
                    clusters,
                    constraints,
                    assignment,
                    root / "noop",
                    route_constraints_path=route_constraints,
                    executable=str(self.executable),
                )
        self.assertEqual(len(report["moves"]), 0)
        self.assertEqual(
            refined["cluster_assignment"], assignment["cluster_assignment"]
        )
        self.assertEqual(
            refined.get("semantic_contract"), assignment.get("semantic_contract")
        )

    def test_candidate_hop_database_preserves_simplex_direction(self) -> None:
        platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {"name": "simplex_line", "kind": "virtual"},
                "fpgas": [
                    {
                        "id": fpga,
                        "part": "academic",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 8, "ff": 8},
                    }
                    for fpga in ("F0", "F1", "F2")
                ],
                "links": [
                    {
                        "id": "L01",
                        "endpoints": ["F0", "F1"],
                        "direction": "unidirectional",
                        "mode": "abstract",
                        "data_lanes_per_direction": 2,
                        "fabric_clock_mhz": 50,
                        "latency_cycles": 1,
                    },
                    {
                        "id": "L12",
                        "endpoints": ["F1", "F2"],
                        "direction": "unidirectional",
                        "mode": "abstract",
                        "data_lanes_per_direction": 2,
                        "fabric_clock_mhz": 50,
                        "latency_cycles": 1,
                    },
                ],
            }
        )
        distances = _partition_hop_distances(
            platform,
            {"unavailable_links": [], "max_route_hops": 2},
        )
        self.assertEqual(distances["F0"]["F2"], 2)
        self.assertIsNone(distances["F2"]["F0"])

    def test_fixed_infeasible_endpoints_fail_before_system_routing(self) -> None:
        constraints, clusters, assignment = self._artifacts(fixed=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(
                ValidationError, "could not satisfy max_route_hops=1"
            ):
                refine_partition_hops(
                    self.ir,
                    self.platform,
                    clusters,
                    constraints,
                    assignment,
                    root / "refinement",
                    route_constraints_path=self._route_constraints(root),
                    executable=str(self.executable),
                )

    def test_native_refiner_rejects_quadratic_large_repair_search(self) -> None:
        cluster_count = 50_001
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "large.in"
            output_path = root / "large.out"
            lines = [
                "EMUFLOW_HOP_PARTITION_REFINER_INPUT_V1",
                f"PARAM 2 {cluster_count} 1 1 1 1",
                "DIST 0 0 0",
                "DIST 0 1 2",
                "DIST 1 0 2",
                "DIST 1 1 0",
                f"BOUND 0 0 {cluster_count} {cluster_count}",
                f"BOUND 1 0 {cluster_count} {cluster_count}",
            ]
            lines.extend(
                f"CLUSTER {index} {0 if index == 0 else 1} -1 1"
                for index in range(cluster_count)
            )
            lines.append(f"NET 0 1 0 1 {cluster_count - 1}")
            input_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            subprocess.run(
                [str(self.executable), str(input_path), str(output_path)],
                check=True,
                timeout=5,
            )
            output = output_path.read_text(encoding="utf-8")
        self.assertIn("STATUS STUCK", output)
        self.assertIn("METRIC initial_violations 1", output)
        self.assertIn("METRIC final_violations 1", output)
        self.assertIn("METRIC scale_guard 1", output)

    def test_native_refiner_repairs_minimum_used_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "min-used.in"
            output_path = root / "min-used.out"
            input_path.write_text(
                "\n".join(
                    [
                        "EMUFLOW_HOP_PARTITION_REFINER_INPUT_V1",
                        "PARAM 3 3 1 0 1 3",
                        "DIST 0 0 0",
                        "DIST 0 1 1",
                        "DIST 0 2 1",
                        "DIST 1 0 1",
                        "DIST 1 1 0",
                        "DIST 1 2 1",
                        "DIST 2 0 1",
                        "DIST 2 1 1",
                        "DIST 2 2 0",
                        "BOUND 0 0 3 3",
                        "BOUND 1 0 3 3",
                        "BOUND 2 0 3 3",
                        "CLUSTER 0 0 -1 1",
                        "CLUSTER 1 0 -1 1",
                        "CLUSTER 2 0 -1 1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                [str(self.executable), str(input_path), str(output_path)],
                check=True,
            )
            output = output_path.read_text(encoding="utf-8")
        self.assertIn("STATUS PASS", output)
        self.assertIn("METRIC initial_used_part_deficit 2", output)
        self.assertIn("METRIC final_used_part_deficit 0", output)
        assignments = {
            int(fields[1]): int(fields[2])
            for line in output.splitlines()
            if (fields := line.split()) and fields[0] == "ASSIGN"
        }
        self.assertEqual(set(assignments.values()), {0, 1, 2})

    def test_native_refiner_rejects_out_of_range_net_sink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "malformed.in"
            output_path = root / "malformed.out"
            input_path.write_text(
                "\n".join(
                    [
                        "EMUFLOW_HOP_PARTITION_REFINER_INPUT_V1",
                        "PARAM 2 2 1 1 1 1",
                        "DIST 0 0 0",
                        "DIST 0 1 1",
                        "DIST 1 0 1",
                        "DIST 1 1 0",
                        "BOUND 0 0 2 2",
                        "BOUND 1 0 2 2",
                        "CLUSTER 0 0 -1 1",
                        "CLUSTER 1 1 -1 1",
                        "NET 0 1 0 1 2",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [str(self.executable), str(input_path), str(output_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("invalid NET sink record", completed.stderr)

    def test_phase3_standalone_applies_route_hop_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            platform_path = root / "board.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            platform_path.write_text(
                json.dumps(self.platform.to_dict()), encoding="utf-8"
            )
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                provider="greedy",
                cut_mode="sequential-only",
                balance_tolerance=1.0,
                route_constraints_path=self._route_constraints(root),
                hop_refiner=str(self.executable),
            )
            assignment = json.loads(
                (root / "phase3/assignment.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["hop_refinement"]["enabled"])
        self.assertEqual(
            report["hop_refinement"]["after"][
                "violating_net_sink_pairs"
            ],
            0,
        )
        self.assertIn(
            "hop_feasibility", assignment["provider_metadata"]
        )

    def test_phase3_supports_relative_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            platform_path = root / "board.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            platform_path.write_text(
                json.dumps(self.platform.to_dict()), encoding="utf-8"
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                report = run_phase3(
                    Path("design.emuir.json"),
                    Path("board.json"),
                    Path("relative-phase3"),
                    provider="greedy",
                    cut_mode="sequential-only",
                    balance_tolerance=1.0,
                    route_constraints_path=self._route_constraints(root),
                    hop_refiner=str(self.executable),
                )
            finally:
                os.chdir(previous)
            assignment_written = (
                root / "relative-phase3/assignment.json"
            ).is_file()
        self.assertEqual(report["status"], "pass")
        self.assertTrue(assignment_written)
        self.assertEqual(
            report["hop_refinement"]["after"][
                "violating_net_sink_pairs"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
