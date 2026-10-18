import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    CUT_MODE_STATIC_EXACT,
    build_clusters,
    normalize_partition_constraints,
)
from emuflow.phase3 import run_phase3
from emuflow.platform import Platform
from emuflow.tritonpart import (
    TRITONPART_INPUT_SCHEMA,
    _repair_min_used_fpgas,
    _repair_multi_resource_balance_reference,
    _run_native_balance_legalizer,
    _tritonpart_ubfactor_percent_points,
    export_tritonpart_inputs,
    parse_tritonpart_solution,
    run_tritonpart,
)
from emuflow.yosys import import_yosys_json
from tests.native_build import tritonpart_balance_legalizer


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"
FOUR_FPGA_PLATFORM_PATH = (
    ROOT / "platforms" / "virtual" / "xcvu3p_4fpga_mesh.json"
)


class TritonPartTest(unittest.TestCase):
    @staticmethod
    def _write_native_balance_case(
        root: Path,
        weights,
        initial,
        allowed,
        fixed=None,
    ):
        dimensions = ["cells", "lut"]
        (root / "partition.hgr").write_text(
            "\n".join([
                f"1 {len(weights)} 11",
                f"1 1 {len(weights)}",
                *(" ".join(str(value) for value in row) for row in weights),
                "",
            ]),
            encoding="utf-8",
        )
        (root / "partition.fix").write_text(
            "\n".join(str(value) for value in (fixed or [-1] * len(weights)))
            + "\n",
            encoding="utf-8",
        )
        (root / "partition.edge-classes").write_text("0\n", encoding="utf-8")
        solution = root / "partition.hgr.part.2"
        solution.write_text(
            "\n".join(str(value) for value in initial) + "\n",
            encoding="utf-8",
        )
        artifact = {
            "cluster_order": [f"c{index}" for index in range(len(weights))],
            "fpga_order": ["fpga0", "fpga1"],
            "vertex_dimensions": dimensions,
            "vertex_weights": weights,
            "balance_allowed_loads": {
                f"fpga{part}": dict(zip(dimensions, row))
                for part, row in enumerate(allowed)
            },
            "files": {
                "hypergraph": "partition.hgr",
                "fixed": "partition.fix",
                "edge_classes": "partition.edge-classes",
            },
        }
        candidate = {
            cluster: f"fpga{part}"
            for cluster, part in zip(artifact["cluster_order"], initial)
        }
        return artifact, candidate, solution

    def test_native_balance_legalizer_executes_direct_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact, candidate, solution = self._write_native_balance_case(
                root,
                [[1, 2], [1, 1], [1, 1]],
                [0, 0, 1],
                [[2, 2], [2, 2]],
            )
            repaired, report = _run_native_balance_legalizer(
                candidate,
                artifact,
                root,
                solution,
                root / "repaired.part.2",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(report["direct_moves"], 1)
        self.assertEqual(report["paired_sequences"], 0)
        self.assertEqual(report["moves"], 1)
        self.assertEqual(repaired["c1"], "fpga1")

    def test_native_balance_legalizer_preserves_legal_solution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact, candidate, solution = self._write_native_balance_case(
                root,
                [[1, 2], [1, 1], [1, 1]],
                [0, 1, 1],
                [[2, 2], [2, 2]],
            )
            repaired, report = _run_native_balance_legalizer(
                candidate,
                artifact,
                root,
                solution,
                root / "repaired.part.2",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(report["moves"], 0)
        self.assertEqual(repaired, candidate)

    def test_native_balance_legalizer_prioritizes_scarce_resource_relief(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "partition.hgr").write_text(
                "2 4 11\n100 3 4\n1 1 2\n1 1\n1 1\n1 0\n1 0\n",
                encoding="utf-8",
            )
            (root / "partition.fix").write_text("-1\n" * 4, encoding="utf-8")
            (root / "partition.edge-classes").write_text("0\n0\n", encoding="utf-8")
            solution = root / "partition.hgr.part.2"
            solution.write_text("0\n0\n0\n1\n", encoding="utf-8")
            artifact = {
                "cluster_order": ["c0", "c1", "c2", "c3"],
                "fpga_order": ["fpga0", "fpga1"],
                "vertex_dimensions": ["cells", "dsp48"],
                "vertex_weights": [[1, 1], [1, 1], [1, 0], [1, 0]],
                "balance_allowed_loads": {
                    "fpga0": {"cells": 2, "dsp48": 1},
                    "fpga1": {"cells": 2, "dsp48": 1},
                },
                "files": {
                    "hypergraph": "partition.hgr",
                    "fixed": "partition.fix",
                    "edge_classes": "partition.edge-classes",
                },
            }
            repaired, report = _run_native_balance_legalizer(
                {
                    "c0": "fpga0",
                    "c1": "fpga0",
                    "c2": "fpga0",
                    "c3": "fpga1",
                },
                artifact,
                root,
                solution,
                root / "repaired.part.2",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(report["direct_moves"], 1)
        self.assertEqual(report["paired_sequences"], 0)
        self.assertEqual(repaired["c2"], "fpga0")
        self.assertEqual(
            sum(repaired[cluster] == "fpga1" for cluster in ("c0", "c1")),
            1,
        )

    def test_native_balance_legalizer_tries_all_direct_sources_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "partition.hgr").write_text(
                "1 6 11\n1 1 6\n"
                "1 0 1\n1 0 1\n"
                "1 1 0\n1 1 0\n1 1 0\n"
                "1 0 1\n",
                encoding="utf-8",
            )
            (root / "partition.fix").write_text("-1\n" * 6, encoding="utf-8")
            (root / "partition.edge-classes").write_text("0\n", encoding="utf-8")
            solution = root / "partition.hgr.part.3"
            solution.write_text("0\n0\n1\n1\n1\n2\n", encoding="utf-8")
            artifact = {
                "cluster_order": [f"c{index}" for index in range(6)],
                "fpga_order": ["fpga0", "fpga1", "fpga2"],
                "vertex_dimensions": ["cells", "lut", "dsp48"],
                "vertex_weights": [
                    [1, 0, 1], [1, 0, 1],
                    [1, 1, 0], [1, 1, 0], [1, 1, 0],
                    [1, 0, 1],
                ],
                "balance_allowed_loads": {
                    fpga: {"cells": 4, "lut": 2, "dsp48": 1}
                    for fpga in ("fpga0", "fpga1", "fpga2")
                },
                "files": {
                    "hypergraph": "partition.hgr",
                    "fixed": "partition.fix",
                    "edge_classes": "partition.edge-classes",
                },
            }
            initial = {
                cluster: fpga
                for cluster, fpga in zip(
                    artifact["cluster_order"],
                    ["fpga0", "fpga0", "fpga1", "fpga1", "fpga1", "fpga2"],
                )
            }
            _, report = _run_native_balance_legalizer(
                initial,
                artifact,
                root,
                solution,
                root / "repaired.part.3",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(report["direct_moves"], 2)
        self.assertEqual(report["paired_sequences"], 0)
        self.assertEqual(report["ejection_chain_sequences"], 0)

    def test_native_balance_legalizer_fails_without_relaxing_fixed_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact, candidate, solution = self._write_native_balance_case(
                root,
                [[1, 2], [1, 1], [1, 1]],
                [0, 0, 1],
                [[2, 2], [2, 2]],
                fixed=[0, 0, -1],
            )
            with self.assertRaisesRegex(
                ValidationError, "native TritonPart balance legalizer failed"
            ):
                _run_native_balance_legalizer(
                    candidate,
                    artifact,
                    root,
                    solution,
                    root / "repaired.part.2",
                    executable=str(tritonpart_balance_legalizer()),
                    timeout_seconds=30,
                )
            report = (root / "balance-repair-report.txt").read_text()
        self.assertIn("STATUS FAILED", report)
        self.assertIn("ALLOWED 0 2 2", report)
        self.assertIn("ALLOWED 1 2 2", report)

    def test_native_balance_legalizer_rejects_false_reported_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact, candidate, solution = self._write_native_balance_case(
                root,
                [[1, 2], [1, 1], [1, 1]],
                [0, 1, 1],
                [[2, 2], [2, 2]],
            )
            real_run = subprocess.run

            def tampering_run(command, **kwargs):
                completed = real_run(command, **kwargs)
                report_path = Path(command[-1])
                report_path.write_text(
                    report_path.read_text().replace(
                        "FINAL 0 1 2", "FINAL 0 0 0"
                    )
                )
                return completed

            with mock.patch(
                "emuflow.tritonpart.subprocess.run",
                side_effect=tampering_run,
            ), self.assertRaisesRegex(
                ValidationError, "final loads do not match assignment"
            ):
                _run_native_balance_legalizer(
                    candidate,
                    artifact,
                    root,
                    solution,
                    root / "repaired.part.2",
                    executable=str(tritonpart_balance_legalizer()),
                    timeout_seconds=30,
                )

    def test_native_balance_legalizer_executes_paired_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "partition.hgr").write_text(
                "1 4 11\n1 1 3\n1 2\n1 4\n1 1\n1 3\n",
                encoding="utf-8",
            )
            (root / "partition.fix").write_text("-1\n" * 4, encoding="utf-8")
            (root / "partition.edge-classes").write_text("0\n", encoding="utf-8")
            solution = root / "partition.hgr.part.2"
            solution.write_text("0\n0\n1\n1\n", encoding="utf-8")
            artifact = {
                "cluster_order": ["c0", "c1", "c2", "c3"],
                "fpga_order": ["fpga0", "fpga1"],
                "vertex_dimensions": ["cells", "lut"],
                "vertex_weights": [[1, 2], [1, 4], [1, 1], [1, 3]],
                "balance_allowed_loads": {
                    "fpga0": {"cells": 2, "lut": 5},
                    "fpga1": {"cells": 2, "lut": 5},
                },
                "files": {
                    "hypergraph": "partition.hgr",
                    "fixed": "partition.fix",
                    "edge_classes": "partition.edge-classes",
                },
            }
            repaired, report = _run_native_balance_legalizer(
                {"c0": "fpga0", "c1": "fpga0", "c2": "fpga1", "c3": "fpga1"},
                artifact,
                root,
                solution,
                root / "repaired.part.2",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(report["provider"], "emuflow-native-tritonpart-balance-legalizer-v1")
        self.assertEqual(report["paired_sequences"], 1)
        self.assertEqual(report["moves"], 2)
        self.assertEqual(repaired, {
            "c0": "fpga1", "c1": "fpga0", "c2": "fpga0", "c3": "fpga1"
        })

    def test_native_balance_legalizer_executes_three_move_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "partition.hgr").write_text(
                "1 3 11\n1 1 2 3\n"
                "1 3 0 0\n1 0 3 0\n1 0 0 3\n",
                encoding="utf-8",
            )
            (root / "partition.fix").write_text("-1\n" * 3, encoding="utf-8")
            (root / "partition.edge-classes").write_text("0\n", encoding="utf-8")
            solution = root / "partition.hgr.part.3"
            solution.write_text("0\n1\n2\n", encoding="utf-8")
            artifact = {
                "cluster_order": ["x", "y", "z"],
                "fpga_order": ["fpga0", "fpga1", "fpga2"],
                "vertex_dimensions": ["cells", "lut", "ff", "dsp48"],
                "vertex_weights": [
                    [1, 3, 0, 0],
                    [1, 0, 3, 0],
                    [1, 0, 0, 3],
                ],
                "balance_allowed_loads": {
                    "fpga0": {"cells": 1, "lut": 1, "ff": 1, "dsp48": 3},
                    "fpga1": {"cells": 1, "lut": 3, "ff": 1, "dsp48": 1},
                    "fpga2": {"cells": 1, "lut": 1, "ff": 3, "dsp48": 1},
                },
                "files": {
                    "hypergraph": "partition.hgr",
                    "fixed": "partition.fix",
                    "edge_classes": "partition.edge-classes",
                },
            }
            repaired, report = _run_native_balance_legalizer(
                {"x": "fpga0", "y": "fpga1", "z": "fpga2"},
                artifact,
                root,
                solution,
                root / "repaired.part.3",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(
            repaired,
            {"x": "fpga1", "y": "fpga2", "z": "fpga0"},
        )
        self.assertEqual(report["ejection_chain_sequences"], 1)
        self.assertEqual(report["max_ejection_chain_moves"], 3)
        self.assertEqual(report["moves"], 3)

    def test_native_balance_legalizer_batches_identical_chain_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            weights = (
                [[1, 20, 1], [1, 0, 1]]
                + [[1, 1, 0]] * 20
                + [[1, 0, 1]]
            )
            vertex_count = len(weights)
            (root / "partition.hgr").write_text(
                f"1 {vertex_count} 11\n"
                + "1 " + " ".join(str(index) for index in range(1, vertex_count + 1))
                + "\n"
                + "".join(
                    " ".join(str(value) for value in row) + "\n"
                    for row in weights
                ),
                encoding="utf-8",
            )
            fixed = [-1, 0] + [-1] * 20 + [2]
            initial = [0, 0] + [1] * 20 + [2]
            (root / "partition.fix").write_text(
                "\n".join(str(value) for value in fixed) + "\n",
                encoding="utf-8",
            )
            (root / "partition.edge-classes").write_text("0\n", encoding="utf-8")
            solution = root / "partition.hgr.part.3"
            solution.write_text(
                "\n".join(str(value) for value in initial) + "\n",
                encoding="utf-8",
            )
            artifact = {
                "cluster_order": [f"c{index}" for index in range(vertex_count)],
                "fpga_order": ["fpga0", "fpga1", "fpga2"],
                "vertex_dimensions": ["cells", "lut", "dsp48"],
                "vertex_weights": weights,
                "balance_allowed_loads": {
                    "fpga0": {"cells": 2, "lut": 20, "dsp48": 1},
                    "fpga1": {"cells": 21, "lut": 20, "dsp48": 1},
                    "fpga2": {"cells": 21, "lut": 20, "dsp48": 1},
                },
                "files": {
                    "hypergraph": "partition.hgr",
                    "fixed": "partition.fix",
                    "edge_classes": "partition.edge-classes",
                },
            }
            candidate = {
                cluster: f"fpga{part}"
                for cluster, part in zip(artifact["cluster_order"], initial)
            }
            repaired, report = _run_native_balance_legalizer(
                candidate,
                artifact,
                root,
                solution,
                root / "repaired.part.3",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(repaired["c0"], "fpga1")
        self.assertTrue(all(repaired[f"c{index}"] == "fpga2" for index in range(2, 22)))
        self.assertEqual(report["direct_moves"], 0)
        self.assertEqual(report["paired_sequences"], 0)
        self.assertEqual(report["ejection_chain_sequences"], 1)
        self.assertEqual(report["max_ejection_chain_moves"], 2)
        self.assertEqual(report["moves"], 21)

    def test_native_balance_legalizer_replays_proven_chain_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            weights = [
                [1, 3, 0, 0], [1, 3, 0, 0],
                [1, 0, 3, 0], [1, 0, 3, 0],
                [1, 0, 0, 3], [1, 0, 0, 3],
            ]
            (root / "partition.hgr").write_text(
                "1 6 11\n1 1 2 3 4 5 6\n"
                + "".join(
                    " ".join(str(value) for value in row) + "\n"
                    for row in weights
                ),
                encoding="utf-8",
            )
            (root / "partition.fix").write_text("-1\n" * 6, encoding="utf-8")
            (root / "partition.edge-classes").write_text("0\n", encoding="utf-8")
            solution = root / "partition.hgr.part.3"
            solution.write_text("0\n0\n1\n1\n2\n2\n", encoding="utf-8")
            artifact = {
                "cluster_order": [f"c{index}" for index in range(6)],
                "fpga_order": ["fpga0", "fpga1", "fpga2"],
                "vertex_dimensions": ["cells", "lut", "ff", "dsp48"],
                "vertex_weights": weights,
                "balance_allowed_loads": {
                    "fpga0": {"cells": 2, "lut": 2, "ff": 2, "dsp48": 6},
                    "fpga1": {"cells": 2, "lut": 6, "ff": 2, "dsp48": 2},
                    "fpga2": {"cells": 2, "lut": 2, "ff": 6, "dsp48": 2},
                },
                "files": {
                    "hypergraph": "partition.hgr",
                    "fixed": "partition.fix",
                    "edge_classes": "partition.edge-classes",
                },
            }
            _, report = _run_native_balance_legalizer(
                {
                    "c0": "fpga0", "c1": "fpga0",
                    "c2": "fpga1", "c3": "fpga1",
                    "c4": "fpga2", "c5": "fpga2",
                },
                artifact,
                root,
                solution,
                root / "repaired.part.3",
                executable=str(tritonpart_balance_legalizer()),
                timeout_seconds=30,
            )
        self.assertEqual(report["ejection_chain_sequences"], 2)
        self.assertEqual(report["replayed_chain_sequences"], 1)
        self.assertEqual(report["max_ejection_chain_moves"], 3)
        self.assertEqual(report["moves"], 6)

    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        self.platform = Platform.load(PLATFORM_PATH)
        self.constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        self.clusters = build_clusters(self.ir, self.constraints)

    def test_export_is_weighted_multiresource_hypergraph(self) -> None:
        timed_net = next(
            net["id"]
            for net in self.ir.value["nets"]
            if net["cut_class"] in {"register_input", "register_output"}
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                output,
                net_weights={timed_net: 7.0},
            )
            self.assertEqual(artifact["schema"], TRITONPART_INPUT_SCHEMA)
            self.assertEqual(artifact["objective_encoding"], "net-cut-v1")
            self.assertEqual(
                artifact["objective_components"],
                {"net_cut_hyperedges": len(artifact["hyperedges"])},
            )
            self.assertEqual(artifact["fpga_order"], ["fpga0", "fpga1"])
            self.assertEqual(artifact["vertex_dimensions"][0], "cells")
            self.assertIn("lut", artifact["vertex_dimensions"])
            self.assertIn("ff", artifact["vertex_dimensions"])
            self.assertGreater(len(artifact["hyperedges"]), 0)
            self.assertTrue(
                all(
                    edge["objective_component"] == "net-cut"
                    and edge["cut_class"]
                    in {"register_input", "register_output"}
                    for edge in artifact["hyperedges"]
                )
            )
            self.assertEqual(
                artifact["timing_weight_coverage"]["specified_nets"], 1
            )
            self.assertEqual(
                artifact["timing_weight_coverage"][
                    "timed_legal_hyperedges"
                ],
                1,
            )
            self.assertTrue(
                (output / "partition.unweighted.hgr").is_file()
            )

            lines = (output / "partition.hgr").read_text().splitlines()
            edge_count, vertex_count, weight_flag = map(
                int, lines[0].split()
            )
            self.assertEqual(edge_count, len(artifact["hyperedges"]))
            self.assertEqual(vertex_count, len(artifact["cluster_order"]))
            self.assertEqual(weight_flag, 11)
            self.assertEqual(
                len((output / "partition.fix").read_text().splitlines()),
                vertex_count,
            )

    def test_export_respects_capacity_selected_partition_targets(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fpga_selection_policy": "minimum-capacity",
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                clusters,
                constraints,
                Path(temporary_directory),
            )
        self.assertEqual(artifact["fpga_order"], ["fpga0"])

    def test_managed_run_does_not_serialize_duplicate_input_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            solution = root / "solution.txt"
            solution.write_text(
                "\n".join(
                    str(index % 2)
                    for index, _ in enumerate(self.clusters["clusters"])
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "run"
            assignment = run_tritonpart(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                output,
                seed=1,
                solution_input=solution,
                persist_input_manifest=False,
            )
            self.assertFalse((output / "tritonpart_input.json").exists())
            self.assertEqual(
                assignment["provider_metadata"]["artifacts"],
                {"retained": False},
            )

    def test_deferred_contract_keeps_intermediate_assignment_lightweight(
        self,
    ) -> None:
        clusters = copy.deepcopy(self.clusters)
        clusters.setdefault("policy", {}).update(
            {
                "cut_mode": CUT_MODE_STATIC_EXACT,
                "max_cross_fpga_dependency_depth": 8,
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            solution = root / "solution.txt"
            solution.write_text(
                "\n".join(
                    str(index % 2)
                    for index, _ in enumerate(clusters["clusters"])
                )
                + "\n",
                encoding="utf-8",
            )
            assignment = run_tritonpart(
                self.ir,
                self.platform,
                clusters,
                self.constraints,
                root / "run",
                seed=1,
                solution_input=solution,
                defer_semantic_contract=True,
                persist_input_manifest=False,
            )
            self.assertNotIn("semantic_contract", assignment)

    def test_balance_tolerance_translates_to_target_percentage_points(self) -> None:
        self.assertAlmostEqual(
            _tritonpart_ubfactor_percent_points([0.25] * 4, 10.0), 2.5
        )

    def test_balance_repair_uses_two_move_exchange_when_parts_are_full(self) -> None:
        clusters = {
            "clusters": [
                {
                    "id": cluster_id,
                    "instances": [f"i{index}"],
                    "resources": {"lut": lut},
                    "fixed_fpga": None,
                }
                for index, (cluster_id, lut) in enumerate(
                    (("c0", 2), ("c1", 4), ("c2", 1), ("c3", 3))
                )
            ]
        }
        assignment = {
            "c0": "fpga0",
            "c1": "fpga0",
            "c2": "fpga1",
            "c3": "fpga1",
        }
        repaired, report = _repair_multi_resource_balance_reference(
            assignment,
            clusters,
            self.platform,
            {
                "balance_tolerance": 0.0,
                "balance_tolerance_by_dimension": {},
            },
            {
                "cluster_order": ["c0", "c1", "c2", "c3"],
                "fpga_order": ["fpga0", "fpga1"],
                "vertex_dimensions": ["cells", "lut"],
                "vertex_weights": [[1, 2], [1, 4], [1, 1], [1, 3]],
                "effective_balance_percent": 0.0,
                "hyperedges": [],
            },
        )
        self.assertEqual(report["paired_move_sequences"], 1)
        self.assertEqual(report["moves"], 2)
        self.assertEqual(repaired["c0"], "fpga1")
        self.assertEqual(repaired["c2"], "fpga0")

    def test_balance_repair_applies_the_ranked_target(self) -> None:
        platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "three-part-repair",
                    "kind": "virtual",
                },
                "fpgas": [
                    {
                        "id": f"fpga{index}",
                        "part": "test-part",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 100},
                    }
                    for index in range(3)
                ],
                "links": [],
            }
        )
        cluster_order = [f"c{index}" for index in range(12)]
        clusters = {
            "clusters": [
                {
                    "id": cluster_id,
                    "instances": [f"i{index}"],
                    "resources": {},
                    "fixed_fpga": None,
                }
                for index, cluster_id in enumerate(cluster_order)
            ]
        }
        assignment = {
            cluster_id: (
                "fpga0" if index < 6 else "fpga1" if index < 9 else "fpga2"
            )
            for index, cluster_id in enumerate(cluster_order)
        }
        repaired, report = _repair_multi_resource_balance_reference(
            assignment,
            clusters,
            platform,
            {
                "balance_tolerance": 0.0,
                "balance_tolerance_by_dimension": {},
            },
            {
                "cluster_order": cluster_order,
                "fpga_order": ["fpga0", "fpga1", "fpga2"],
                "vertex_dimensions": ["cells"],
                "vertex_weights": [[1] for _ in cluster_order],
                "effective_balance_percent": 0.0,
                "hyperedges": [
                    {"vertices": [1, 7], "weight": 10.0},
                    {"vertices": [2, 10], "weight": 9.0},
                ],
            },
        )
        self.assertEqual(report["moves"], 2)
        self.assertEqual(report["paired_move_sequences"], 0)
        self.assertEqual(repaired["c0"], "fpga1")
        self.assertEqual(repaired["c1"], "fpga2")

    def test_balance_repair_does_not_create_avoidable_combinational_cut(
        self,
    ) -> None:
        cluster_order = [f"c{index}" for index in range(6)]
        clusters = {
            "clusters": [
                {
                    "id": cluster_id,
                    "instances": [f"i{index}"],
                    "resources": {},
                    "fixed_fpga": None,
                }
                for index, cluster_id in enumerate(cluster_order)
            ]
        }
        assignment = {
            cluster_id: "fpga0" if index < 4 else "fpga1"
            for index, cluster_id in enumerate(cluster_order)
        }
        repaired, report = _repair_multi_resource_balance_reference(
            assignment,
            clusters,
            self.platform,
            {
                "balance_tolerance": 0.0,
                "balance_tolerance_by_dimension": {},
            },
            {
                "cluster_order": cluster_order,
                "fpga_order": ["fpga0", "fpga1"],
                "vertex_dimensions": ["cells"],
                "vertex_weights": [[1] for _ in cluster_order],
                "effective_balance_percent": 0.0,
                "hyperedges": [
                    {
                        "vertices": [1, 2],
                        "weight": 1.0,
                        "cut_class": "combinational",
                    },
                    {
                        "vertices": [1, 5],
                        "weight": 100.0,
                        "cut_class": "register_input",
                    },
                ],
            },
        )
        self.assertEqual(repaired["c0"], "fpga0")
        self.assertEqual(repaired["c1"], "fpga0")
        self.assertEqual(report["initial_combinational_cut_hyperedges"], 0)
        self.assertEqual(report["final_combinational_cut_hyperedges"], 0)
        self.assertGreater(report["combinational_cut_penalty"], 100.0)

    def test_balance_repair_uses_three_part_ejection_chain(self) -> None:
        platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "three-part-ejection-chain",
                    "kind": "virtual",
                },
                "fpgas": [
                    {
                        "id": "fpga0",
                        "part": "test-part",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 1, "ff": 1, "dsp": 3},
                    },
                    {
                        "id": "fpga1",
                        "part": "test-part",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 3, "ff": 1, "dsp": 1},
                    },
                    {
                        "id": "fpga2",
                        "part": "test-part",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 1, "ff": 3, "dsp": 1},
                    },
                ],
                "links": [],
            }
        )
        cluster_order = ["x", "y", "z"]
        clusters = {
            "clusters": [
                {
                    "id": cluster_id,
                    "instances": [f"i{index}"],
                    "resources": {},
                    "fixed_fpga": None,
                }
                for index, cluster_id in enumerate(cluster_order)
            ]
        }
        repaired, report = _repair_multi_resource_balance_reference(
            {"x": "fpga0", "y": "fpga1", "z": "fpga2"},
            clusters,
            platform,
            {
                "balance_tolerance": 2.0 / 3.0,
                "balance_tolerance_by_dimension": {},
            },
            {
                "cluster_order": cluster_order,
                "fpga_order": ["fpga0", "fpga1", "fpga2"],
                "vertex_dimensions": ["cells", "lut", "ff", "dsp"],
                "vertex_weights": [
                    [1, 3, 0, 0],
                    [1, 0, 3, 0],
                    [1, 0, 0, 3],
                ],
                "effective_balance_percent": 200.0 / 3.0,
                "hyperedges": [],
            },
        )
        self.assertEqual(
            repaired,
            {"x": "fpga1", "y": "fpga2", "z": "fpga0"},
        )
        self.assertEqual(report["paired_move_sequences"], 0)
        self.assertEqual(report["ejection_chain_sequences"], 1)
        self.assertEqual(report["max_ejection_chain_moves"], 3)
        self.assertEqual(report["moves"], 3)

    def test_export_translates_relative_tolerance_to_ubfactor_points(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                output,
            )
            expected = (
                max(artifact["base_balance"])
                * artifact["effective_balance_percent"]
            )
            self.assertAlmostEqual(
                artifact["tritonpart_ubfactor_percent_points"],
                expected,
            )
            tcl = (output / "run_tritonpart.tcl").read_text()
            self.assertIn(
                f"-balance_constraint {expected:.9g}",
                tcl,
            )
            self.assertIn("-num_initial_solutions 50", tcl)
            self.assertIn("-num_best_initial_solutions 10", tcl)
            self.assertEqual(
                artifact["search_effort"],
                {
                    "num_initial_solutions": 50,
                    "num_best_initial_solutions": 10,
                },
            )

    def test_export_records_bounded_validation_search_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                output,
                num_initial_solutions=4,
                num_best_initial_solutions=2,
            )
            tcl = (output / "run_tritonpart.tcl").read_text()
        self.assertIn("-num_initial_solutions 4", tcl)
        self.assertIn("-num_best_initial_solutions 2", tcl)
        self.assertEqual(
            artifact["search_effort"],
            {
                "num_initial_solutions": 4,
                "num_best_initial_solutions": 2,
            },
        )

    def test_export_rejects_invalid_search_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                ValidationError, "num_best_initial_solutions"
            ):
                export_tritonpart_inputs(
                    self.ir,
                    self.platform,
                    self.clusters,
                    self.constraints,
                    Path(temporary_directory),
                    num_initial_solutions=2,
                    num_best_initial_solutions=3,
                )

    def test_solution_parser_rejects_invalid_part(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                output,
            )
            solution = output / "bad.part"
            solution.write_text(
                "\n".join(["2"] * len(artifact["cluster_order"])) + "\n"
            )
            with self.assertRaisesRegex(ValidationError, "invalid part"):
                parse_tritonpart_solution(solution, artifact)

    def test_min_used_repair_moves_one_small_atomic_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                Path(temporary_directory),
            )
            raw = {
                cluster["id"]: "fpga0"
                for cluster in self.clusters["clusters"]
            }
            repaired, moves = _repair_min_used_fpgas(
                raw,
                self.clusters,
                self.platform,
                self.constraints,
                artifact["hyperedges"],
            )
            self.assertEqual(set(repaired.values()), {"fpga0", "fpga1"})
            self.assertEqual(len(moves), 1)
            self.assertEqual(moves[0]["source"], "fpga0")
            self.assertEqual(moves[0]["target"], "fpga1")
            self.assertEqual(moves[0]["instances"], 1)

    def test_min_used_repair_populates_multiple_empty_partitions(self) -> None:
        platform = Platform.load(FOUR_FPGA_PLATFORM_PATH)
        constraints = normalize_partition_constraints(None, self.ir, platform)
        clusters = build_clusters(self.ir, constraints)
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = export_tritonpart_inputs(
                self.ir,
                platform,
                clusters,
                constraints,
                Path(temporary_directory),
            )
            raw = {
                cluster["id"]: "fpga0"
                for cluster in clusters["clusters"]
            }
            repaired, moves = _repair_min_used_fpgas(
                raw,
                clusters,
                platform,
                constraints,
                artifact["hyperedges"],
            )
            self.assertEqual(
                set(repaired.values()),
                {"fpga0", "fpga1", "fpga2", "fpga3"},
            )
            self.assertEqual(len(moves), 3)
            self.assertTrue(
                all(isinstance(move["estimated_cut_delta"], float) for move in moves)
            )

    def test_balance_repair_legalizes_best_effort_solution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                Path(temporary_directory),
            )
            raw = {
                cluster["id"]: "fpga0"
                for cluster in self.clusters["clusters"]
            }
            repaired, summary = _repair_multi_resource_balance_reference(
                raw,
                self.clusters,
                self.platform,
                self.constraints,
                artifact,
            )
            self.assertEqual(set(repaired.values()), {"fpga0", "fpga1"})
            self.assertGreater(summary["moves"], 0)
            self.assertEqual(
                summary["final_cut_weight"],
                summary["initial_cut_weight"]
                + summary["estimated_cut_delta"],
            )
            self.assertEqual(
                summary["final_loads"]["fpga0"]["cells"],
                summary["final_loads"]["fpga1"]["cells"],
            )
            self.assertEqual(
                set(summary["moved_weights"]),
                {"fpga0->fpga1"},
            )
            self.assertEqual(
                set(summary["moved_weights"]["fpga0->fpga1"]),
                {"cells", "lut", "ff"},
            )

    def test_balance_repair_honors_dimension_specific_tolerances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            constraints = {
                **self.constraints,
                "balance_tolerance_by_dimension": {
                    "cells": 1.0,
                    "lut": 1.0,
                    "ff": 1.0,
                },
            }
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                constraints,
                Path(temporary_directory),
            )
            raw = {
                cluster["id"]: "fpga0"
                for cluster in self.clusters["clusters"]
            }
            repaired, summary = _repair_multi_resource_balance_reference(
                raw,
                self.clusters,
                self.platform,
                constraints,
                artifact,
            )
            self.assertEqual(repaired, raw)
            self.assertEqual(summary["moves"], 0)

    def test_phase3_executes_provider_and_independently_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            ir_path = output / "design.emuir.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            attempted_seeds = []

            def fake_openroad(command, **kwargs):
                self.assertTrue(Path(command[-1]).is_absolute())
                run_directory = Path(kwargs["cwd"])
                tcl = Path(command[-1]).read_text(encoding="utf-8")
                seed_line = next(
                    line for line in tcl.splitlines() if "-seed" in line
                )
                attempted_seeds.append(int(seed_line.split()[1]))
                tritonpart_input = json.loads(
                    (run_directory / "tritonpart_input.json").read_text()
                )
                solution = run_directory / tritonpart_input["files"]["solution"]
                solution.write_text(
                    "\n".join(
                        (
                            ("1" if index == 0 else "0")
                            if len(attempted_seeds) == 1
                            else str(index % 2)
                        )
                        for index in range(
                            len(tritonpart_input["cluster_order"])
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout="TritonPart test provider\n"
                )

            with mock.patch(
                "emuflow.tritonpart.subprocess.run",
                side_effect=fake_openroad,
            ):
                report = run_phase3(
                    ir_path=ir_path,
                    platform_path=PLATFORM_PATH,
                    output_dir=output / "phase3",
                    seed=19,
                    provider="tritonpart",
                    cut_mode="sequential-only",
                    openroad="/fake/openroad",
                    tritonpart_seed_attempts=2,
                    retain_diagnostics=True,
                )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["seed"], 20)
            self.assertEqual(attempted_seeds, [19, 20])
            self.assertEqual(
                report["provider"],
                "tritonpart-openroad-hypergraph-v1",
            )
            self.assertEqual(report["validation"]["used_fpgas"], 2)
            self.assertEqual(report["validation"]["illegal_cuts"], 0)
            assignment = json.loads(
                (output / "phase3" / "assignment.json").read_text()
            )
            self.assertEqual(
                assignment["provider_metadata"]["mode"], "execute"
            )
            attempts = assignment["provider_metadata"]["seed_attempts"]
            self.assertEqual([item["seed"] for item in attempts], [19, 20])
            self.assertEqual(attempts[0]["raw_used_fpgas"], 2)
            self.assertFalse(attempts[0]["accepted"])
            self.assertEqual(
                attempts[0]["rejection"],
                "multi_resource_balance",
            )
            self.assertIn("violates", attempts[0]["error"])
            self.assertEqual(
                attempts[0]["solution"],
                "partition.hgr.seed-19.part.2",
            )
            self.assertEqual(attempts[1]["raw_used_fpgas"], 2)
            self.assertTrue(attempts[1]["accepted"])
            self.assertEqual(
                attempts[1]["balance_validation"]["balance_violations"],
                0,
            )
            self.assertEqual(
                attempts[1]["solution"],
                "partition.hgr.seed-20.part.2",
            )
            self.assertEqual(
                assignment["provider_metadata"]["min_used_fpgas_repair"],
                {"enabled": False, "moves": []},
            )
            self.assertTrue(
                (
                    output
                    / "phase3"
                    / "tritonpart"
                    / "openroad-tritonpart.seed-20.log"
                ).is_file()
            )

    def test_timing_portfolio_binds_each_hypergraph_solution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            ir_path = output / "design.emuir.json"
            weights_path = output / "weights.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            timed_net = next(
                net["id"]
                for net in self.ir.value["nets"]
                if net["cut_class"] in {"register_input", "register_output"}
            )
            weights_path.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.partition-net-weights/v1",
                        "weights": {timed_net: 7.0},
                    }
                ),
                encoding="utf-8",
            )
            hypergraphs = []

            def fake_openroad(command, **kwargs):
                run_directory = Path(kwargs["cwd"])
                tcl = Path(command[-1]).read_text(encoding="utf-8")
                line = next(
                    item
                    for item in tcl.splitlines()
                    if "-hypergraph_file" in item
                )
                hypergraph = Path(line.split("{", 1)[1].split("}", 1)[0])
                hypergraphs.append(hypergraph.name)
                tritonpart_input = json.loads(
                    (run_directory / "tritonpart_input.json").read_text()
                )
                Path(
                    f"{hypergraph}.part."
                    f"{len(tritonpart_input['fpga_order'])}"
                ).write_text(
                    "\n".join(
                        str(index % 2)
                        for index in range(
                            len(tritonpart_input["cluster_order"])
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="ok\n")

            with mock.patch(
                "emuflow.tritonpart.subprocess.run",
                side_effect=fake_openroad,
            ):
                run_phase3(
                    ir_path=ir_path,
                    platform_path=PLATFORM_PATH,
                    output_dir=output / "phase3-default",
                    seed=7,
                    provider="tritonpart",
                    cut_mode="sequential-only",
                    openroad="/fake/openroad",
                    net_weights_path=weights_path,
                    retain_diagnostics=True,
                )
                default_assignment = json.loads(
                    (output / "phase3-default" / "assignment.json")
                    .read_text()
                )
                default_hypergraphs = list(hypergraphs)
                hypergraphs.clear()
                run_phase3(
                    ir_path=ir_path,
                    platform_path=PLATFORM_PATH,
                    output_dir=output / "phase3",
                    seed=7,
                    provider="tritonpart",
                    cut_mode="sequential-only",
                    openroad="/fake/openroad",
                    net_weights_path=weights_path,
                    tritonpart_run_unweighted_baseline=True,
                    retain_diagnostics=True,
                )
            assignment = json.loads(
                (output / "phase3" / "assignment.json").read_text()
            )
        self.assertEqual(default_hypergraphs, ["partition.hgr"])
        self.assertEqual(
            [
                attempt["mode"]
                for attempt in default_assignment["provider_metadata"][
                    "seed_attempts"
                ]
            ],
            ["timing_weighted"],
        )
        self.assertEqual(
            hypergraphs,
            ["partition.hgr", "partition.unweighted.hgr"],
        )
        self.assertEqual(
            [
                attempt["mode"]
                for attempt in assignment["provider_metadata"][
                    "seed_attempts"
                ]
            ],
            ["timing_weighted", "unweighted_baseline"],
        )

    def test_seed_sweep_continues_after_balance_repair_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            ir_path = output / "design.emuir.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )

            def fake_openroad(command, **kwargs):
                run_directory = Path(kwargs["cwd"])
                tritonpart_input = json.loads(
                    (run_directory / "tritonpart_input.json").read_text()
                )
                solution = (
                    run_directory
                    / tritonpart_input["files"]["solution"]
                )
                solution.write_text(
                    "\n".join(
                        str(index % 2)
                        for index in range(
                            len(tritonpart_input["cluster_order"])
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="ok\n")

            repair_calls = 0

            def fake_repair(candidate, *_args, **_kwargs):
                nonlocal repair_calls
                repair_calls += 1
                if repair_calls == 1:
                    raise ValidationError("no legal repair move")
                Path(_args[3]).write_bytes(Path(_args[2]).read_bytes())
                return candidate, {"moves": 0}

            with (
                mock.patch(
                    "emuflow.tritonpart.subprocess.run",
                    side_effect=fake_openroad,
                ),
                mock.patch(
                    "emuflow.tritonpart._run_native_balance_legalizer",
                    side_effect=fake_repair,
                ),
            ):
                report = run_phase3(
                    ir_path=ir_path,
                    platform_path=PLATFORM_PATH,
                    output_dir=output / "phase3",
                    seed=41,
                    provider="tritonpart",
                    cut_mode="sequential-only",
                    openroad="/fake/openroad",
                    tritonpart_seed_attempts=2,
                    tritonpart_repair_balance=True,
                    retain_diagnostics=True,
                )

            self.assertEqual(report["status"], "pass")
            assignment = json.loads(
                (output / "phase3" / "assignment.json").read_text()
            )
            attempts = assignment["provider_metadata"]["seed_attempts"]
            self.assertEqual([item["seed"] for item in attempts], [41, 42])
            self.assertFalse(attempts[0]["accepted"])
            self.assertEqual(attempts[0]["rejection"], "balance_repair")
            self.assertIn("no legal repair move", attempts[0]["error"])
            self.assertTrue(attempts[1]["accepted"])

    def test_phase3_balance_repair_is_audited_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            ir_path = output / "design.emuir.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )

            real_run = subprocess.run

            def fake_openroad(command, **kwargs):
                if command[0] != "/fake/openroad":
                    return real_run(command, **kwargs)
                run_directory = Path(kwargs["cwd"])
                tritonpart_input = json.loads(
                    (run_directory / "tritonpart_input.json").read_text()
                )
                solution = run_directory / tritonpart_input["files"]["solution"]
                solution.write_text(
                    "\n".join(
                        ["0"] * len(tritonpart_input["cluster_order"])
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="ok\n")

            with mock.patch(
                "emuflow.tritonpart.subprocess.run",
                side_effect=fake_openroad,
            ):
                report = run_phase3(
                    ir_path=ir_path,
                    platform_path=PLATFORM_PATH,
                    output_dir=output / "phase3",
                    seed=31,
                    provider="tritonpart",
                    cut_mode="sequential-only",
                    openroad="/fake/openroad",
                    tritonpart_balance_legalizer=(
                        str(tritonpart_balance_legalizer())
                    ),
                    tritonpart_repair_balance=True,
                    retain_diagnostics=True,
                )

            self.assertEqual(report["status"], "pass")
            assignment = json.loads(
                (output / "phase3" / "assignment.json").read_text()
            )
            repair = assignment["provider_metadata"]["balance_repair"]
            self.assertTrue(repair["enabled"])
            self.assertGreater(repair["summary"]["moves"], 0)
            repaired_solution = (
                output
                / "phase3"
                / "tritonpart"
                / repair["summary"]["solution"]
            )
            self.assertTrue(repaired_solution.is_file())
            self.assertEqual(
                assignment["provider_metadata"]["seed_attempts"][0][
                    "repaired_solution"
                ],
                repaired_solution.name,
            )


if __name__ == "__main__":
    unittest.main()
