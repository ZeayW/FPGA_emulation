import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    build_clusters,
    normalize_partition_constraints,
)
from emuflow.phase3 import run_phase3
from emuflow.platform import Platform
from emuflow.tritonpart import (
    TRITONPART_INPUT_SCHEMA,
    _effective_balance_percent,
    _repair_min_used_fpgas,
    _repair_multi_resource_balance,
    _tritonpart_ubfactor_percent_points,
    export_tritonpart_inputs,
    parse_tritonpart_solution,
    run_tritonpart,
)
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


class TritonPartTest(unittest.TestCase):
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
            self.assertEqual(artifact["fpga_order"], ["fpga0", "fpga1"])
            self.assertEqual(artifact["vertex_dimensions"][0], "cells")
            self.assertIn("lut", artifact["vertex_dimensions"])
            self.assertIn("ff", artifact["vertex_dimensions"])
            self.assertGreater(len(artifact["hyperedges"]), 0)
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

    def test_atomic_balance_relaxation_is_relative_to_target_block(self) -> None:
        requested, effective = _effective_balance_percent(
            [[37], [21], [21], [21]],
            [
                {"fixed_fpga": None},
                {"fixed_fpga": None},
                {"fixed_fpga": None},
                {"fixed_fpga": None},
            ],
            ["fpga0", "fpga1", "fpga2", "fpga3"],
            [0.25, 0.25, 0.25, 0.25],
            0.10,
        )
        self.assertEqual(requested, 10.0)
        self.assertAlmostEqual(effective, 48.01)
        self.assertAlmostEqual(
            _tritonpart_ubfactor_percent_points(
                [0.25, 0.25, 0.25, 0.25],
                effective,
            ),
            12.0025,
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
        repaired, report = _repair_multi_resource_balance(
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
        repaired, report = _repair_multi_resource_balance(
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
        repaired, report = _repair_multi_resource_balance(
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
            repaired, summary = _repair_multi_resource_balance(
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
            repaired, summary = _repair_multi_resource_balance(
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
                    output_dir=output / "phase3",
                    seed=7,
                    provider="tritonpart",
                    cut_mode="sequential-only",
                    openroad="/fake/openroad",
                    net_weights_path=weights_path,
                    retain_diagnostics=True,
                )
            assignment = json.loads(
                (output / "phase3" / "assignment.json").read_text()
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

            def fake_repair(candidate, *_args):
                nonlocal repair_calls
                repair_calls += 1
                if repair_calls == 1:
                    raise ValidationError("no legal repair move")
                return candidate, {"moves": 0}

            with (
                mock.patch(
                    "emuflow.tritonpart.subprocess.run",
                    side_effect=fake_openroad,
                ),
                mock.patch(
                    "emuflow.tritonpart._repair_multi_resource_balance",
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

            def fake_openroad(command, **kwargs):
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
