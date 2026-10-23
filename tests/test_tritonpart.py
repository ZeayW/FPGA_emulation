import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    CUT_MODE_STATIC_EXACT,
    build_clusters,
    normalize_partition_constraints,
)
from emuflow.phase3 import run_phase3
from emuflow.partition_ir import load_partition_ir
from emuflow.platform import Platform
from emuflow.tritonpart import (
    TRITONPART_INPUT_SCHEMA,
    _repair_min_used_fpgas,
    _stream_assignment_cut_summary,
    _tritonpart_ubfactor_percent_points,
    export_tritonpart_inputs,
    parse_tritonpart_solution,
    run_tritonpart,
)
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"
FOUR_FPGA_PLATFORM_PATH = (
    ROOT / "platforms" / "virtual" / "xcvu3p_4fpga_mesh.json"
)


class TritonPartTest(unittest.TestCase):


    def test_illegal_native_assignment_is_rejected_without_mutation(self):
        constraints = {**self.constraints, "balance_dimensions": ["cells", "lut", "ff"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = "0\n" * len(self.clusters["clusters"])
            solution = root / "input.part.2"
            solution.write_text(original)
            with self.assertRaisesRegex(ValidationError, "multi_resource_balance"):
                run_tritonpart(self.ir, self.platform, self.clusters, constraints,
                               root / "run", seed=1, solution_input=solution,
                               repair_min_used_fpgas=True)
            self.assertEqual(solution.read_text(), original)
            self.assertEqual((root / "run/partition.hgr.part.2").read_text(), original)
            self.assertFalse(list((root / "run").glob("*repaired*")))

    def test_export_passes_exact_independent_resource_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = export_tritonpart_inputs(self.ir, self.platform, self.clusters,
                                              self.constraints, root)
            bounds = root / artifact["files"]["resource_bounds"]
            rows = bounds.read_text().splitlines()
            dimensions = artifact["vertex_dimensions"]
            self.assertEqual(rows[0], f"{len(artifact['fpga_order'])} {len(dimensions)}")
            for fpga, row in zip(artifact["fpga_order"], rows[1:]):
                values = list(map(int, row.split()))
                self.assertEqual(values[::2], [0] * len(dimensions))
                self.assertEqual(values[1::2], [artifact["balance_allowed_loads"][fpga][d]
                                               for d in dimensions])
            self.assertIn(f"-resource_bounds_file {{{bounds.resolve()}}}",
                          (root / artifact["files"]["tcl"]).read_text())

    def test_cut_summary_streams_once_and_combines_timing_metrics(self) -> None:
        class OnePassEdges:
            def __init__(self):
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                if self.iterations != 1:
                    raise AssertionError("hyperedges were scanned more than once")
                yield {"net": "n0", "clusters": ["a", "b"], "weight": 2.5}
                yield {"net": "n1", "clusters": ["b", "c"], "weight": 3.0}
                yield {"net": "n2", "clusters": ["a", "c"], "weight": 4.0}

        edges = OnePassEdges()
        summary = _stream_assignment_cut_summary(
            edges,
            {"a": "fpga0", "b": "fpga1", "c": "fpga1"},
            {"n0", "n2"},
        )
        self.assertEqual(edges.iterations, 1)
        self.assertEqual(summary, {
            "cut_hyperedges": 2,
            "cut_weight": 6.5,
            "timed_cut_hyperedges": 2,
            "timed_cut_weight": 6.5,
        })


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

    def test_export_keeps_resource_absent_from_one_device(self):
        value = self.platform.to_dict()
        value["fpgas"][1]["capacity"]["ff"] = 0
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(None, self.ir, platform)
        with tempfile.TemporaryDirectory() as temporary:
            artifact = export_tritonpart_inputs(self.ir, platform, self.clusters,
                                              constraints, Path(temporary))
            self.assertIn("ff", artifact["vertex_dimensions"])
            self.assertEqual(artifact["balance_allowed_loads"]["fpga1"]["ff"], 0)

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

    def test_compact_export_matches_reference_without_graph_materialization(
        self,
    ) -> None:
        timed_net = next(
            net["id"]
            for net in self.ir.value["nets"]
            if net["cut_class"] in {"register_input", "register_output"}
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                root / "reference",
                net_weights={timed_net: 7.0},
                write_manifest=False,
            )
            ir_path = root / "partition.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            compact_ir = load_partition_ir(ir_path)
            compact_clusters = build_clusters(
                compact_ir, self.constraints
            )
            actual = export_tritonpart_inputs(
                compact_ir,
                self.platform,
                compact_clusters,
                self.constraints,
                root / "compact",
                net_weights={timed_net: 7.0},
                write_manifest=False,
            )

            for field in (
                "cluster_order",
                "vertex_weights",
                "hyperedges",
            ):
                self.assertEqual(list(actual[field]), list(reference[field]))
            for field in (
                "objective_components",
                "vertex_dimensions",
                "timing_weight_coverage",
                "balance_allowed_loads",
                "files",
            ):
                self.assertEqual(actual[field], reference[field])
            for name in (
                "partition.hgr",
                "partition.unweighted.hgr",
                "partition.fix",
                "partition.edge-classes",
            ):
                self.assertEqual(
                    (root / "compact" / name).read_bytes(),
                    (root / "reference" / name).read_bytes(),
                )
            self.assertGreater(actual["hyperedges"].payload_bytes, 0)

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
                _defer_derived_records=True,
                persist_input_manifest=False,
            )
            self.assertNotIn("semantic_contract", assignment)

    def test_deferred_compact_assignment_keeps_instance_owner_columns(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "partition.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            compact_ir = load_partition_ir(ir_path)
            clusters = build_clusters(compact_ir, self.constraints)
            solution = root / "solution.txt"
            solution.write_text(
                "\n".join(
                    str(index % 2)
                    for index in range(len(clusters["clusters"]))
                )
                + "\n",
                encoding="utf-8",
            )
            assignment = run_tritonpart(
                compact_ir,
                self.platform,
                clusters,
                self.constraints,
                root / "run",
                seed=1,
                solution_input=solution,
                defer_semantic_contract=True,
                _defer_derived_records=True,
                persist_input_manifest=False,
            )
            owners = assignment["instance_assignment"]
            cluster_owners = assignment["cluster_assignment"]
            self.assertNotIsInstance(owners, dict)
            self.assertNotIsInstance(cluster_owners, dict)
            self.assertEqual(
                assignment["_derived_records"], "deferred-selection-only"
            )
            self.assertNotIn("cut_nets", assignment)
            self.assertEqual(
                cluster_owners.packed_payload_bytes,
                4 * len(clusters["clusters"]),
            )
            expected = {
                instance_id: f"fpga{index % 2}"
                for index, cluster in enumerate(clusters["clusters"])
                for instance_id in cluster["instances"]
            }
            self.assertEqual(dict(owners.items()), expected)


    def test_initializer_envelope_has_no_hidden_design_share_bound(self) -> None:
        ids = [f"fpga{i}" for i in range(4)]
        limits = {fpga: {"cells": 100, "lut": 100} for fpga in ids}
        self.assertEqual(_tritonpart_ubfactor_percent_points(
            [0.25] * 4, ids, {"cells": 100, "lut": 100}, limits), 75.0)

    def test_initializer_envelope_does_not_add_an_unrequested_lower_bound(self) -> None:
        ids = [f"fpga{i}" for i in range(4)]
        # The integer upper bound is 27%; the smallest feasible part has 19%.
        limits = {fpga: {"cells": 27} for fpga in ids}
        ub = _tritonpart_ubfactor_percent_points([0.25] * 4, ids, {"cells": 100}, limits)
        self.assertGreaterEqual(ub, 6.0)
        self.assertLessEqual(ub, 6.000001)


    def test_export_capacity_only_envelope_reaches_full_design_share(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            artifact = export_tritonpart_inputs(
                self.ir,
                self.platform,
                self.clusters,
                {**self.constraints, "balance_dimensions": []},
                output,
            )
            expected = 100.0 * (1.0 - min(artifact["base_balance"]))
            self.assertEqual(artifact["strict_balance_dimensions"], [])
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
                    "num_vertices_threshold_ilp": 1000,
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
        self.assertIn("-num_vertices_threshold_ilp 1000", tcl)
        self.assertEqual(
            artifact["search_effort"],
            {
                "num_initial_solutions": 4,
                "num_best_initial_solutions": 2,
                "num_vertices_threshold_ilp": 1000,
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


    def test_phase3_executes_provider_and_independently_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            constraints_path = output / "strict-constraints.json"
            constraints_path.write_text(json.dumps({
                "schema": "emuflow.partition-constraints/v1",
                "balance_dimensions": ["cells", "lut", "ff"],
            }))
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
                    constraints_path=constraints_path,
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
                {"enabled": True, "moves": []},
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


if __name__ == "__main__":
    unittest.main()
