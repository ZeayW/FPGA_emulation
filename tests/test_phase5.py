import copy
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest import mock

from emuflow.errors import (
    EmuFlowError,
    TDMScheduleInfeasibleError,
    ValidationError,
)
from emuflow.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.phase5 import run_phase5, validate_phase5
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from emuflow.tdm import (
    _exact_capture_certificate,
    build_tdm_schedule,
    reconstruct_tdm_schedule_timing,
    reconstruct_tdm_schedule_timing_paths,
    reconstruct_tdm_schedule_timing_paths_from_routes,
    schedule_to_systemverilog_testbench,
    simulate_tdm_schedule,
    validate_tdm_schedule,
)
from emuflow.tdm_ratio import (
    TDM_TIMING_DAG_RATIO_PROVIDER,
    _prepare_model,
    _round_barrier_legalize,
    build_tdm_ratio_plan,
    validate_tdm_ratio_plan,
)
from emuflow.tdm_slot import refine_tdm_schedule_native
from emuflow.tdm_cp_sat import (
    solve_cp_sat_slot_schedule,
    validate_cp_sat_slot_schedule,
)
from emuflow.tdm_timing_dag import (
    _build_dag,
    build_timing_dag_ratio_plan,
    build_timing_dag_ratio_seed,
    optimize_prepared_timing_dag,
    validate_timing_dag_seed,
)
from emuflow.tdm_oracle import (
    exact_discrete_ratio_legalization,
    exact_multi_round_slot_schedule,
    exact_single_round_slot_schedule,
    exact_timing_ratio_assignment,
    validate_exact_slot_schedule,
)
from emuflow.timing_routing import route_system_native
from tests.native_build import (
    tdm_ratio_optimizer,
    tdm_slot_optimizer,
    tdm_timing_dag_optimizer,
    tlr_router,
)


ROOT = Path(__file__).resolve().parents[1]


def _platform_value(name, fpga_ids, links):
    return {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "virtual",
            "description": "Phase 5 test topology",
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": "xcvu3p-ffvc1517-2-e",
                "utilization_limit": 1.0,
                "capacity": {"lut": 100, "ff": 100},
            }
            for fpga_id in fpga_ids
        ],
        "links": links,
    }


def _link(link_id, left, right, lanes=1, latency=1, direction="full_duplex"):
    return {
        "id": link_id,
        "endpoints": [left, right],
        "direction": direction,
        "mode": "abstract",
        "data_lanes_per_direction": lanes,
        "fabric_clock_mhz": 250.0,
        "latency_cycles": latency,
    }


def _assignment(platform, cuts):
    return {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": "tdm_test",
        "platform": platform.name,
        "cut_nets": [
            {
                "net": net,
                "cut_class": "register_output",
                "source_fpgas": [source],
                "sink_fpgas": list(sinks),
                "sink_endpoints": len(sinks),
            }
            for net, source, sinks in cuts
        ],
    }


def _routes(platform, cuts, frame_slots):
    assignment = _assignment(platform, cuts)
    constraints = normalize_route_constraints(
        {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": frame_slots,
        },
        platform,
    )
    return route_system_native(
        assignment,
        platform,
        constraints,
        executable=str(tlr_router()),
    )


def _candidate_fallback_inputs(root):
    platform = Platform.from_dict(
        _platform_value(
            "candidate_fallback",
            ["a", "b"],
            [_link("ab", "a", "b")],
        )
    )
    routes_path = root / "routes.json"
    platform_path = root / "platform.json"
    routes_path.write_text(json.dumps({"timing": {}}), encoding="utf-8")
    platform_path.write_text(
        json.dumps(platform.to_dict()), encoding="utf-8"
    )
    return routes_path, platform_path


def _candidate_fallback_schedule():
    return {
        "design": "candidate_fallback",
        "provider": "test-schedule-provider",
        "metrics": {"completion_slot": 1},
    }


def _candidate_fallback_patches(schedule_side_effect):
    plan = {
        "provider": "test-ratio-provider",
        "metrics": {
            "discrete_worst_normalized_slack": 0.0,
            "dp_legalized_domains": 0,
            "greedy_legalized_domains": 1,
        },
    }
    timing = {
        "worst_normalized_slack": 0.0,
        "p01_normalized_slack": 0.0,
        "median_normalized_slack": 0.0,
    }
    plan_builder = mock.Mock(return_value=plan)
    patcher = mock.patch.multiple(
        "emuflow.phase5",
        _prepare_model=mock.Mock(return_value={}),
        build_timing_dag_ratio_plan=plan_builder,
        validate_tdm_ratio_plan=mock.Mock(return_value={"status": "pass"}),
        build_tdm_schedule=mock.Mock(side_effect=schedule_side_effect),
        validate_tdm_schedule=mock.Mock(return_value={"status": "pass"}),
        reconstruct_tdm_schedule_timing=mock.Mock(return_value=timing),
        simulate_tdm_schedule=mock.Mock(return_value={"status": "pass"}),
        build_transport_manifest=mock.Mock(return_value={}),
        build_tdm_feedback=mock.Mock(return_value={}),
        validate_tdm_feedback=mock.Mock(return_value={"status": "pass"}),
        schedule_to_tsv=mock.Mock(return_value=""),
        schedule_to_systemverilog_testbench=mock.Mock(return_value=""),
    )
    return patcher, plan_builder


class Phase5Test(unittest.TestCase):
    def test_exact_capture_certificate_indexes_100k_segments_once(self) -> None:
        class CountingSegments(dict):
            def __init__(self, value):
                super().__init__(value)
                self.values_calls = 0

            def values(self):
                self.values_calls += 1
                return super().values()

        count = 100_000
        captures = {
            f"capture-{index}": {
                "id": f"capture-{index}",
                "cut_net": f"net-{index}",
                "fpga": "fpga1",
            }
            for index in range(count)
        }
        segments = CountingSegments(
            {
                f"segment-{index}": {
                    "id": f"segment-{index}",
                    "kind": "rx_to_capture",
                    "capture_requirement": f"capture-{index}",
                    "source_cut_net": f"net-{index}",
                    "fpga": "fpga1",
                    "budget_slots": 1,
                }
                for index in range(count)
            }
        )
        arrivals = {
            (f"net-{index}", "fpga1"): 1 for index in range(count)
        }
        records, minimum = _exact_capture_certificate(
            {"commit_slot": 3}, segments, captures, arrivals
        )
        self.assertEqual(len(records), count)
        self.assertEqual(minimum, 1)
        self.assertEqual(segments.values_calls, 1)

    def test_phase5_skips_infeasible_candidate_and_uses_next_strategy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path, platform_path = _candidate_fallback_inputs(root)
            patcher, plan_builder = _candidate_fallback_patches(
                [
                    TDMScheduleInfeasibleError(
                        "exact candidate has no legal schedule"
                    ),
                    _candidate_fallback_schedule(),
                ]
            )
            with patcher:
                report = run_phase5(
                    routes_path=routes_path,
                    platform_path=platform_path,
                    output_dir=root / "phase5",
                )

        self.assertEqual(plan_builder.call_count, 2)
        self.assertEqual(
            report["candidate_selection"]["selected"],
            "scalable-minimum-wire",
        )
        candidates = report["candidate_selection"]["candidates"]
        self.assertEqual(
            [candidate["strategy"] for candidate in candidates],
            ["scalable-minimum-wire"],
        )
        self.assertEqual(
            report["candidate_selection"]["rejected_candidates"],
            [
                {
                    "strategy": "exact-displacement-dp",
                    "status": "infeasible",
                    "reason": "exact candidate has no legal schedule",
                }
            ],
        )

    def test_phase5_does_not_hide_candidate_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path, platform_path = _candidate_fallback_inputs(root)
            patcher, plan_builder = _candidate_fallback_patches(
                ValidationError("candidate certificate is invalid")
            )
            with patcher:
                with self.assertRaisesRegex(
                    ValidationError,
                    "candidate certificate is invalid",
                ) as raised:
                    run_phase5(
                        routes_path=routes_path,
                        platform_path=platform_path,
                        output_dir=root / "phase5",
                    )

        self.assertNotIsInstance(
            raised.exception, TDMScheduleInfeasibleError
        )
        self.assertEqual(plan_builder.call_count, 1)

    def test_phase5_reports_all_infeasible_candidate_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path, platform_path = _candidate_fallback_inputs(root)
            patcher, plan_builder = _candidate_fallback_patches(
                [
                    TDMScheduleInfeasibleError("exact schedule failed"),
                    TDMScheduleInfeasibleError("scalable schedule failed"),
                ]
            )
            with patcher:
                with self.assertRaisesRegex(
                    TDMScheduleInfeasibleError,
                    "all academic Phase 5 candidates are infeasible",
                ) as raised:
                    run_phase5(
                        routes_path=routes_path,
                        platform_path=platform_path,
                        output_dir=root / "phase5",
                    )

        self.assertEqual(plan_builder.call_count, 2)
        self.assertIn(
            "exact-displacement-dp: exact schedule failed",
            str(raised.exception),
        )
        self.assertIn(
            "scalable-minimum-wire: scalable schedule failed",
            str(raised.exception),
        )

    def test_native_slot_optimizer_compacts_sparse_lane_ids(self) -> None:
        source = (
            ROOT / "src" / "native" / "tdm_slot_optimizer.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("lane_resource_count", source)
        self.assertIn("lane_resource_index", source)
        self.assertIn("occupancy_capacity", source)
        self.assertNotIn(
            "static_cast<std::size_t>(model.lane_resource_count) *\n"
            "          model.frame_slots",
            source,
        )
        self.assertNotIn(
            "const int lane_domains = (maximum_domain + 1) * lane_stride",
            source,
        )

    def test_cp_sat_medium_oracle_matches_exhaustive_oracle(self) -> None:
        try:
            from ortools.sat.python import cp_model  # noqa: F401
        except ImportError:
            self.skipTest("optional OR-Tools CP-SAT dependency is absent")
        platform = Platform.from_dict(
            _platform_value(
                "cp_sat",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=2, latency=1),
                    _link("bc", "b", "c", lanes=2, latency=1),
                ],
            )
        )
        routes = _routes(
            platform,
            [
                ("ab0", "a", ["b"]),
                ("ab1", "a", ["b"]),
                ("bc0", "b", ["c"]),
                ("bc1", "b", ["c"]),
            ],
            frame_slots=10,
        )
        routes["timing"] = {
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 40.0,
            },
            "paths": [
                {
                    "path": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 40.0,
                    "fixed_delay_ns": 2.0,
                    "cut_nets": ["ab1", "bc0"],
                    "cut_transitions": [
                        {"net": "ab1", "from": "a", "to": "b"},
                        {"net": "bc0", "from": "b", "to": "c"},
                    ],
                },
                {
                    "path": "p1",
                    "clock_domain": "clk",
                    "clock_period_ns": 40.0,
                    "fixed_delay_ns": 5.0,
                    "cut_nets": ["ab0", "bc1"],
                    "cut_transitions": [
                        {"net": "ab0", "from": "a", "to": "b"},
                        {"net": "bc1", "from": "b", "to": "c"},
                    ],
                },
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=4,
            ratio_quantum=2,
        )
        exhaustive = exact_multi_round_slot_schedule(
            routes, platform, plan, max_hops=6
        )
        cp_sat = solve_cp_sat_slot_schedule(
            routes, platform, plan, max_hops=32
        )
        self.assertEqual(
            validate_cp_sat_slot_schedule(
                routes, platform, plan, cp_sat
            )["status"],
            "pass",
        )
        self.assertAlmostEqual(
            cp_sat["worst_normalized_slack"],
            exhaustive["worst_normalized_slack"],
        )
        self.assertEqual(
            cp_sat["completion_slot"], exhaustive["completion_slot"]
        )
        self.assertEqual(
            cp_sat["total_wait_slots"], exhaustive["total_wait_slots"]
        )
        tampered = copy.deepcopy(cp_sat)
        first = min(tampered["slot_by_hop"])
        tampered["slot_by_hop"][first] += 1
        with self.assertRaises(ValidationError):
            validate_cp_sat_slot_schedule(
                routes, platform, plan, tampered
            )

    def test_clock_protocol_compatibility_separates_lane_groups(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "compatibility",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
            frame_slots=8,
        )
        for route, domain in zip(
            sorted(routes["routes"], key=lambda item: item["net"]),
            ("source-synchronous", "global-frame-cdc"),
        ):
            route["tdm_compatibility"] = domain
        routes["timing"] = {
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 100.0,
            },
            "paths": [
                {
                    "path": f"p{index}",
                    "clock_domain": clock,
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": [f"n{index}"],
                    "cut_transitions": [
                        {"net": f"n{index}", "from": "a", "to": "b"}
                    ],
                }
                for index, clock in enumerate(("clk0", "clk1"))
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=8,
        )
        self.assertEqual(
            validate_tdm_ratio_plan(routes, platform, plan)["status"],
            "pass",
        )
        self.assertEqual(len(plan["compatibility"]["classes"]), 2)
        self.assertEqual(len({hop["lane"] for hop in plan["hops"]}), 2)

        tampered = copy.deepcopy(plan)
        tampered["compatibility"]["hops"][0]["compatibility"] += 2
        with self.assertRaisesRegex(ValidationError, "compatibility"):
            validate_tdm_ratio_plan(routes, platform, tampered)

        one_lane = Platform.from_dict(
            _platform_value(
                "compatibility",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1)],
            )
        )
        with self.assertRaisesRegex(
            EmuFlowError, "groups cannot fit domain lane budget"
        ):
            build_tdm_ratio_plan(
                routes,
                one_lane,
                executable=str(tdm_ratio_optimizer()),
                max_ratio=8,
            )

    def _timing_dag_fixture(self):
        platform = Platform.from_dict(
            _platform_value(
                "timing_dag",
                ["a", "b"],
                [_link("ab", "a", "b")],
            )
        )
        routes = {
            "schema": "emuflow.system-routes/v1",
            "design": "tdm_test",
            "platform": platform.name,
            "constraints": {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 32,
            },
            "routes": [
                {
                    "id": f"d{index}",
                    "net": f"n{index}",
                    "source": "a",
                    "sinks": ["b"],
                    "tree_edges": [
                        {"link": "ab", "from": "a", "to": "b"}
                    ],
                }
                for index in range(3)
            ],
            "timing": {
                "normalization": {
                    "positive_slack_scale_ns": 1.0,
                    "negative_slack_scale_ns": 1.0,
                    "max_clock_period_ns": 100.0,
                },
                "paths": [
                    {
                        "path": f"p{index}",
                        "clock_domain": "clk",
                        "clock_period_ns": 100.0,
                        "fixed_delay_ns": fixed,
                        "cut_nets": [f"n{index}"],
                        "cut_transitions": [
                            {
                                "net": f"n{index}",
                                "from": "a",
                                "to": "b",
                            }
                        ],
                    }
                    for index, fixed in enumerate((0.0, 8.0))
                ],
            },
        }
        return routes, platform

    def test_timing_dag_equations_improve_contended_uniform_seed(self) -> None:
        routes, platform = self._timing_dag_fixture()
        result = build_timing_dag_ratio_seed(
            routes,
            platform,
            executable=str(tdm_timing_dag_optimizer()),
            max_iterations=500,
            max_ratio=32.0,
        )
        validation = result["validation"]
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(result["equations"], [8, 13, 16, 17, 19, 20])
        self.assertEqual(validation["covered_hops"], 2)
        self.assertEqual(validation["uncovered_hops"], 1)
        self.assertLess(validation["worst_delay_ns"], 16.0)
        self.assertAlmostEqual(
            result["domains"][0]["usage"], 1.0, places=9
        )
        self.assertLessEqual(
            result["metrics"]["max_flow_conservation_error"], 1.0e-12
        )

    def test_timing_dag_independent_checker_rejects_mu_corruption(self) -> None:
        routes, platform = self._timing_dag_fixture()
        result = build_timing_dag_ratio_seed(
            routes,
            platform,
            executable=str(tdm_timing_dag_optimizer()),
        )
        corrupted = copy.deepcopy(result)
        corrupted["edge_mu"][0] += 0.125
        model = _prepare_model(routes, platform)
        with self.assertRaisesRegex(ValidationError, "Eq. 16/17"):
            validate_timing_dag_seed(model, _build_dag(model), corrupted)

    def test_timing_dag_capacity_check_scans_hops_once(self) -> None:
        routes, platform = self._timing_dag_fixture()
        result = build_timing_dag_ratio_seed(
            routes,
            platform,
            executable=str(tdm_timing_dag_optimizer()),
        )
        model = _prepare_model(routes, platform)
        dag = _build_dag(model)

        class CountedHops(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        counted = CountedHops(model["hops"])
        model["hops"] = counted
        validation = validate_timing_dag_seed(model, dag, result)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(counted.iterations, 1)

    def test_timing_dag_residual_scaling_honors_nonunit_min_ratio(self) -> None:
        model = {
            "domains": [{"index": 0, "lanes": 1}],
            "hops": [
                {
                    "index": index,
                    "domain": 0,
                    "direction": 0,
                    "base_delay_ns": 1.5,
                    "beta_ns": 1.0,
                }
                for index in range(5)
            ],
            "timing_paths": [
                {
                    "index": index,
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": float(index),
                    "hops": [index],
                }
                for index in range(5)
            ],
            "normalization": {
                "positive_slack_scale_ns": 1.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 100.0,
            },
        }
        result = optimize_prepared_timing_dag(
            model,
            executable=str(tdm_timing_dag_optimizer()),
            min_ratio=4.0,
            max_ratio=32.0,
        )
        self.assertEqual(result["validation"]["status"], "pass")
        self.assertAlmostEqual(result["domains"][0]["usage"], 1.0, places=9)

    def test_timing_dag_seed_uses_checked_discrete_legalization(self) -> None:
        routes, platform = self._timing_dag_fixture()
        plan = build_timing_dag_ratio_plan(
            routes,
            platform,
            dag_executable=str(tdm_timing_dag_optimizer()),
            legalization_executable=str(tdm_ratio_optimizer()),
            max_ratio=32,
            ratio_quantum=8,
            post_refinement_iterations=20,
        )
        self.assertEqual(
            plan["provider"], "aspdac26-timing-dag-lagrangian-v1"
        )
        validation = validate_tdm_ratio_plan(routes, platform, plan)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(
            validation["provider"],
            "aspdac26-timing-dag-lagrangian-v1",
        )
        continuous = [hop["continuous_ratio"] for hop in plan["hops"]]
        discrete = [hop["discrete_ratio"] for hop in plan["hops"]]
        bound = max(
            abs(before - after)
            for before, after in zip(continuous, discrete)
        )
        oracle = exact_discrete_ratio_legalization(
            continuous,
            [hop["direction"] for hop in plan["hops"]],
            lanes=1,
            allowed_ratios=[1, 8, 16, 24, 32],
            displacement_bound=bound,
        )
        self.assertEqual(discrete, oracle["discrete_ratios"])
        timing_oracle = exact_timing_ratio_assignment(
            _prepare_model(routes, platform),
            [1, 8, 16, 24, 32],
        )
        self.assertEqual(timing_oracle["status"], "optimal")
        self.assertEqual(discrete, timing_oracle["discrete_ratios"])
        self.assertAlmostEqual(
            plan["metrics"]["discrete_worst_normalized_slack"],
            timing_oracle["worst_normalized_slack"],
        )

    def test_timing_dag_plan_derives_schedule_safe_max_ratio(self) -> None:
        routes, platform = self._timing_dag_fixture()
        plan = build_timing_dag_ratio_plan(
            routes,
            platform,
            dag_executable=str(tdm_timing_dag_optimizer()),
            legalization_executable=str(tdm_ratio_optimizer()),
            ratio_quantum=8,
            post_refinement_iterations=20,
        )
        # The 32-slot frame and one-cycle link latency leave 31 usable
        # launch slots; the largest legal ratio quantum is therefore 24.
        self.assertEqual(plan["configuration"]["max_ratio"], 24)
        self.assertEqual(
            validate_tdm_ratio_plan(routes, platform, plan)["status"],
            "pass",
        )

    def test_phase5_inherits_nonunit_ratio_domain_from_routes(self) -> None:
        routes, platform = self._timing_dag_fixture()
        routes["constraints"]["tdm_min_ratio"] = 4
        routes["constraints"]["tdm_ratio_quantum"] = 4
        plan = build_timing_dag_ratio_plan(
            routes,
            platform,
            dag_executable=str(tdm_timing_dag_optimizer()),
            legalization_executable=str(tdm_ratio_optimizer()),
            post_refinement_iterations=20,
        )
        self.assertEqual(plan["configuration"]["min_ratio"], 4)
        self.assertEqual(plan["configuration"]["ratio_quantum"], 4)
        self.assertEqual(plan["configuration"]["max_ratio"], 28)
        self.assertTrue(
            all(
                hop["continuous_ratio"] >= 4.0
                and hop["discrete_ratio"] >= 4
                and hop["discrete_ratio"] % 4 == 0
                for hop in plan["hops"]
            )
        )
        self.assertEqual(
            validate_tdm_ratio_plan(routes, platform, plan)["status"],
            "pass",
        )
        tampered = copy.deepcopy(plan)
        tampered["hops"][0]["discrete_ratio"] = 1
        with self.assertRaisesRegex(ValidationError, "discrete ratio"):
            validate_tdm_ratio_plan(routes, platform, tampered)
        rebound = copy.deepcopy(plan)
        rebound["configuration"]["min_ratio"] = 1
        with self.assertRaisesRegex(ValidationError, "differs from routes"):
            validate_tdm_ratio_plan(routes, platform, rebound)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path = root / "routes.json"
            platform_path = root / "platform.json"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            report = run_phase5(
                routes_path,
                platform_path,
                root / "phase5",
                provider="aspdac26-timing-dag-lagrangian-v1",
                ratio_optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                max_ratio=28,
                post_refinement_iterations=20,
            )
            self.assertEqual(report["status"], "pass")
            persisted = json.loads(
                (root / "phase5" / "ratio_plan.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted["configuration"]["min_ratio"], 4)
            self.assertEqual(
                persisted["configuration"]["ratio_quantum"], 4
            )

    def test_phase5_accepts_timing_dag_provider_end_to_end(self) -> None:
        routes, _platform = self._timing_dag_fixture()
        platform_value = _platform_value(
            "timing_dag", ["a", "b"], [_link("ab", "a", "b")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path = root / "routes.json"
            platform_path = root / "platform.json"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            platform_path.write_text(
                json.dumps(platform_value), encoding="utf-8"
            )
            with mock.patch(
                "emuflow.phase5._prepare_model", wraps=_prepare_model
            ) as prepare_model, mock.patch(
                "emuflow.tdm_ratio._prepare_model",
                side_effect=AssertionError("ratio model rebuilt"),
            ), mock.patch(
                "emuflow.tdm_timing_dag._prepare_model",
                side_effect=AssertionError("timing-DAG model rebuilt"),
            ):
                report = run_phase5(
                    routes_path,
                    platform_path,
                    root / "phase5",
                    simulation_frames=2,
                    provider="aspdac26-timing-dag-lagrangian-v1",
                    ratio_optimizer=str(tdm_ratio_optimizer()),
                    timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                    slot_optimizer=str(tdm_slot_optimizer()),
                    max_ratio=32,
                    post_refinement_iterations=20,
                    slot_refinement_iterations=1,
                )
            self.assertEqual(prepare_model.call_count, 1)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["optimization_provider"],
                "aspdac26-timing-dag-lagrangian-v1",
            )
            self.assertEqual(
                report["ratio_validation"]["status"], "pass"
            )
            with mock.patch(
                "emuflow.phase5._prepare_model", wraps=_prepare_model
            ) as validate_prepare_model:
                validation = validate_phase5(
                    routes_path,
                    platform_path,
                    root / "phase5" / "schedule.json",
                    root / "phase5" / "ratio_plan.json",
                )
            self.assertEqual(validate_prepare_model.call_count, 1)
            self.assertEqual(validation["status"], "pass")

    def test_baseline_phase5_prepares_large_timing_model_once(self) -> None:
        routes, _platform = self._timing_dag_fixture()
        platform_value = _platform_value(
            "timing_dag", ["a", "b"], [_link("ab", "a", "b")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path = root / "routes.json"
            platform_path = root / "platform.json"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            platform_path.write_text(
                json.dumps(platform_value), encoding="utf-8"
            )
            with mock.patch(
                "emuflow.phase5._prepare_model", wraps=_prepare_model
            ) as prepare_model, mock.patch(
                "emuflow.tdm_ratio._prepare_model",
                side_effect=AssertionError("timing model rebuilt"),
            ):
                report = run_phase5(
                    routes_path,
                    platform_path,
                    root / "phase5",
                    simulation_frames=1,
                    provider="deterministic-round-barrier-earliest-slot-v2",
                )
            self.assertEqual(prepare_model.call_count, 0)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["tdm_feedback_validation"]["status"], "pass")

    def test_sparse_baseline_timing_matches_dense_optimizer_model(self) -> None:
        routes, platform = self._timing_dag_fixture()
        schedule = build_tdm_schedule(routes, platform)
        dense = reconstruct_tdm_schedule_timing_paths(
            routes,
            platform,
            schedule,
            model=_prepare_model(routes, platform),
        )
        sparse = reconstruct_tdm_schedule_timing_paths_from_routes(
            routes, platform, schedule
        )
        self.assertEqual(sparse, dense)

    def test_native_ratio_capacity_product_uses_64_bit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native_input = root / "ratio.in"
            native_output = root / "ratio.out"
            native_input.write_text(
                "\n".join(
                    [
                        "EMUFLOW_TDM_RATIO_INPUT_V3",
                        "PARAM 1 500000 4 4 0 0 0 1e-9 1 1 1000000",
                        "DOMAIN 0 5000",
                        "HOP 0 0 0 1.5 1",
                        "PATH 0 1000000 0 0",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    str(tdm_ratio_optimizer()),
                    str(native_input),
                    str(native_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(
                native_output.read_text(encoding="utf-8").startswith(
                    "EMUFLOW_TDM_RATIO_OUTPUT_V1\n"
                )
            )

    def test_native_ratio_large_domain_radix_legalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native_input = root / "ratio.in"
            native_output = root / "ratio.out"
            records = [
                "EMUFLOW_TDM_RATIO_INPUT_V3",
                "PARAM 1 64 4 4 0 0 0 1e-9 1 1 10000",
                "DOMAIN 0 100",
            ]
            records.extend(
                f"HOP {index} 0 0 1.5 0.5" for index in range(5000)
            )
            records.extend(
                f"PATH {index} 10000 0 {index}" for index in range(5000)
            )
            native_input.write_text(
                "\n".join(records) + "\n", encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    str(tdm_ratio_optimizer()),
                    str(native_input),
                    str(native_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = native_output.read_text(encoding="utf-8")
            self.assertIn("METRIC greedy_legalized_domains 1\n", output)
            self.assertIn("METRIC max_discrete_ratio 52\n", output)

    def test_ratio_model_uses_member_specific_multicast_sink(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "member_sink",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b"),
                    _link("ac", "a", "c"),
                ],
            )
        )
        routes = {
            "schema": "emuflow.system-routes/v1",
            "design": "tdm_test",
            "platform": platform.name,
            "constraints": {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
            },
            "routes": [
                {
                    "id": "d0",
                    "net": "multicast",
                    "source": "a",
                    "sinks": ["b", "c"],
                    "tree_edges": [
                        {"link": "ab", "from": "a", "to": "b"},
                        {"link": "ac", "from": "a", "to": "c"},
                    ],
                }
            ],
            "timing": {
                "normalization": {
                    "positive_slack_scale_ns": 1.0,
                    "negative_slack_scale_ns": 1.0,
                    "max_clock_period_ns": 10.0,
                },
                "paths": [
                    {
                        "path": "to-c",
                        "clock_domain": "clk",
                        "clock_period_ns": 10.0,
                        "fixed_delay_ns": 1.0,
                        "cut_nets": ["multicast"],
                        "cut_transitions": [
                            {"net": "multicast", "from": "a", "to": "c"}
                        ],
                    }
                ],
            },
        }
        model = _prepare_model(routes, platform)
        selected = model["timing_paths"][0]["hops"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(model["hops"][selected[0]]["to"], "c")
        self.assertEqual(
            model["timing_paths"][0]["cut_transitions"][0]["to"],
            "c",
        )

    def test_academic_ratio_optimizer_drives_lane_and_slot_schedule(
        self,
    ) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("a C++17 compiler is required")
        platform = Platform.from_dict(
            _platform_value(
                "ratio",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [
                (f"n{index:02d}", "a", ["b"])
                for index in range(17)
            ],
            frame_slots=32,
        )
        for route in routes["routes"]:
            route["transport_round"] = (
                0 if int(route["net"][1:]) < 8 else 1
            )
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 100.0,
            },
            "compression": {
                "original_paths": 2,
                "compressed_paths": 2,
            },
            "paths": [
                {
                    "path": "critical",
                    "clock_domain": "fast",
                    "clock_period_ns": 20.0,
                    "fixed_delay_ns": 12.0,
                    "cut_nets": ["n16"],
                },
                {
                    "path": "relaxed",
                    "clock_domain": "slow",
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["n01"],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "emuflow_tdm_ratio_optimizer"
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
                max_ratio=16,
                post_refinement_iterations=20,
            )
            repeated = build_tdm_ratio_plan(
                routes,
                platform,
                executable=str(executable),
                max_ratio=16,
                post_refinement_iterations=20,
            )
            self.assertEqual(plan, repeated)
            plan_validation = validate_tdm_ratio_plan(
                routes, platform, plan
            )
            self.assertEqual(plan_validation["status"], "pass")
            self.assertEqual(
                plan["round_barrier_legalization"]["active_rounds"],
                [0, 1],
            )
            self.assertIsNotNone(
                plan["round_barrier_legalization"][
                    "source_ready_slot"
                ]
            )
            domain_by_index = {
                domain["index"]: domain for domain in plan["domains"]
            }
            latency_by_link = {
                link.id: link.latency_cycles for link in platform.links
            }
            bucket_counts = defaultdict(Counter)
            for hop in plan["hops"]:
                bucket_counts[
                    (
                        hop["domain"],
                        hop["direction"],
                        hop["discrete_ratio"],
                    )
                ][hop["transport_round"]] += 1
            frame_slots = routes["constraints"]["frame_slots"]
            minimum_latency = max(latency_by_link.values())
            brute_candidates = []
            for source_ready in range(
                minimum_latency + 1,
                frame_slots - 1 - minimum_latency,
            ):
                required = {}
                for domain, domain_record in domain_by_index.items():
                    latency = latency_by_link[domain_record["link"]]
                    total_slots = frame_slots - 1 - 2 * latency
                    required[domain] = sum(
                        max(
                            math.ceil(
                                sum(counts.values())
                                / min(ratio, total_slots)
                            ),
                            math.ceil(
                                counts.get(0, 0)
                                / (source_ready - latency)
                            ),
                            math.ceil(
                                counts.get(1, 0)
                                / (frame_slots - 1 - latency - source_ready)
                            ),
                        )
                        for (bucket_domain, _direction, ratio), counts
                        in bucket_counts.items()
                        if bucket_domain == domain
                    )
                excesses = [
                    max(
                        0,
                        required[domain]
                        - domain_by_index[domain]["lanes"],
                    )
                    for domain in required
                ]
                brute_candidates.append(
                    (
                        (
                            max(excesses, default=0),
                            sum(excesses),
                            max(required.values(), default=0),
                            sum(required.values()),
                            abs(source_ready - frame_slots // 2),
                        ),
                        source_ready,
                    )
                )
            self.assertEqual(
                plan["round_barrier_legalization"]["source_ready_slot"],
                min(brute_candidates)[1],
            )
            by_net = {hop["net"]: hop for hop in plan["hops"]}
            self.assertEqual(by_net["n16"]["discrete_ratio"], 1)
            self.assertEqual(by_net["n01"]["discrete_ratio"], 16)
            self.assertNotEqual(
                by_net["n16"]["lane"], by_net["n01"]["lane"]
            )

            baseline_schedule = build_tdm_schedule(routes, platform)
            baseline_timing = reconstruct_tdm_schedule_timing(
                routes, platform, baseline_schedule
            )
            schedule = build_tdm_schedule(routes, platform, plan)
            validation = validate_tdm_schedule(
                routes, platform, schedule, plan
            )
            timing_validation = reconstruct_tdm_schedule_timing(
                routes, platform, schedule
            )
            simulation = simulate_tdm_schedule(
                routes, schedule, frames=7
            )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["ratio_constrained_hops"], 17)
            self.assertEqual(validation["round_barriers"], 1)
            self.assertEqual(timing_validation["status"], "pass")
            self.assertEqual(timing_validation["timing_paths"], 2)
            self.assertEqual(
                timing_validation["worst_path"], "critical"
            )
            self.assertAlmostEqual(
                timing_validation["worst_delay_ns"], 16.0
            )
            self.assertAlmostEqual(
                timing_validation["worst_slack_ns"], 4.0
            )
            self.assertGreater(
                timing_validation["worst_normalized_slack"],
                baseline_timing["worst_normalized_slack"],
            )
            self.assertEqual(simulation["delivered_sink_values"], 119)
            entry_by_net = {
                entry["net"]: entry for entry in schedule["entries"]
            }
            self.assertEqual(
                entry_by_net["n16"]["lane"], by_net["n16"]["lane"]
            )
            self.assertLess(
                entry_by_net["n16"]["ratio_wait_slots"],
                entry_by_net["n16"]["tdm_ratio"],
            )

            broken_plan = copy.deepcopy(plan)
            broken_plan["timing_paths"][0][
                "normalized_slack"
            ] += 0.25
            with self.assertRaisesRegex(
                ValidationError,
                "does not match independent recomputation",
            ):
                validate_tdm_ratio_plan(
                    routes, platform, broken_plan
                )

            broken_schedule = copy.deepcopy(schedule)
            broken_schedule["entries"][0]["lane"] = (
                1 - broken_schedule["entries"][0]["lane"]
            )
            with self.assertRaisesRegex(
                ValidationError, "does not match ratio plan"
            ):
                validate_tdm_schedule(
                    routes, platform, broken_schedule, plan
                )

            routes_path = root / "routes.json"
            platform_path = root / "platform.json"
            routes_path.write_text(
                json.dumps(routes), encoding="utf-8"
            )
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            report = run_phase5(
                routes_path=routes_path,
                platform_path=platform_path,
                output_dir=root / "phase5",
                simulation_frames=7,
                ratio_optimizer=str(executable),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                slot_optimizer=str(tdm_slot_optimizer()),
                max_ratio=16,
                post_refinement_iterations=20,
                slot_refinement_iterations=20,
            )
            self.assertEqual(
                report["optimization_provider"],
                TDM_TIMING_DAG_RATIO_PROVIDER,
            )
            self.assertGreaterEqual(
                report["timing_validation"][
                    "worst_normalized_slack"
                ],
                timing_validation["worst_normalized_slack"],
            )
            optimized_schedule = json.loads(
                (root / "phase5" / "schedule.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("slot_optimization", optimized_schedule)
            self.assertEqual(
                report["candidate_selection"]["selected"],
                "exact-displacement-dp",
            )
            self.assertEqual(
                len(report["candidate_selection"]["candidates"]), 2
            )
            self.assertTrue(
                (root / "phase5" / "ratio_plan.json").is_file()
            )

    def test_academic_ratio_optimizer_accepts_round_one_only(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "round_one_only",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("registered_cut", "a", ["b"])],
            frame_slots=16,
        )
        routes["routes"][0]["transport_round"] = 1
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 10.0,
                "max_clock_period_ns": 10.0,
            },
            "compression": {
                "original_paths": 1,
                "compressed_paths": 1,
            },
            "paths": [
                {
                    "path": "registered_path",
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "fixed_delay_ns": 1.0,
                    "cut_nets": ["registered_cut"],
                }
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=4,
            ratio_quantum=4,
            post_refinement_iterations=0,
        )
        self.assertEqual(
            plan["round_barrier_legalization"]["active_rounds"],
            [1],
        )
        self.assertEqual(
            validate_tdm_ratio_plan(routes, platform, plan)["status"],
            "pass",
        )

    def test_round_barrier_promotion_can_migrate_the_boundary(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "boundary_migration",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        hops = []
        for ratio, round_counts in ((1, (0, 1)), (2, (1, 3))):
            for transport_round, count in enumerate(round_counts):
                for _ in range(count):
                    index = len(hops)
                    hops.append(
                        {
                            "index": index,
                            "domain": 0,
                            "direction": 0,
                            "discrete_ratio": ratio,
                            "transport_round": transport_round,
                            "demand": index,
                            "hop": 0,
                            "lane": 0,
                            "beta_ns": 1.0,
                            "base_delay_ns": 1.0,
                            "net": f"n{index}",
                            "capacity_key": "ab:a->b",
                            "link": "ab",
                        }
                    )
        model = {
            "constraints": {"frame_slots": 8},
            "domains": [{"index": 0, "link": "ab", "lanes": 2}],
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 10.0,
                "max_clock_period_ns": 10.0,
            },
            "timing_paths": [
                {
                    "path": f"p{index}",
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "fixed_delay_ns": 0.0,
                    "hops": [index],
                }
                for index in range(len(hops))
            ],
        }

        legalization = _round_barrier_legalize(
            hops, model, platform, max_ratio=8, ratio_quantum=2
        )

        self.assertEqual(legalization["source_ready_slot"], 3)
        self.assertEqual(legalization["promotion_steps"], 1)
        self.assertEqual(legalization["promoted_hops"], 4)
        self.assertEqual(
            [(hop["transport_round"], hop["discrete_ratio"]) for hop in hops],
            [(1, 1), (0, 4), (1, 4), (1, 4), (1, 4)],
        )

    def test_academic_post_refinement_improves_worst_slack(
        self,
    ) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("a C++17 compiler is required")
        platform = Platform.from_dict(
            _platform_value(
                "post_refinement",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=5, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [(f"n{index}", "a", ["b"]) for index in range(6)],
            frame_slots=64,
        )
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 100.0,
            },
            "compression": {
                "original_paths": 2,
                "compressed_paths": 2,
            },
            "paths": [
                {
                    "path": "short_period",
                    "clock_domain": "fast",
                    "clock_period_ns": 10.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["n4", "n5"],
                },
                {
                    "path": "longer_path",
                    "clock_domain": "medium",
                    "clock_period_ns": 20.0,
                    "fixed_delay_ns": 5.0,
                    "cut_nets": ["n0", "n1", "n3"],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = (
                Path(temporary_directory)
                / "emuflow_tdm_ratio_optimizer"
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
            unrefined = build_tdm_ratio_plan(
                routes,
                platform,
                executable=str(executable),
                max_iterations=120,
                max_ratio=32,
                post_refinement_iterations=0,
            )
            self.assertEqual(
                unrefined["metrics"]["dp_legalized_domains"], 1
            )
            continuous = [
                hop["continuous_ratio"] for hop in unrefined["hops"]
            ]
            discrete = [
                hop["discrete_ratio"] for hop in unrefined["hops"]
            ]
            displacement_bound = max(
                abs(before - after)
                for before, after in zip(continuous, discrete)
            )
            oracle = exact_discrete_ratio_legalization(
                continuous,
                [hop["direction"] for hop in unrefined["hops"]],
                lanes=unrefined["domains"][0]["lanes"],
                allowed_ratios=[1, 8, 16, 24, 32],
                displacement_bound=displacement_bound,
            )
            self.assertAlmostEqual(
                sum(
                    abs(before - after)
                    for before, after in zip(continuous, discrete)
                ),
                oracle["total_displacement"],
            )
            refined = build_tdm_ratio_plan(
                routes,
                platform,
                executable=str(executable),
                max_iterations=120,
                max_ratio=32,
                post_refinement_iterations=100,
            )
            self.assertEqual(
                refined["metrics"]["post_refinement_swaps"], 1
            )
            self.assertGreater(
                refined["metrics"][
                    "discrete_worst_normalized_slack"
                ],
                unrefined["metrics"][
                    "discrete_worst_normalized_slack"
                ],
            )
            self.assertEqual(
                validate_tdm_ratio_plan(routes, platform, refined)[
                    "status"
                ],
                "pass",
            )
            schedule = build_tdm_schedule(
                routes, platform, refined
            )
            self.assertEqual(
                validate_tdm_schedule(
                    routes, platform, schedule, refined
                )["status"],
                "pass",
            )
            schedule_timing = reconstruct_tdm_schedule_timing(
                routes, platform, schedule
            )
            slot_oracle = exact_single_round_slot_schedule(
                routes, platform, refined
            )
            self.assertAlmostEqual(
                schedule_timing["worst_normalized_slack"],
                slot_oracle["worst_normalized_slack"],
            )
            self.assertEqual(
                schedule["metrics"]["completion_slot"],
                slot_oracle["completion_slot"],
            )

    def test_native_slot_refinement_matches_exact_path_balance(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "path_balance",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=1, latency=1),
                    _link("bc", "b", "c", lanes=1, latency=1),
                ],
            )
        )
        routes = _routes(
            platform,
            [
                *[(f"ab{index}", "a", ["b"]) for index in range(3)],
                *[(f"bc{index}", "b", ["c"]) for index in range(3)],
            ],
            frame_slots=10,
        )
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 40.0,
            },
            "compression": {
                "original_paths": 2,
                "compressed_paths": 2,
            },
            "paths": [
                {
                    "path": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 40.0,
                    "fixed_delay_ns": 2.0,
                    "cut_nets": ["ab2", "bc0"],
                    "cut_transitions": [
                        {"net": "ab2", "from": "a", "to": "b"},
                        {"net": "bc0", "from": "b", "to": "c"},
                    ],
                },
                {
                    "path": "p1",
                    "clock_domain": "clk",
                    "clock_period_ns": 40.0,
                    "fixed_delay_ns": 5.0,
                    "cut_nets": ["ab1", "bc1"],
                    "cut_transitions": [
                        {"net": "ab1", "from": "a", "to": "b"},
                        {"net": "bc1", "from": "b", "to": "c"},
                    ],
                },
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=4,
            ratio_quantum=2,
            post_refinement_iterations=30,
        )
        baseline = build_tdm_schedule(routes, platform, plan)
        baseline_timing = reconstruct_tdm_schedule_timing(
            routes, platform, baseline
        )
        refined = refine_tdm_schedule_native(
            routes,
            platform,
            plan,
            baseline,
            executable=str(tdm_slot_optimizer()),
            max_iterations=20,
        )
        refined_timing = reconstruct_tdm_schedule_timing(
            routes, platform, refined
        )
        oracle = exact_multi_round_slot_schedule(
            routes, platform, plan, max_hops=6
        )
        self.assertEqual(
            validate_tdm_schedule(routes, platform, refined, plan)[
                "status"
            ],
            "pass",
        )
        self.assertGreater(
            refined_timing["worst_normalized_slack"],
            baseline_timing["worst_normalized_slack"],
        )
        self.assertAlmostEqual(
            refined_timing["worst_normalized_slack"],
            oracle["worst_normalized_slack"],
        )
        self.assertEqual(
            refined["metrics"]["completion_slot"],
            oracle["completion_slot"],
        )
        self.assertGreater(
            refined["slot_optimization"]["metrics"]["accepted_moves"],
            0,
        )

    def test_exact_multi_round_slot_oracle_models_global_barrier(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "multi_round_oracle",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [(f"n{index}", "a", ["b"]) for index in range(4)],
            frame_slots=8,
        )
        for route in routes["routes"]:
            route["transport_round"] = (
                0 if route["net"] in {"n0", "n1"} else 1
            )
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 20.0,
            },
            "compression": {
                "original_paths": 4,
                "compressed_paths": 4,
            },
            "paths": [
                {
                    "path": f"path_{index}",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "fixed_delay_ns": float(index),
                    "cut_nets": [f"n{index}"],
                }
                for index in range(4)
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=4,
            ratio_quantum=2,
            post_refinement_iterations=0,
        )
        oracle = exact_multi_round_slot_schedule(
            routes, platform, plan, max_hops=4
        )
        validation = validate_exact_slot_schedule(
            routes, platform, plan, oracle
        )
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(oracle["active_rounds"], [0, 1])
        self.assertEqual(oracle["round_source_ready_slots"][0], 0)
        self.assertEqual(
            oracle["round_source_ready_slots"][1],
            oracle["completion_by_round"][0] + 1,
        )

        schedule = build_tdm_schedule(routes, platform, plan)
        schedule_timing = reconstruct_tdm_schedule_timing(
            routes, platform, schedule
        )
        self.assertGreaterEqual(
            oracle["worst_normalized_slack"],
            schedule_timing["worst_normalized_slack"],
        )
        if oracle["worst_normalized_slack"] == schedule_timing[
            "worst_normalized_slack"
        ]:
            self.assertLessEqual(
                oracle["completion_slot"],
                schedule["metrics"]["completion_slot"],
            )

        with self.assertRaisesRegex(
            ValidationError, "wrapper supports one round"
        ):
            exact_single_round_slot_schedule(routes, platform, plan)
        corrupted = copy.deepcopy(oracle)
        first = min(corrupted["ready_by_hop"])
        corrupted["ready_by_hop"][first] += 1
        with self.assertRaisesRegex(
            ValidationError, "ready_by_hop does not match"
        ):
            validate_exact_slot_schedule(
                routes, platform, plan, corrupted
            )

    def test_capacity_split_estimate_is_not_a_hard_multihop_barrier(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "multihop_barrier",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=1, latency=1),
                    _link("bc", "b", "c", lanes=1, latency=1),
                ],
            )
        )
        routes = _routes(
            platform,
            [
                ("round0_a", "a", ["c"]),
                ("round0_b", "a", ["c"]),
                ("round1", "c", ["a"]),
            ],
            frame_slots=10,
        )
        for route in routes["routes"]:
            route["transport_round"] = (
                1 if route["net"] == "round1" else 0
            )
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 20.0,
            },
            "compression": {
                "original_paths": 3,
                "compressed_paths": 3,
            },
            "paths": [
                {
                    "path": f"path_{net}",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": [net],
                }
                for net in ("round0_a", "round0_b", "round1")
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=4,
            ratio_quantum=2,
            post_refinement_iterations=0,
        )
        plan["round_barrier_legalization"]["source_ready_slot"] = 3
        validate_tdm_ratio_plan(routes, platform, plan)

        schedule = build_tdm_schedule(routes, platform, plan)
        validation = validate_tdm_schedule(
            routes, platform, schedule, plan
        )
        realization = schedule["round_barrier_realization"]
        self.assertEqual(realization["capacity_split_slot"], 3)
        self.assertEqual(realization["source_ready_slot"], 5)
        self.assertEqual(realization["shift_slots"], 2)
        self.assertEqual(validation["status"], "pass")

        oracle = exact_multi_round_slot_schedule(
            routes, platform, plan, max_hops=6
        )
        self.assertEqual(oracle["round_source_ready_slots"][1], 5)
        refined = refine_tdm_schedule_native(
            routes,
            platform,
            plan,
            schedule,
            executable=str(tdm_slot_optimizer()),
            max_iterations=1,
        )
        self.assertEqual(
            validate_tdm_schedule(routes, platform, refined, plan)[
                "status"
            ],
            "pass",
        )

    def test_exact_displacement_dp_scales_beyond_legacy_limit(self) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("a C++17 compiler is required")
        platform = Platform.from_dict(
            _platform_value(
                "large-exact-domain",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [
                (f"n{index:03d}", "a", ["b"])
                for index in range(300)
            ],
            frame_slots=512,
        )
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 100.0,
                "negative_slack_scale_ns": 100.0,
                "max_clock_period_ns": 100.0,
            },
            "compression": {
                "original_paths": 1,
                "compressed_paths": 1,
            },
            "paths": [
                {
                    "path": "critical",
                    "clock_domain": "clk",
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["n299"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = (
                Path(temporary_directory)
                / "emuflow_tdm_ratio_optimizer"
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
                max_ratio=512,
                post_refinement_iterations=0,
            )
        self.assertEqual(plan["metrics"]["dp_legalized_domains"], 1)
        self.assertEqual(plan["metrics"]["greedy_legalized_domains"], 0)
        self.assertEqual(
            validate_tdm_ratio_plan(routes, platform, plan)["status"],
            "pass",
        )

    def test_schedule_validate_simulate_and_write_artifacts(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "two",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=4, latency=2)],
            )
        )
        routes = _routes(
            platform,
            [
                ("n0", "a", ["b"]),
                ("n1", "a", ["b"]),
                ("n2", "b", ["a"]),
            ],
            frame_slots=8,
        )
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        simulation = simulate_tdm_schedule(routes, schedule, frames=9)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(validation["scheduled_bit_hops"], 3)
        self.assertEqual(validation["collisions"], 0)
        self.assertEqual(simulation["delivered_sink_values"], 27)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            routes_path = root / "routes.json"
            platform_path = root / "platform.json"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            report = run_phase5(
                routes_path=routes_path,
                platform_path=platform_path,
                output_dir=root / "phase5",
                simulation_frames=9,
            )
            self.assertEqual(report["status"], "pass")
            for filename in (
                "schedule.json",
                "schedule.tsv",
                "transport_manifest.json",
                "transport_schedule_tb.sv",
                "phase5_report.json",
            ):
                self.assertTrue((root / "phase5" / filename).is_file())
            managed_report = run_phase5(
                routes_path=routes_path,
                platform_path=platform_path,
                output_dir=root / "phase5-managed",
                simulation_frames=9,
                managed_storage=True,
            )
            self.assertEqual(
                set(managed_report["artifacts"]), {"schedule", "report"}
            )
            for filename in (
                "schedule.tsv",
                "transport_manifest.json",
                "transport_schedule_tb.sv",
                "tdm_feedback.json",
            ):
                self.assertFalse((root / "phase5-managed" / filename).exists())

    def test_multihop_precedence_includes_store_and_forward_cycle(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "line",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=2, latency=1),
                    _link("bc", "b", "c", lanes=2, latency=1),
                ],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["c"])],
            frame_slots=8,
        )
        schedule = build_tdm_schedule(routes, platform)
        validate_tdm_schedule(routes, platform, schedule)
        entries = sorted(schedule["entries"], key=lambda entry: entry["hop"])
        self.assertEqual(entries[0]["slot"], 0)
        self.assertEqual(entries[0]["arrival_slot"], 1)
        self.assertEqual(entries[1]["ready_slot"], 2)
        self.assertEqual(entries[1]["slot"], 2)
        self.assertEqual(entries[1]["arrival_slot"], 3)

    def test_register_input_round_waits_for_register_output_round(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "dependency",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        assignment = _assignment(
            platform,
            [("q", "a", ["b"]), ("d", "b", ["a"])],
        )
        assignment["cut_nets"][1]["cut_class"] = "register_input"
        assignment["cut_nets"][1]["transport_round"] = 1
        constraints = normalize_route_constraints(
            None, platform, frame_slots=8
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        entries = {entry["net"]: entry for entry in schedule["entries"]}
        self.assertEqual(entries["q"]["slot"], 0)
        self.assertEqual(entries["q"]["arrival_slot"], 1)
        self.assertEqual(entries["d"]["ready_slot"], 2)
        self.assertEqual(entries["d"]["slot"], 2)
        self.assertEqual(validation["transport_rounds"], 2)
        self.assertEqual(validation["round_barriers"], 1)
        self.assertEqual(validation["max_transport_round"], 1)
        completion = {
            item["net"]: item for item in schedule["demand_completions"]
        }
        self.assertEqual(completion["d"]["source_ready_slot"], 2)
        self.assertEqual(completion["d"]["transport_round"], 1)

    def test_many_transport_rounds_do_not_rescan_prior_rounds(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "many-rounds",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=0)],
            )
        )
        routes = _routes(
            platform,
            [(f"n{index}", "a", ["b"]) for index in range(128)],
            frame_slots=512,
        )
        for index, route in enumerate(routes["routes"]):
            route["transport_round"] = index
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        self.assertEqual(validation["transport_rounds"], 128)
        self.assertEqual(validation["max_transport_round"], 127)
        self.assertEqual(
            sorted(
                (
                    item["transport_round"],
                    item["source_ready_slot"],
                )
                for item in schedule["demand_completions"]
            ),
            [(index, index) for index in range(128)],
        )

    def test_latency_can_make_route_capacity_schedule_infeasible(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "tight",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
            frame_slots=2,
        )
        with self.assertRaisesRegex(
            TDMScheduleInfeasibleError, "infeasible"
        ):
            build_tdm_schedule(routes, platform)

    def test_scheduler_reserves_final_slot_for_runtime_barrier(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "barrier_slot",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"])],
            frame_slots=2,
        )
        with self.assertRaisesRegex(
            TDMScheduleInfeasibleError, "infeasible"
        ):
            build_tdm_schedule(routes, platform)

    def test_half_duplex_opposing_directions_do_not_collide(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "half",
                ["a", "b"],
                [
                    _link(
                        "ab",
                        "a",
                        "b",
                        lanes=1,
                        latency=1,
                        direction="half_duplex",
                    )
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "b", ["a"])],
        )
        constraints = normalize_route_constraints(
            None, platform, frame_slots=4
        )
        demands = [
            {
                "id": f"d{index:06d}",
                "net": cut["net"],
                "source": cut["source_fpgas"][0],
                "sinks": cut["sink_fpgas"],
                "width_bits": 1,
            }
            for index, cut in enumerate(assignment["cut_nets"])
        ]
        routes = {
            "schema": "emuflow.system-routes/v1",
            "design": assignment["design"],
            "platform": platform.name,
            "provider": "half-duplex-scheduler-fixture",
            "constraints": constraints,
            "demands": demands,
            "routes": [
                {
                    **demand,
                    "tree_edges": [
                        {
                            "link": "ab",
                            "from": demand["source"],
                            "to": demand["sinks"][0],
                        }
                    ],
                    "max_latency_cycles": 1,
                }
                for demand in demands
            ],
            "link_utilization": [
                {
                    "key": "ab:shared",
                    "link": "ab",
                    "direction": "shared",
                    "capacity_bits": 4,
                    "used_bits": 2,
                    "utilization": 0.5,
                }
            ],
            "metrics": {
                "demands": 2,
                "routed_sinks": 2,
                "tree_edges": 2,
                "iterations": 0,
                "max_link_utilization": 0.5,
                "total_link_bit_hops": 2,
            },
        }
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        self.assertEqual(validation["collisions"], 0)
        slots = sorted(entry["slot"] for entry in schedule["entries"])
        self.assertEqual(slots, [0, 1])

    def test_collision_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "collision",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
            frame_slots=4,
        )
        schedule = build_tdm_schedule(routes, platform)
        broken = copy.deepcopy(schedule)
        broken["entries"][1]["slot"] = broken["entries"][0]["slot"]
        broken["entries"][1]["lane"] = broken["entries"][0]["lane"]
        with self.assertRaisesRegex(ValidationError, "collision"):
            validate_tdm_schedule(routes, platform, broken)

    def test_precedence_violation_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "precedence",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=2, latency=1),
                    _link("bc", "b", "c", lanes=2, latency=1),
                ],
            )
        )
        routes = _routes(platform, [("n0", "a", ["c"])], frame_slots=8)
        schedule = build_tdm_schedule(routes, platform)
        broken = copy.deepcopy(schedule)
        child = next(entry for entry in broken["entries"] if entry["hop"] == 1)
        child["ready_slot"] -= 1
        child["slot"] -= 1
        child["arrival_slot"] -= 1
        with self.assertRaisesRegex(ValidationError, "ready-slot mismatch"):
            validate_tdm_schedule(routes, platform, broken)

    def test_generated_testbench_contains_real_schedule(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "sv",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=2)],
            )
        )
        routes = _routes(platform, [("n0", "a", ["b"])], frame_slots=8)
        schedule = build_tdm_schedule(routes, platform)
        testbench = schedule_to_systemverilog_testbench(
            routes,
            schedule,
            platform,
            frames=5,
        )
        self.assertIn("emuflow_tdm_link", testbench)
        self.assertIn("EMUFLOW_TDM_RTL_SIM status=pass", testbench)
        self.assertIn("delivery mismatch", testbench)


if __name__ == "__main__":
    unittest.main()
