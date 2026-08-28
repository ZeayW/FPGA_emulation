import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.mfspart import MFSPART_HIERARCHY_SCHEMA
from emuflow.mfspart_initial import MFSPART_INITIAL_SCHEMA
from emuflow.mfspart_refine import (
    _normalise_refinement,
    _replay,
    _replay_exhaustive,
    _write_native_input,
    refine_mfspart_hierarchy,
    refine_mfspart_level,
    validate_mfspart_native_certificate,
    validate_mfspart_refinement,
)


ROOT = Path(__file__).resolve().parents[1]


def _line_problem(part_count=3):
    parts = [f"F{index}" for index in range(part_count)]
    distances = {
        source: {
            target: abs(source_index - target_index)
            for target_index, target in enumerate(parts)
        }
        for source_index, source in enumerate(parts)
    }
    capacities = {part: {"cells": 8} for part in parts}
    return parts, distances, capacities


class MFSPartRefinementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            raise unittest.SkipTest("a C++17 compiler is required")
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.executable = (
            Path(cls.temporary_directory.name) / "emuflow_mfspart_refiner"
        )
        cls.checker = (
            Path(cls.temporary_directory.name)
            / "emuflow_mfspart_refiner_checker"
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
                str(ROOT / "src/native/mfspart_refiner.cpp"),
                "-o",
                str(cls.executable),
            ],
            check=True,
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
                str(ROOT / "src/native/mfspart_refiner_checker.cpp"),
                "-o",
                str(cls.checker),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    @staticmethod
    def _violating_graph():
        return {
            "nodes": [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
            ],
            "nets": [{"weight": 1.0, "source": 0, "sinks": [1]}],
        }

    def test_eq9_eq10_move_removes_violation(self) -> None:
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                self._violating_graph(),
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2],
                Path(temporary_directory),
                hmax=1,
                early_stop=1,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["validation"]["status"], "pass")
        self.assertEqual(artifact["assignment"], [0, 0])
        self.assertGreater(artifact["metrics"]["best_cumulative_gain"], 0)
        self.assertEqual(artifact["metrics"]["final_violating_pairs"], 0.0)

    def test_worst_sink_hop_blocks_high_fanout_driver_move_to_edge(self) -> None:
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": 1, "weights": [1]},
            ],
            "nets": [{"weight": 1.0, "source": 0, "sinks": [1, 2, 3, 4, 5]}],
        }
        parts, distances, capacities = _line_problem()
        initial = [1, 2, 2, 2, 0, 1]
        with tempfile.TemporaryDirectory() as temporary_directory:
            without_bottleneck = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                initial,
                Path(temporary_directory) / "pair-only",
                hmax=2,
                early_stop=1,
                bottleneck_beta=0.0,
                executable=str(self.executable),
                checker=str(self.checker),
                python_replay_max_nodes=0,
            )
            with_bottleneck = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                initial,
                Path(temporary_directory) / "worst-sink",
                hmax=2,
                early_stop=1,
                bottleneck_beta=2.0,
                executable=str(self.executable),
                checker=str(self.checker),
            )
        self.assertEqual(without_bottleneck["assignment"][0], 2)
        self.assertEqual(with_bottleneck["assignment"][0], 1)
        self.assertEqual(with_bottleneck["metrics"]["best_prefix"], 0.0)

    def test_immutable_net_guard_rejects_a_locally_profitable_regression(self) -> None:
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
                {"fixed_part": 0, "weights": [1]},
            ],
            "nets": [
                {
                    "weight": 1.0,
                    "bottleneck_weight": 0.0,
                    "max_distance_limit": 1,
                    "source": 0,
                    "sinks": [1, 2, 3, 4],
                }
            ],
        }
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                [1, 2, 2, 2, 0],
                Path(temporary_directory),
                hmax=2,
                early_stop=1,
                bottleneck_beta=0.0,
                executable=str(self.executable),
                checker=str(self.checker),
            )
        self.assertEqual(artifact["assignment"][0], 1)
        self.assertEqual(artifact["metrics"]["best_prefix"], 0.0)
        self.assertEqual(
            artifact["metrics"]["final_topology_guard_violations"], 0.0
        )

    def test_class_weighted_guard_allows_a_valuable_combinational_cut(self) -> None:
        graph = {
            "nodes": [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": 2, "weights": [1]},
            ],
            "nets": [
                {
                    "weight": 10.0,
                    "bottleneck_weight": 10.0,
                    "max_distance_limit": 2,
                    "source": 0,
                    "sinks": [1],
                },
                {
                    "weight": 1.0,
                    "bottleneck_weight": 0.0,
                    "max_distance_limit": -1,
                    "source": 1,
                    "sinks": [2],
                },
            ],
        }
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2, 2],
                Path(temporary_directory),
                hmax=2,
                early_stop=1,
                executable=str(self.executable),
                checker=str(self.checker),
            )
        self.assertEqual(artifact["assignment"], [0, 0, 2])
        self.assertGreater(artifact["metrics"]["best_cumulative_gain"], 0.0)
        self.assertEqual(
            artifact["metrics"]["final_topology_guard_violations"], 0.0
        )

    def test_native_v1_v2_inputs_keep_legacy_bottleneck_semantics(self) -> None:
        parts, distances, capacities = _line_problem()
        problem = _normalise_refinement(
            self._violating_graph(),
            ["cells"],
            parts,
            distances,
            capacities,
            [0, 2],
            hmax=1,
            move_distance=2,
            early_stop=1,
            gamma=15.0,
            violation_lambda=10_000.0,
            mu=0.1,
            bottleneck_beta=0.0,
        )
        for version in (1, 2):
            with self.subTest(version=version):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    input_path = root / "legacy.in"
                    output_path = root / "legacy.out"
                    check_path = root / "legacy.check"
                    _write_native_input(input_path, problem)
                    lines = input_path.read_text(encoding="utf-8").splitlines()
                    lines[0] = f"EMUFLOW_MFSPART_REFINER_INPUT_V{version}"
                    if version == 1:
                        parameter = lines[1].split()
                        self.assertEqual(parameter.pop(), "0")
                        lines[1] = " ".join(parameter)
                    legacy_lines = []
                    for line in lines:
                        fields = line.split()
                        if fields and fields[0] == "NET":
                            # V1/V2 store neither the class-weighted
                            # bottleneck term nor the immutable per-net
                            # topology guard.  The reader must recover the
                            # original weight/-1 defaults.
                            fields = [*fields[:3], *fields[5:]]
                            line = " ".join(fields)
                        legacy_lines.append(line)
                    input_path.write_text(
                        "\n".join(legacy_lines) + "\n", encoding="utf-8"
                    )
                    optimizer = subprocess.run(
                        [str(self.executable), str(input_path), str(output_path)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False,
                    )
                    checker = subprocess.run(
                        [
                            str(self.checker),
                            str(input_path),
                            str(output_path),
                            str(check_path),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False,
                    )
                self.assertEqual(optimizer.returncode, 0, optimizer.stdout)
                self.assertEqual(checker.returncode, 0, checker.stdout)

    def test_best_prefix_rolls_back_negative_moves(self) -> None:
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
            ],
            "nets": [{"weight": 1.0, "source": 0, "sinks": [1]}],
        }
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 0],
                Path(temporary_directory),
                hmax=1,
                early_stop=1,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["assignment"], [0, 0])
        self.assertGreaterEqual(len(artifact["moves"]), 1)
        self.assertEqual(sum(move["kept"] for move in artifact["moves"]), 0)
        self.assertEqual(artifact["metrics"]["best_prefix"], 0.0)

    def test_capacity_blocks_otherwise_profitable_move(self) -> None:
        parts, distances, capacities = _line_problem()
        capacities["F0"]["cells"] = 1
        capacities["F1"]["cells"] = 1
        graph = {
            "nodes": [
                {"fixed_part": 0, "weights": [1]},
                {"fixed_part": -1, "weights": [2]},
                {"fixed_part": 1, "weights": [1]},
            ],
            "nets": [{"weight": 1.0, "source": 0, "sinks": [1]}],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2, 1],
                Path(temporary_directory),
                hmax=1,
                early_stop=1,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["assignment"][1], 2)
        self.assertEqual(artifact["metrics"]["final_capacity_violations"], 0.0)

    def test_independent_oracle_rejects_corrupt_gain(self) -> None:
        parts, distances, capacities = _line_problem()
        problem = _normalise_refinement(
            self._violating_graph(),
            ["cells"],
            parts,
            distances,
            capacities,
            [0, 2],
            hmax=1,
            move_distance=2,
            early_stop=1,
            gamma=15.0,
            violation_lambda=10_000.0,
            mu=0.1,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                self._violating_graph(),
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2],
                Path(temporary_directory),
                hmax=1,
                early_stop=1,
                executable=str(self.executable),
            )
        corrupt = copy.deepcopy(artifact)
        corrupt["moves"][0]["gain"] += 1.0
        with self.assertRaisesRegex(ValidationError, "replay mismatch"):
            validate_mfspart_refinement(corrupt, problem)

    def test_uncoarsening_projects_each_level_before_refinement(self) -> None:
        fine = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
                {"fixed_part": -1, "weights": [1]},
            ],
            "nets": [
                {"weight": 1.0, "source": 0, "sinks": [2]},
                {"weight": 1.0, "source": 1, "sinks": [3]},
            ],
        }
        coarse = {
            "nodes": [
                {"fixed_part": -1, "weights": [2]},
                {"fixed_part": -1, "weights": [2]},
            ],
            "nets": [{"weight": 2.0, "source": 0, "sinks": [1]}],
        }
        hierarchy = {
            "schema": MFSPART_HIERARCHY_SCHEMA,
            "dimensions": ["cells"],
            "levels": [fine, coarse],
            "fine_to_coarse": [{0: 0, 1: 0, 2: 1, 3: 1}],
        }
        initial = {
            "schema": MFSPART_INITIAL_SCHEMA,
            "assignment": {0: 0, 1: 2},
        }
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_hierarchy(
                hierarchy,
                initial,
                parts,
                distances,
                capacities,
                Path(temporary_directory),
                hmax=1,
                early_stop_fraction=0.5,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["validation"]["status"], "pass")
        self.assertEqual(artifact["validation"]["refined_levels"], 2)
        self.assertEqual(len(artifact["assignment"]), 4)

    def test_sparse_incremental_gain_avoids_full_rescan(self) -> None:
        node_count = 2_000
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]}
                for _ in range(node_count)
            ],
            "nets": [
                {
                    "weight": 1.0,
                    "source": node,
                    "sinks": [node + 1],
                }
                for node in range(0, node_count, 2)
            ],
        }
        parts = ["F0", "F1"]
        distances = {
            "F0": {"F0": 0, "F1": 1},
            "F1": {"F0": 1, "F1": 0},
        }
        capacities = {part: {"cells": 5_000} for part in parts}
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                [0] * node_count,
                Path(temporary_directory),
                hmax=1,
                early_stop=20,
                executable=str(self.executable),
            )
        self.assertEqual(artifact["metrics"]["attempted_moves"], 20.0)
        self.assertLess(
            artifact["metrics"]["candidate_recomputations"], 3_000
        )
        self.assertEqual(artifact["validation"]["status"], "pass")

    def test_incremental_oracle_matches_exhaustive_global_best(self) -> None:
        parts, distances, capacities = _line_problem()
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [weight]}
                for weight in [1, 2, 1, 3, 1, 2]
            ],
            "nets": [
                {
                    "weight": 2.0,
                    "bottleneck_weight": 0.0,
                    "max_distance_limit": 1,
                    "source": 0,
                    "sinks": [1, 2],
                },
                {
                    "weight": 1.0,
                    "max_distance_limit": 0,
                    "source": 2,
                    "sinks": [3],
                },
                {"weight": 3.0, "source": 4, "sinks": [1, 5]},
            ],
        }
        problem = _normalise_refinement(
            graph,
            ["cells"],
            parts,
            distances,
            capacities,
            [0, 0, 1, 1, 2, 2],
            hmax=1,
            move_distance=2,
            early_stop=6,
            gamma=15.0,
            violation_lambda=10_000.0,
            mu=0.1,
        )
        incremental_moves, incremental_assignment, incremental_metrics = _replay(problem)
        exhaustive_moves, exhaustive_assignment, exhaustive_metrics = _replay_exhaustive(problem)
        self.assertEqual(incremental_moves, exhaustive_moves)
        self.assertEqual(incremental_assignment, exhaustive_assignment)
        for name, value in exhaustive_metrics.items():
            self.assertEqual(incremental_metrics[name], value)

    def test_native_orthant_certificate_matches_small_python_replay(self) -> None:
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                self._violating_graph(),
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2],
                Path(temporary_directory),
                hmax=1,
                early_stop=2,
                executable=str(self.executable),
                checker=str(self.checker),
                python_replay_max_nodes=0,
            )
        self.assertEqual(
            artifact["validation"]["mode"],
            "native-orthant-global-best-certificate",
        )
        self.assertEqual(artifact["assignment"], [0, 0])
        self.assertIn("checker_output_sha256", artifact["artifacts"])

    def test_read_only_native_certificate_replay_rejects_tampering(self) -> None:
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = refine_mfspart_level(
                self._violating_graph(),
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2],
                root,
                hmax=1,
                early_stop=2,
                executable=str(self.executable),
                checker=str(self.checker),
                python_replay_max_nodes=0,
            )
            certificate = validate_mfspart_native_certificate(
                root / "mfspart_refiner.in",
                root / "mfspart_refiner.out",
                checker=str(self.checker),
            )
            self.assertEqual(
                certificate["parsed"]["assignment"], artifact["assignment"]
            )
            self.assertEqual(
                certificate["input_evidence"],
                {
                    "native_header": "EMUFLOW_MFSPART_REFINER_INPUT_V3",
                    "guarded_nets": 0,
                    "zero_bottleneck_nets": 0,
                },
            )
            output = root / "mfspart_refiner.out"
            lines = output.read_text(encoding="utf-8").splitlines()
            move = next(
                index for index, line in enumerate(lines)
                if line.startswith("MOVE ")
            )
            fields = lines[move].split()
            fields[2] = "0"
            lines[move] = " ".join(fields)
            output.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "checker rejected refinement"
            ):
                validate_mfspart_native_certificate(
                    root / "mfspart_refiner.in",
                    output,
                    checker=str(self.checker),
                )

    def test_native_certificate_rejects_non_global_move(self) -> None:
        parts, distances, capacities = _line_problem()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            refine_mfspart_level(
                self._violating_graph(),
                ["cells"],
                parts,
                distances,
                capacities,
                [0, 2],
                root,
                hmax=1,
                early_stop=2,
                executable=str(self.executable),
                checker=str(self.checker),
                python_replay_max_nodes=0,
            )
            output = root / "mfspart_refiner.out"
            lines = output.read_text(encoding="utf-8").splitlines()
            move = next(index for index, line in enumerate(lines) if line.startswith("MOVE "))
            fields = lines[move].split()
            fields[2] = "0"
            lines[move] = " ".join(fields)
            output.write_text("\n".join(lines) + "\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    str(self.checker),
                    str(root / "mfspart_refiner.in"),
                    str(output),
                    str(root / "tampered.check"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("global-best certificate mismatch", completed.stdout)

    def test_incremental_oracle_scales_to_100k_sparse_nodes(self) -> None:
        node_count = 100_000
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]}
                for _ in range(node_count)
            ],
            "nets": [
                {"weight": 1.0, "source": node, "sinks": [node + 1]}
                for node in range(0, node_count, 2)
            ],
        }
        parts = ["F0", "F1"]
        problem = _normalise_refinement(
            graph,
            ["cells"],
            parts,
            {"F0": {"F0": 0, "F1": 1}, "F1": {"F0": 1, "F1": 0}},
            {part: {"cells": 200_000} for part in parts},
            [0] * node_count,
            hmax=1,
            move_distance=2,
            early_stop=20,
            gamma=15.0,
            violation_lambda=10_000.0,
            mu=0.1,
        )
        moves, _, metrics = _replay(problem)
        self.assertEqual(len(moves), 20)
        self.assertLess(metrics["oracle_candidate_recomputations"], 101_000)

    def test_native_certificate_scales_to_100k_sparse_nodes(self) -> None:
        node_count = 100_000
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]}
                for _ in range(node_count)
            ],
            "nets": [
                {"weight": 1.0, "source": node, "sinks": [node + 1]}
                for node in range(0, node_count, 2)
            ],
        }
        parts = ["F0", "F1"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                {"F0": {"F0": 0, "F1": 1}, "F1": {"F0": 1, "F1": 0}},
                {part: {"cells": 200_000} for part in parts},
                [0] * node_count,
                Path(temporary_directory),
                hmax=1,
                early_stop=20,
                executable=str(self.executable),
                checker=str(self.checker),
                python_replay_max_nodes=0,
            )
        self.assertEqual(artifact["validation"]["attempted_moves"], 20)
        self.assertLess(
            artifact["validation"]["checker_orthant_tree_nodes_visited"],
            1_000,
        )

    def test_high_fanout_connectivity_uses_indexed_part_counts(self) -> None:
        node_count = 10_000
        graph = {
            "nodes": [
                {"fixed_part": -1, "weights": [1]}
                for _ in range(node_count)
            ],
            "nets": [
                {
                    "weight": 1.0,
                    "source": 0,
                    "sinks": list(range(1, node_count)),
                }
            ],
        }
        parts = ["F0", "F1"]
        distances = {
            "F0": {"F0": 0, "F1": 1},
            "F1": {"F0": 1, "F1": 0},
        }
        capacities = {part: {"cells": 20_000} for part in parts}
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = refine_mfspart_level(
                graph,
                ["cells"],
                parts,
                distances,
                capacities,
                [0] * node_count,
                Path(temporary_directory),
                hmax=1,
                early_stop=3,
                executable=str(self.executable),
                checker=str(self.checker),
            )
        self.assertEqual(artifact["validation"]["status"], "pass")
        self.assertLess(artifact["metrics"]["candidate_recomputations"], 50_000)


if __name__ == "__main__":
    unittest.main()
