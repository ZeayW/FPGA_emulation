import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.mfspart import MFSPART_HIERARCHY_SCHEMA
from emuflow.mfspart_initial import (
    _expected,
    _expected_exhaustive,
    _normalise_problem,
    _sequential_float_sum,
    build_mfspart_initial_partition,
    exhaustively_enumerate_mfspart_assignments,
    validate_mfspart_initial_partition,
)


ROOT = Path(__file__).resolve().parents[1]


def _hierarchy(nodes, nets):
    return {
        "schema": MFSPART_HIERARCHY_SCHEMA,
        "dimensions": ["cells"],
        "levels": [{"nodes": nodes, "nets": nets}],
    }


def _line_problem():
    parts = ["F0", "F1", "F2"]
    distances = {
        "F0": {"F0": 0, "F1": 1, "F2": 2},
        "F1": {"F0": 1, "F1": 0, "F2": 1},
        "F2": {"F0": 2, "F1": 1, "F2": 0},
    }
    capacities = {part: {"cells": 8} for part in parts}
    degrees = {"F0": 1.0, "F1": 2.0, "F2": 1.0}
    return parts, distances, capacities, degrees


class MFSPartInitialPartitionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            raise unittest.SkipTest("a C++17 compiler is required")
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.executable = (
            Path(cls.temporary_directory.name) / "emuflow_mfspart_initializer"
        )
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                str(ROOT / "src/native/mfspart_initializer.cpp"),
                "-o",
                str(cls.executable),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_delayed_propagation_and_eq5_to_eq7_replay(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
            ],
            [
                {"weight": 2.0, "source": 0, "sinks": [1]},
                {"weight": 2.0, "source": 1, "sinks": [2]},
            ],
        )
        parts, distances, capacities, degrees = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                degrees,
                Path(temporary_directory),
                hmax=1,
                seed=19,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["validation"]["status"], "pass")
        self.assertEqual(artifact["candidate_parts"][1], [1])
        self.assertEqual(artifact["assignment"][1], 1)
        self.assertEqual(artifact["validation"]["phase_counts"], {0: 2, 1: 1, 2: 0})
        self.assertEqual(artifact["metrics"]["violating_pairs"], 0.0)
        problem = _normalise_problem(
            hierarchy,
            parts,
            distances,
            capacities,
            degrees,
            hmax=1,
            seed=19,
            theta=1.0,
            eta=1.0,
            violation_lambda=1.0,
            mu=1.0,
            temperature=1.0,
        )
        exhaustive = exhaustively_enumerate_mfspart_assignments(problem)
        self.assertEqual(exhaustive["enumerated_assignments"], 3)
        self.assertEqual(exhaustive["topology_feasible_assignments"], 1)
        self.assertEqual(exhaustive["best_assignment"], [0, 1, 2])

    def test_incremental_propagation_matches_exhaustive_replay(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": -1, "weights": [weight]}
                for weight in [1, 2, 1, 1, 2, 1]
            ],
            [
                {"weight": 2.0, "source": 0, "sinks": [1, 2]},
                {"weight": 1.0, "source": 2, "sinks": [3]},
                {"weight": 3.0, "source": 4, "sinks": [3, 5]},
            ],
        )
        parts, distances, capacities, degrees = _line_problem()
        problem = _normalise_problem(
            hierarchy,
            parts,
            distances,
            capacities,
            degrees,
            hmax=1,
            seed=23,
            theta=1.0,
            eta=1.0,
            violation_lambda=1.0,
            mu=1.0,
            temperature=1.0,
        )
        self.assertEqual(_expected(problem), _expected_exhaustive(problem))

    def test_sparse_10k_initialization_uses_local_priority_updates(self) -> None:
        node_count = 10_000
        hierarchy = _hierarchy(
            [
                {"fixed_part": -1, "weights": [1]}
                for _ in range(node_count)
            ],
            [
                {"weight": 1.0, "source": node, "sinks": [node + 1]}
                for node in range(0, node_count, 2)
            ],
        )
        parts = ["F0", "F1"]
        distances = {
            "F0": {"F0": 0, "F1": 1},
            "F1": {"F0": 1, "F1": 0},
        }
        capacities = {part: {"cells": node_count * 2} for part in parts}
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                {"F0": 1.0, "F1": 1.0},
                Path(temporary_directory),
                hmax=1,
                seed=31,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["validation"]["status"], "pass")
        self.assertEqual(artifact["validation"]["phase_counts"][2], 0)
        self.assertLess(artifact["metrics"]["priority_recomputations"], 40_000)

    def test_records_every_candidate_domain_contraction(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
            ],
            [
                {"weight": 2.0, "source": 0, "sinks": [1]},
                {"weight": 1.0, "source": 1, "sinks": [2]},
            ],
        )
        parts, distances, capacities, degrees = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                degrees,
                Path(temporary_directory),
                hmax=1,
                seed=7,
                theta=0.0,
                eta=0.0,
                temperature=0.01,
                executable=str(self.executable),
            )
        self.assertIn(
            {"assignment_step": 1, "node": 2, "parts": [0, 1]},
            artifact["domain_trace"],
        )
        self.assertEqual(artifact["validation"]["status"], "pass")

    def test_phase2_uses_eq8_when_candidate_capacity_is_exhausted(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [2]},
            ],
            [{"weight": 3.0, "source": 0, "sinks": [1]}],
        )
        parts, distances, capacities, degrees = _line_problem()
        capacities["F0"]["cells"] = 1
        capacities["F1"]["cells"] = 1
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                degrees,
                Path(temporary_directory),
                hmax=1,
                seed=3,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["assignment"][1], 2)
        self.assertEqual(artifact["validation"]["phase_counts"][2], 1)
        self.assertEqual(artifact["validation"]["violating_pairs"], 1)

    def test_hmax_two_generalizes_candidate_radius(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
            ],
            [{"weight": 1.0, "source": 0, "sinks": [1]}],
        )
        parts, distances, capacities, degrees = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                degrees,
                Path(temporary_directory),
                hmax=2,
                seed=11,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["candidate_parts"][1], [0, 1, 2])
        self.assertEqual(artifact["validation"]["status"], "pass")

    def test_nonfixed_case_selects_normalized_degree_anchor(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": -1, "weights": [4]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": -1, "weights": [2]},
            ],
            [
                {"weight": 2.0, "source": 0, "sinks": [1]},
                {"weight": 2.0, "source": 1, "sinks": [2]},
            ],
        )
        parts, distances, capacities, degrees = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                degrees,
                Path(temporary_directory),
                hmax=1,
                seed=5,
                executable=str(self.executable),
            )
        first = artifact["assignment_trace"][0]
        self.assertEqual((first["node"], first["part"], first["phase"]), (1, 1, 0))

    def test_oracle_uses_native_ordered_float_accumulation(self) -> None:
        self.assertEqual(_sequential_float_sum([1e16, 1.0, 1.0]), 1e16)
        hierarchy = _hierarchy(
            [
                {"fixed_part": -1, "weights": [1]}
                for _ in range(8)
            ],
            [
                {"weight": 1e16, "source": 0, "sinks": [4]},
                {"weight": 1e16, "source": 1, "sinks": [5]},
                {"weight": 1.0, "source": 1, "sinks": [6]},
                {"weight": 1.0, "source": 1, "sinks": [7]},
            ],
        )
        parts = ["F0", "F1"]
        distances = {
            "F0": {"F0": 0, "F1": 1},
            "F1": {"F0": 1, "F1": 0},
        }
        capacities = {part: {"cells": 16} for part in parts}
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                {"F0": 1.0, "F1": 1.0},
                Path(temporary_directory),
                hmax=1,
                seed=37,
                executable=str(self.executable),
            )
        first = artifact["assignment_trace"][0]
        self.assertEqual((first["node"], first["phase"]), (0, 0))
        self.assertEqual(artifact["validation"]["status"], "pass")

    def test_oracle_rejects_corrupt_probabilistic_assignment(self) -> None:
        hierarchy = _hierarchy(
            [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
            ],
            [
                {"weight": 2.0, "source": 0, "sinks": [1]},
                {"weight": 2.0, "source": 1, "sinks": [2]},
            ],
        )
        parts, distances, capacities, degrees = _line_problem()
        problem = _normalise_problem(
            hierarchy,
            parts,
            distances,
            capacities,
            degrees,
            hmax=1,
            seed=19,
            theta=1.0,
            eta=1.0,
            violation_lambda=1.0,
            mu=1.0,
            temperature=1.0,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = build_mfspart_initial_partition(
                hierarchy,
                parts,
                distances,
                capacities,
                degrees,
                Path(temporary_directory),
                hmax=1,
                seed=19,
                executable=str(self.executable),
            )
        corrupt = copy.deepcopy(artifact)
        corrupt["assignment_trace"][-1]["part"] = 0
        with self.assertRaisesRegex(ValidationError, "replay mismatch"):
            validate_mfspart_initial_partition(corrupt, problem)


if __name__ == "__main__":
    unittest.main()
