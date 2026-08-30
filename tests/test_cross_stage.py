import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.board_link_timing import build_board_link_timing_model
from emuflow.cross_stage import (
    build_cross_stage_candidate,
    compare_candidate_objectives,
    reconstruct_partition_class,
    reconstruct_partition_migration,
    run_cross_stage_optimization,
    validate_cross_stage_report,
)
from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.phase3 import run_phase3
from emuflow.platform import Platform
from emuflow.routing import load_route_constraints
from emuflow.tdm import build_tdm_schedule
from emuflow.tdm_ratio import build_tdm_ratio_plan
from emuflow.yosys import import_yosys_json
from tests.test_phase5 import _link, _platform_value, _routes


ROOT = Path(__file__).resolve().parents[1]


class CrossStageCandidateTest(unittest.TestCase):
    def test_partition_migration_aligns_only_platform_automorphisms(
        self,
    ) -> None:
        value = _platform_value(
            "symmetric_ring",
            ["a", "b", "c"],
            [
                _link("ab", "a", "b"),
                _link("bc", "b", "c"),
                _link("ca", "c", "a"),
            ],
        )
        platform = Platform.from_dict(value)
        incumbent = {
            "cluster_assignment": {"c0": "a", "c1": "b", "c2": "c"}
        }
        candidate = {
            "cluster_assignment": {"c0": "b", "c1": "c", "c2": "a"}
        }
        migration = reconstruct_partition_migration(
            incumbent,
            candidate,
            platform,
            load_route_constraints(None, platform),
        )
        self.assertEqual(migration["moved_clusters"], 3)
        self.assertEqual(
            migration["symmetry_alignment"]["moved_clusters"], 0
        )
        self.assertEqual(
            migration["symmetry_alignment"]["mapping"],
            {"a": "b", "b": "c", "c": "a"},
        )
        self.assertEqual(
            reconstruct_partition_class(
                incumbent,
                platform,
                load_route_constraints(None, platform),
            ),
            reconstruct_partition_class(
                candidate,
                platform,
                load_route_constraints(None, platform),
            ),
        )

        asymmetric = copy.deepcopy(value)
        for index, fpga in enumerate(asymmetric["fpgas"]):
            fpga["capacity"]["lut"] += index
        asymmetric_platform = Platform.from_dict(asymmetric)
        asymmetric_migration = reconstruct_partition_migration(
            incumbent,
            candidate,
            asymmetric_platform,
            load_route_constraints(None, asymmetric_platform),
        )
        self.assertEqual(
            asymmetric_migration["symmetry_alignment"]["moved_clusters"],
            3,
        )
        self.assertEqual(
            asymmetric_migration["symmetry_alignment"]["valid_automorphisms"],
            1,
        )
        self.assertNotEqual(
            reconstruct_partition_class(
                incumbent,
                asymmetric_platform,
                load_route_constraints(None, asymmetric_platform),
            )["sha256"],
            reconstruct_partition_class(
                candidate,
                asymmetric_platform,
                load_route_constraints(None, asymmetric_platform),
            )["sha256"],
        )

    def test_all_path_objective_keeps_non_crossing_paths(self) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("a C++17 compiler is required")
        platform = Platform.from_dict(
            _platform_value(
                "cross_stage",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        cuts = [("n0", "a", ["b"]), ("n1", "a", ["b"])]
        assignment = {
            "schema": PARTITION_ASSIGNMENT_SCHEMA,
            "design": "tdm_test",
            "platform": platform.name,
            "cut_nets": [
                {
                    "net": net,
                    "cut_class": "register_output",
                    "source_fpgas": [source],
                    "sink_fpgas": sinks,
                    "sink_endpoints": len(sinks),
                }
                for net, source, sinks in cuts
            ],
        }
        routes = _routes(platform, cuts, frame_slots=16)
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 10.0,
                "max_clock_period_ns": 20.0,
            },
            "compression": {
                "original_paths": 2,
                "compressed_paths": 2,
            },
            "paths": [
                {
                    "path": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "fixed_delay_ns": 8.0,
                    "cut_nets": ["n0"],
                },
                {
                    "path": "p1",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "fixed_delay_ns": 4.0,
                    "cut_nets": ["n1"],
                },
            ],
        }
        database = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "tdm_test",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 10.0,
                "max_clock_period_ns": 20.0,
            },
            "paths": [
                {
                    "id": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 10.0,
                    "fixed_delay_ns": 8.0,
                    "path_nets": ["n0"],
                    "normalized_slack": 1.0,
                },
                {
                    "id": "p1",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 10.0,
                    "fixed_delay_ns": 4.0,
                    "path_nets": ["n1"],
                    "normalized_slack": 1.0,
                },
                {
                    "id": "local-critical",
                    "clock_domain": "clk",
                    "clock_period_ns": 5.0,
                    "slack_ns": -5.0,
                    "fixed_delay_ns": 10.0,
                    "path_nets": ["local"],
                    "normalized_slack": -0.1,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            executable = (
                Path(temporary) / "emuflow_tdm_ratio_optimizer"
            )
            subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-O2",
                    str(
                        ROOT
                        / "src"
                        / "native"
                        / "tdm_ratio_optimizer.cpp"
                    ),
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            plan = build_tdm_ratio_plan(
                routes,
                platform,
                executable=str(executable),
                max_ratio=8,
                post_refinement_iterations=10,
            )
            schedule = build_tdm_schedule(routes, platform, plan)
            candidate = build_cross_stage_candidate(
                database,
                assignment,
                routes,
                schedule,
                plan,
                platform,
            )
            repeated = build_cross_stage_candidate(
                database,
                assignment,
                routes,
                schedule,
                plan,
                platform,
            )
        self.assertEqual(candidate, repeated)
        self.assertEqual(candidate["path_metrics"]["all_paths"], 3)
        self.assertEqual(candidate["path_metrics"]["crossing_paths"], 2)
        self.assertEqual(candidate["path_metrics"]["no_cut_paths"], 1)
        self.assertEqual(
            candidate["path_metrics"]["worst_path"], "local-critical"
        )

    def test_lexicographic_acceptance_and_rollback(self) -> None:
        incumbent = {
            "objective_metrics": {
                "frame_slots": 32,
                "nominal_virtual_frequency_mhz": 7.8125,
                "estimated_runtime_slack_ns": 20.0,
                "estimated_runtime_closed": True,
                "worst_normalized_slack": -2.0,
                "total_negative_normalized_slack": -8.0,
                "negative_slack_paths": 4,
                "max_tdm_ratio": 8,
                "completion_slot": 20,
                "total_link_bit_hops": 30,
                "cut_bits": 20,
                "replica_luts": 0,
            }
        }
        improved = {
            "objective_metrics": {
                **incumbent["objective_metrics"],
                "worst_normalized_slack": -1.0,
            }
        }
        regressed = {
            "objective_metrics": {
                **incumbent["objective_metrics"],
                "worst_normalized_slack": -3.0,
            }
        }
        tied = {"objective_metrics": dict(incumbent["objective_metrics"])}
        faster = {
            "objective_metrics": {
                **regressed["objective_metrics"],
                "frame_slots": 31,
                "estimated_runtime_slack_ns": 1.0,
            }
        }
        slower_with_better_original_slack = {
            "objective_metrics": {
                **improved["objective_metrics"],
                "frame_slots": 33,
                "estimated_runtime_slack_ns": 24.0,
            }
        }
        self.assertTrue(
            compare_candidate_objectives(faster, incumbent)["accepted"]
        )
        self.assertFalse(
            compare_candidate_objectives(
                slower_with_better_original_slack, incumbent
            )["accepted"]
        )
        self.assertTrue(
            compare_candidate_objectives(improved, incumbent)["accepted"]
        )
        self.assertFalse(
            compare_candidate_objectives(regressed, incumbent)["accepted"]
        )
        self.assertFalse(
            compare_candidate_objectives(tied, incumbent)["accepted"]
        )

    def test_connected_rtl_baseline_transaction_is_reproducible(self) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("a C++17 compiler is required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir = import_yosys_json(
                ROOT / "examples" / "yosys" / "counter.json",
                top="counter",
                clocks=["clk"],
            )
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            initial_root = root / "initial"
            database_path = root / "database.json"
            board_link_timing_path = root / "board-link-timing.json"
            write_json(ir_path, ir.value)
            write_json(
                platform_path,
                _platform_value(
                    "connected_cross_stage",
                    ["a", "b"],
                    [_link("ab", "a", "b", lanes=8, latency=1)],
                ),
            )
            run_phase3(
                ir_path,
                platform_path,
                initial_root,
                provider="greedy",
                cut_mode="sequential-only",
                min_used_fpgas=2,
                balance_tolerance=1.0,
            )
            assignment = json.loads(
                (initial_root / "assignment.json").read_text()
            )
            link_timing = build_board_link_timing_model(
                Platform.load(platform_path)
            )
            for record in link_timing["links"]:
                record["delay_bound_ns"] = (
                    3.0 if record["from"] == "a" else 7.0
                )
            write_json(board_link_timing_path, link_timing)
            self.assertTrue(assignment["cut_nets"])
            path_nets = [net["id"] for net in ir.value["nets"]]
            write_json(
                database_path,
                {
                    "schema": "emuflow.sta-path-database/v1",
                    "design": "counter",
                    "source": {
                        "provider": "connected-rtl-fixture",
                        "input": "counter",
                    },
                    "normalization": {
                        "positive_slack_scale_ns": 10.0,
                        "negative_slack_scale_ns": 1.0,
                        "max_clock_period_ns": 20.0,
                    },
                    "paths": [
                        {
                            "id": "counter-critical",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 10.0,
                            "path_nets": path_nets,
                            "normalized_slack": 1.0,
                        }
                    ],
                },
            )
            router = root / "emuflow_tlr_router"
            ratio_optimizer = root / "emuflow_tdm_ratio_optimizer"
            timing_dag_optimizer = (
                root / "emuflow_tdm_timing_dag_optimizer"
            )
            feedback_optimizer = (
                root / "emuflow_tdm_partition_feedback"
            )
            for source, output in (
                ("tlr_router.cpp", router),
                ("tdm_ratio_optimizer.cpp", ratio_optimizer),
                (
                    "tdm_timing_dag_optimizer.cpp",
                    timing_dag_optimizer,
                ),
                ("tdm_partition_feedback.cpp", feedback_optimizer),
            ):
                subprocess.run(
                    [
                        compiler,
                        "-std=c++17",
                        "-O2",
                        str(ROOT / "src" / "native" / source),
                        "-o",
                        str(output),
                    ],
                    check=True,
                )
            reports = []
            for run in range(2):
                output = root / f"run_{run}"
                report = run_cross_stage_optimization(
                    ir_path=ir_path,
                    platform_path=platform_path,
                    database_path=database_path,
                    initial_assignment_path=(
                        initial_root / "assignment.json"
                    ),
                    output_dir=output,
                    board_link_timing_path=board_link_timing_path,
                    phase3_provider="greedy",
                    max_outer_iterations=1,
                    min_used_fpgas=2,
                    balance_tolerance=1.0,
                    router=str(router),
                    ratio_optimizer=str(ratio_optimizer),
                    timing_dag_optimizer=str(timing_dag_optimizer),
                    feedback_optimizer=str(feedback_optimizer),
                    simulation_frames=2,
                    max_ratio=8,
                    post_refinement_iterations=10,
                )
                checked = validate_cross_stage_report(
                    output / "cross_stage_report.json",
                    ir_path,
                    database_path,
                    platform_path,
                )
                self.assertEqual(checked["status"], "pass")
                self.assertEqual(report["selected_iteration"], 0)
                self.assertTrue(
                    report["configuration"]["board_link_timing"]
                )
                self.assertEqual(
                    report["board_link_timing"]["routing_projection"][
                        "directed_links"
                    ],
                    2,
                )
                routes = json.loads(
                    (
                        output
                        / report["candidates"][0]["routes"]
                    ).read_text()
                )
                self.assertEqual(
                    routes["constraints"]["directed_link_delay_ns"],
                    {"ab": {"a": {"b": 3.0}, "b": {"a": 7.0}}},
                )
                self.assertEqual(
                    report["termination"], "symmetry-stagnation"
                )
                self.assertEqual(
                    report["configuration"][
                        "partition_timeout_seconds"
                    ],
                    3600,
                )
                self.assertEqual(
                    report["configuration"]["partition_seed_attempts"],
                    1,
                )
                self.assertFalse(
                    report["configuration"][
                        "partition_repair_min_used_fpgas"
                    ]
                )
                self.assertFalse(
                    report["configuration"][
                        "partition_repair_balance"
                    ]
                )
                self.assertEqual(len(report["candidates"]), 5)
                self.assertEqual(
                    [
                        candidate["feedback_step"]
                        for candidate in report["candidates"][1:]
                    ],
                    [1.0, 0.5, 0.25, 0.125],
                )
                self.assertTrue(
                    all(
                        not candidate["decision"]["accepted"]
                        for candidate in report["candidates"][1:]
                    )
                )
                self.assertTrue(
                    all(
                        candidate["equivalent_partition_iteration"]
                        == 0
                        for candidate in report["candidates"][1:]
                    )
                )
                self.assertTrue(
                    all(
                        candidate["partition_migration"][
                            "moved_clusters"
                        ]
                        == 0
                        for candidate in report["candidates"][1:]
                    )
                )
                reports.append(report)
            self.assertEqual(
                reports[0]["selected_candidate_id"],
                reports[1]["selected_candidate_id"],
            )
            self.assertEqual(
                reports[0]["candidates"][0]["objective_key"],
                reports[1]["candidates"][0]["objective_key"],
            )
            seed_output = root / "seed_candidate"
            seed_report = run_cross_stage_optimization(
                ir_path=ir_path,
                platform_path=platform_path,
                database_path=database_path,
                initial_assignment_path=initial_root / "assignment.json",
                seed_candidate_phase3_root=initial_root,
                output_dir=seed_output,
                phase3_provider="greedy",
                max_outer_iterations=0,
                min_used_fpgas=2,
                balance_tolerance=1.0,
                router=str(router),
                ratio_optimizer=str(ratio_optimizer),
                timing_dag_optimizer=str(timing_dag_optimizer),
                feedback_optimizer=str(feedback_optimizer),
                simulation_frames=2,
                max_ratio=8,
                post_refinement_iterations=10,
            )
            self.assertEqual(len(seed_report["candidates"]), 2)
            self.assertEqual(
                seed_report["candidates"][1]["candidate_origin"], "seed"
            )
            self.assertEqual(
                seed_report["candidates"][1][
                    "equivalent_partition_iteration"
                ],
                0,
            )
            self.assertFalse(
                seed_report["candidates"][1]["decision"]["accepted"]
            )
            self.assertEqual(seed_report["selected_iteration"], 0)
            self.assertEqual(
                validate_cross_stage_report(
                    seed_output / "cross_stage_report.json",
                    ir_path,
                    database_path,
                    platform_path,
                )["status"],
                "pass",
            )
            tampered_seed = copy.deepcopy(seed_report)
            tampered_seed["source_sha256"][
                "seed_candidate_assignment"
            ] = "0" * 64
            tampered_seed_path = (
                seed_output / "tampered_seed_candidate_report.json"
            )
            write_json(tampered_seed_path, tampered_seed)
            with self.assertRaisesRegex(
                ValidationError, "seed candidate seal"
            ):
                validate_cross_stage_report(
                    tampered_seed_path,
                    ir_path,
                    database_path,
                    platform_path,
                )
            corrupted_link_timing = copy.deepcopy(reports[0])
            corrupted_link_timing["board_link_timing"][
                "routing_projection"
            ]["maximum_route_link_delay_ns"] += 1.0
            corrupted_link_timing_path = (
                root / "run_0" / "corrupted_link_timing_report.json"
            )
            write_json(
                corrupted_link_timing_path, corrupted_link_timing
            )
            with self.assertRaisesRegex(
                ValidationError,
                "board link timing reconstruction mismatch",
            ):
                validate_cross_stage_report(
                    corrupted_link_timing_path,
                    ir_path,
                    database_path,
                    platform_path,
                )
            corrupted = copy.deepcopy(reports[0])
            corrupted["candidates"][1]["feedback_validation"][
                "maximum_feedback_weight"
            ] += 0.1
            corrupted_path = root / "run_0" / "corrupted_report.json"
            write_json(corrupted_path, corrupted)
            with self.assertRaisesRegex(
                ValidationError, "damped feedback validation mismatch"
            ):
                validate_cross_stage_report(
                    corrupted_path,
                    ir_path,
                    database_path,
                    platform_path,
                )

            corrupted_class = copy.deepcopy(reports[0])
            corrupted_class["candidates"][0]["partition_class"][
                "sha256"
            ] = "0" * 64
            corrupted_class_path = (
                root / "run_0" / "corrupted_partition_class_report.json"
            )
            write_json(corrupted_class_path, corrupted_class)
            with self.assertRaisesRegex(
                ValidationError, "partition class mismatch"
            ):
                validate_cross_stage_report(
                    corrupted_class_path,
                    ir_path,
                    database_path,
                    platform_path,
                )

            optimized_root = root / "frame_optimized"
            optimized = run_cross_stage_optimization(
                ir_path=ir_path,
                platform_path=platform_path,
                database_path=database_path,
                initial_assignment_path=initial_root / "assignment.json",
                output_dir=optimized_root,
                phase3_provider="greedy",
                max_outer_iterations=0,
                min_used_fpgas=2,
                balance_tolerance=1.0,
                router=str(router),
                ratio_optimizer=str(ratio_optimizer),
                timing_dag_optimizer=str(timing_dag_optimizer),
                feedback_optimizer=str(feedback_optimizer),
                simulation_frames=2,
                frame_slots=16,
                optimize_frame_slots=True,
                max_ratio=8,
                post_refinement_iterations=10,
            )
            optimized_candidate = optimized["candidates"][0]
            frame_report_path = (
                optimized_root / optimized_candidate["frame_search"]
            )
            frame_report = json.loads(frame_report_path.read_text())
            selected_slots = frame_report["selected_frame_slots"]
            self.assertEqual(
                selected_slots,
                optimized_candidate["objective_metrics"]["frame_slots"],
            )
            self.assertTrue(
                optimized_candidate["objective_metrics"][
                    "estimated_runtime_closed"
                ]
            )
            if selected_slots > 2:
                frame_report["attempts"] = [
                    attempt
                    for attempt in frame_report["attempts"]
                    if attempt["frame_slots"] != selected_slots - 1
                ]
                frame_report["evaluated_candidates"] = len(
                    frame_report["attempts"]
                )
                write_json(frame_report_path, frame_report)
                with self.assertRaisesRegex(
                    ValidationError, "minimum boundary"
                ):
                    validate_cross_stage_report(
                        optimized_root / "cross_stage_report.json",
                        ir_path,
                        database_path,
                        platform_path,
                    )
            corrupted = copy.deepcopy(reports[0])
            corrupted["configuration"][
                "partition_timeout_seconds"
            ] = 0
            write_json(corrupted_path, corrupted)
            with self.assertRaisesRegex(
                ValidationError, "partition timeout is invalid"
            ):
                validate_cross_stage_report(
                    corrupted_path,
                    ir_path,
                    database_path,
                    platform_path,
                )


if __name__ == "__main__":
    unittest.main()
