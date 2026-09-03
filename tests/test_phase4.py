import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.partition import (
    PARTITION_ASSIGNMENT_SCHEMA,
    assign_clusters,
    build_clusters,
    normalize_partition_constraints,
)
from emuflow.phase4 import run_phase4
from emuflow.platform import Platform
from emuflow.routing import (
    demands_from_assignment,
    normalize_route_constraints,
    validate_system_routes,
)
from emuflow.routing_oracle import exact_route_tree_selection
from emuflow.timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    NATIVE_ROUTER_PROVIDER,
    NATIVE_TIMING_EVALUATED_PROVIDER,
    TLR_PROVIDER,
    compress_sta_paths,
    load_sta_paths,
    normalize_sta_paths,
    reconstruct_system_route_timing,
    route_system_native,
    validate_native_system_routes,
)
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


def _platform_value(name, fpga_ids, links):
    return {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "virtual",
            "description": "Phase 4 test topology",
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


def _link(
    link_id,
    left,
    right,
    lanes=1,
    direction="full_duplex",
    latency=1,
):
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
        "design": "route_test",
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


class Phase4Test(unittest.TestCase):
    def test_path_compression_keeps_distinct_required_times(self) -> None:
        normalized = normalize_sta_paths(
            {
                "schema": "emuflow.sta-paths/v1",
                "design": "required_time_compression",
                "paths": [
                    {
                        "id": "p0",
                        "clock_domain": "clk",
                        "clock_period_ns": 10.0,
                        "slack_ns": 1.0,
                        "fixed_delay_ns": 5.0,
                        "cut_nets": ["n0"],
                    },
                    {
                        "id": "p1",
                        "clock_domain": "clk",
                        "clock_period_ns": 10.0,
                        "slack_ns": 1.0,
                        "fixed_delay_ns": 6.0,
                        "cut_nets": ["n0"],
                    },
                ],
            },
            [{"net": "n0", "source": "a", "sinks": ["b"]}],
        )
        compressed = compress_sta_paths(normalized)
        self.assertEqual(compressed["compression"]["compressed_paths"], 2)
        self.assertIn("required-time", compressed["compression"]["lossless_by"])

    def test_timing_oblivious_route_retains_independent_timing_records(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "evaluated_baseline",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=4)],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["b"])])
        timing = {
            "schema": "emuflow.sta-paths/v1",
            "design": "route_test",
            "paths": [
                {
                    "id": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 9.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["n0"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            timing_path.write_text(json.dumps(timing), encoding="utf-8")
            report = run_phase4(
                assignment_path,
                platform_path,
                root / "phase4",
                timing_paths_path=timing_path,
                provider=NATIVE_TIMING_EVALUATED_PROVIDER,
                router=str(tlr_router()),
            )
            routes = json.loads(
                (root / "phase4/routes.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                routes["provider"], NATIVE_TIMING_EVALUATED_PROVIDER
            )
            self.assertEqual(
                routes["timing_evaluation"],
                {
                    "mode": "post-route-evaluation-only",
                    "optimization_enabled": False,
                    "source_provider": NATIVE_ROUTER_PROVIDER,
                },
            )
            self.assertEqual(len(routes["timing"]["paths"]), 1)
            self.assertEqual(report["validation"]["timing_paths_original"], 1)
            self.assertEqual(
                report["candidate_generation"],
                {
                    "requested_workers": 1,
                    "ordering": "demand-index-then-generator-index",
                    "route_artifact_deterministic": True,
                },
            )
            normalized = load_sta_paths(timing_path, routes["demands"])
            self.assertEqual(
                validate_native_system_routes(
                    assignment, platform, routes, normalized
                )["status"],
                "pass",
            )
            tampered = copy.deepcopy(routes)
            tampered["timing_evaluation"]["optimization_enabled"] = True
            with self.assertRaisesRegex(
                ValidationError, "evaluation metadata"
            ):
                validate_native_system_routes(
                    assignment, platform, tampered, normalized
                )

    def test_cpp_router_enforces_source_to_sink_hop_limit(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "hop_bounded_routes",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("bc", "b", "c", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                    _link("ad", "a", "d", lanes=4),
                ],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["d"])])
        base = {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 8,
            "link_delay_ns": {
                "ab": 1.0,
                "bc": 1.0,
                "cd": 1.0,
                "ad": 10.0,
            },
        }
        unconstrained = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(base, platform),
            executable=str(tlr_router()),
        )
        self.assertEqual(len(unconstrained["routes"][0]["tree_edges"]), 3)

        bounded = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(
                {**base, "max_route_hops": 2}, platform
            ),
            executable=str(tlr_router()),
        )
        self.assertEqual(
            [edge["link"] for edge in bounded["routes"][0]["tree_edges"]],
            ["ad"],
        )
        self.assertEqual(bounded["metrics"]["max_route_hops_observed"], 1)
        self.assertEqual(
            validate_system_routes(assignment, platform, bounded)["status"],
            "pass",
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "n0_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["n0"],
                        }
                    ],
                },
                bounded["demands"],
            )
        )
        oracle = exact_route_tree_selection(
            assignment, platform, bounded["constraints"], timing
        )
        self.assertEqual(oracle["trees"]["n0"], [["ad", "a", "d"]])

        forged = copy.deepcopy(unconstrained)
        forged["constraints"] = normalize_route_constraints(
            {**base, "max_route_hops": 2}, platform
        )
        forged["metrics"]["max_route_hops_observed"] = 3
        with self.assertRaisesRegex(ValidationError, "above maximum"):
            validate_system_routes(assignment, platform, forged)

        line_platform = Platform.from_dict(
            _platform_value(
                "hop_bounded_infeasible",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("bc", "b", "c", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                ],
            )
        )
        with self.assertRaisesRegex(EmuFlowError, "infeasible"):
            route_system_native(
                _assignment(line_platform, [("n0", "a", ["d"])]),
                line_platform,
                normalize_route_constraints(
                    {
                        "schema": "emuflow.system-route-constraints/v1",
                        "max_route_hops": 2,
                    },
                    line_platform,
                ),
                executable=str(tlr_router()),
            )

    def test_route_constraint_rejects_invalid_hop_limit(self) -> None:
        platform = Platform.from_dict(
            _platform_value("hop_limit", ["a", "b"], [_link("ab", "a", "b")])
        )
        for invalid in (0, -1, True, 1.5, "2"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValidationError, "max_route_hops"
            ):
                normalize_route_constraints(
                    {
                        "schema": "emuflow.system-route-constraints/v1",
                        "max_route_hops": invalid,
                    },
                    platform,
                )

    def test_cpp_router_preserves_direction_exact_link_delays(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "asymmetric_diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b"),
                    _link("bd", "b", "d"),
                    _link("ac", "a", "c"),
                    _link("cd", "c", "d"),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("forward", "a", ["d"]), ("reverse", "d", ["a"])],
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": net,
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": [net],
                        }
                        for net in ("forward", "reverse")
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 32,
                "directed_link_delay_ns": {
                    "ab": {"a": {"b": 1.0}, "b": {"a": 5.0}},
                    "bd": {"b": {"d": 1.0}, "d": {"b": 5.0}},
                    "ac": {"a": {"c": 5.0}, "c": {"a": 1.0}},
                    "cd": {"c": {"d": 5.0}, "d": {"c": 1.0}},
                },
            },
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider="timing-aware-route-tdm-cooptimized-v1",
        )
        by_net = {route["net"]: route for route in routes["routes"]}
        self.assertEqual(
            {edge["link"] for edge in by_net["forward"]["tree_edges"]},
            {"ab", "bd"},
        )
        self.assertEqual(
            {edge["link"] for edge in by_net["reverse"]["tree_edges"]},
            {"ac", "cd"},
        )
        self.assertEqual(by_net["forward"]["predicted_max_delay_ns"], 2.0)
        self.assertEqual(by_net["reverse"]["predicted_max_delay_ns"], 2.0)

    def test_tree_edge_sum_tdm_counts_each_multicast_edge_once(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "tree_edge_sum",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b"),
                    _link("bc", "b", "c"),
                    _link("bd", "b", "d"),
                ],
            )
        )
        assignment = _assignment(
            platform, [("multicast", "a", ["c", "d"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "multicast_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 10.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["multicast"],
                        }
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        base_value = {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 8,
            "reroute_rounds": 0,
            "link_delay_ns": {"ab": 1.0, "bc": 1.0, "bd": 1.0},
        }
        longest_sink = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(base_value, platform),
            timing,
            executable=str(tlr_router()),
            provider="timing-aware-route-tdm-cooptimized-v1",
        )
        edge_sum_value = {**base_value, "tree_edge_sum_tdm": True}
        edge_sum = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(edge_sum_value, platform),
            timing,
            executable=str(tlr_router()),
            provider="timing-aware-route-tdm-cooptimized-v1",
        )

        self.assertEqual(len(edge_sum["routes"][0]["tree_edges"]), 3)
        self.assertAlmostEqual(
            longest_sink["metrics"]["estimated_worst_tdm_slack_ns"],
            8.0,
        )
        self.assertAlmostEqual(
            edge_sum["metrics"]["estimated_worst_tdm_slack_ns"],
            7.0,
        )
        checked = validate_native_system_routes(
            assignment, platform, edge_sum, timing
        )
        self.assertEqual(checked["status"], "pass")
        self.assertAlmostEqual(
            checked["estimated_worst_tdm_slack_ns"], 7.0
        )

    def test_router_preserves_member_specific_multicast_sink(self) -> None:
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
        assignment = _assignment(
            platform, [("multicast", "a", ["b", "c"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "to-c",
                            "clock_domain": "clk",
                            "clock_period_ns": 10.0,
                            "slack_ns": 1.0,
                            "fixed_delay_ns": 9.0,
                            "cut_nets": ["multicast"],
                            "cut_transitions": [
                                {
                                    "net": "multicast",
                                    "from": "a",
                                    "to": "c",
                                }
                            ],
                        }
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(None, platform)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider="timing-aware-route-tdm-cooptimized-v1",
        )
        self.assertEqual(
            routes["timing"]["paths"][0]["cut_transitions"],
            [{"net": "multicast", "from": "a", "to": "c"}],
        )
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes, timing
            )["status"],
            "pass",
        )

    def test_sll_capacity_is_not_multiplied_by_frame_slots(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "sll_capacity",
                ["a", "b"],
                [_link("sll", "a", "b", lanes=1)],
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "sll_links": ["sll"],
            },
            platform,
        )
        legal_assignment = _assignment(
            platform, [("n0", "a", ["b"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "sll_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["n0"],
                        }
                    ],
                },
                demands_from_assignment(legal_assignment, platform),
            )
        )
        routes = route_system_native(
            legal_assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider="timing-aware-route-tdm-cooptimized-v1",
        )
        self.assertEqual(
            routes["metrics"]["estimated_max_tdm_ratio"], 1
        )
        self.assertEqual(
            validate_native_system_routes(
                legal_assignment, platform, routes, timing
            )["status"],
            "pass",
        )

        overfull_assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
        )
        with self.assertRaisesRegex(
            EmuFlowError, "routing infeasible after capacity iterations"
        ):
            route_system_native(
                overfull_assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider="timing-aware-route-tdm-cooptimized-v1",
            )

    def test_native_router_accepts_capacity_above_signed_32_bit(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "wide_tdm_capacity",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=5000)],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["b"])])
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 837876,
            },
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
            provider=NATIVE_ROUTER_PROVIDER,
        )
        self.assertEqual(routes["metrics"]["total_link_bit_hops"], 1)
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes
            )["status"],
            "pass",
        )

    def test_hybrid_router_selects_delay_demand_balanced_multicast(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "balanced_multicast",
                ["a", "b", "c", "d", "e"],
                [
                    _link("ab", "a", "b"),
                    _link("bd", "b", "d"),
                    _link("be", "b", "e"),
                    _link("ac", "a", "c"),
                    _link("ce", "c", "e"),
                ],
            )
        )
        assignment = _assignment(
            platform, [("multicast", "a", ["d", "e"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "multicast_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["multicast"],
                        }
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "reroute_rounds": 0,
                "link_delay_ns": {
                    "ab": 1.0,
                    "bd": 1.0,
                    "be": 1.0,
                    "ac": 0.995,
                    "ce": 0.995,
                },
            },
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider="timing-aware-route-tdm-cooptimized-v1",
        )
        baseline = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=TLR_PROVIDER,
        )
        self.assertEqual(len(baseline["routes"][0]["tree_edges"]), 4)
        self.assertNotIn("joint_optimization", baseline)
        self.assertEqual(
            routes["joint_optimization"]["candidate_generation"][
                "selected"
            ],
            "delay-demand-balanced",
        )
        self.assertEqual(len(routes["routes"][0]["tree_edges"]), 3)
        oracle = exact_route_tree_selection(
            assignment, platform, constraints, timing
        )
        self.assertEqual(
            {
                (edge["link"], edge["from"], edge["to"])
                for edge in routes["routes"][0]["tree_edges"]
            },
            {
                tuple(edge)
                for edge in oracle["trees"]["multicast"]
            },
        )
        self.assertGreater(oracle["enumerated_combinations"], 1)
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes, timing
            )["status"],
            "pass",
        )

    def test_common_timing_checker_keeps_multiclock_extrema_separate(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "multiclock",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        assignment = _assignment(
            platform,
            [("long_period", "a", ["b"]), ("short_period", "a", ["b"])],
        )
        routes = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 64,
                },
                platform,
            ),
            executable=str(tlr_router()),
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "raw_slack_worst",
                            "clock_domain": "slow",
                            "clock_period_ns": 100.0,
                            "slack_ns": -2.0,
                            "fixed_delay_ns": 110.0,
                            "cut_nets": ["long_period"],
                        },
                        {
                            "id": "normalized_worst",
                            "clock_domain": "fast",
                            "clock_period_ns": 10.0,
                            "slack_ns": -1.0,
                            "fixed_delay_ns": 10.0,
                            "cut_nets": ["short_period"],
                        },
                    ],
                },
                routes["demands"],
            )
        )
        checked = reconstruct_system_route_timing(
            assignment, platform, routes, timing
        )
        self.assertEqual(
            checked["worst_slack_path"], "raw_slack_worst"
        )
        self.assertEqual(
            checked["worst_normalized_path"], "normalized_worst"
        )
        self.assertEqual(
            checked["estimated_worst_tdm_slack_path"],
            "raw_slack_worst",
        )
        self.assertEqual(
            checked["estimated_worst_tdm_normalized_path"],
            "normalized_worst",
        )

    def test_timing_aware_cpp_router_prioritizes_critical_clock_domain(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "timing_diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", latency=1),
                    _link("bd", "b", "d", latency=1),
                    _link("ac", "a", "c", latency=5),
                    _link("cd", "c", "d", latency=5),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [
                ("a_low_priority", "a", ["d"]),
                ("z_critical", "a", ["d"]),
            ],
        )
        timing_value = {
            "schema": "emuflow.sta-paths/v1",
            "design": "route_test",
            "paths": [
                {
                    "id": "critical_0",
                    "clock_domain": "fast_clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": -1.0,
                    "fixed_delay_ns": 10.0,
                    "cut_nets": ["z_critical"],
                    "cut_signature": ["a->d"],
                },
                {
                    "id": "critical_duplicate",
                    "clock_domain": "fast_clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 0.0,
                    "fixed_delay_ns": 9.0,
                    "cut_nets": ["z_critical"],
                    "cut_signature": ["a->d"],
                },
                {
                    "id": "relaxed_0",
                    "clock_domain": "slow_clk",
                    "clock_period_ns": 100.0,
                    "slack_ns": 80.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["a_low_priority"],
                    "cut_signature": ["slow:a->d"],
                },
            ],
        }
        constraints_value = {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 1,
            "max_iterations": 12,
            "reroute_rounds": 4,
            "link_delay_ns": {
                "ab": 1.0,
                "bd": 1.0,
                "ac": 5.0,
                "cd": 5.0,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = tlr_router()
            assignment_path = root / "assignment.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            constraints_path = root / "constraints.json"
            assignment_path.write_text(
                json.dumps(assignment), encoding="utf-8"
            )
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            timing_path.write_text(
                json.dumps(timing_value), encoding="utf-8"
            )
            constraints_path.write_text(
                json.dumps(constraints_value), encoding="utf-8"
            )
            report = run_phase4(
                assignment_path=assignment_path,
                platform_path=platform_path,
                output_dir=root / "phase4",
                constraints_path=constraints_path,
                timing_paths_path=timing_path,
                router=str(executable),
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["validation"]["timing_paths_original"], 3)
            self.assertEqual(report["validation"]["timing_paths_compressed"], 2)
            routes = json.loads(
                (root / "phase4" / "routes.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                routes["provider"],
                GLOBAL_CANDIDATE_PROVIDER,
            )
            self.assertIn("candidate_generation", report)
            self.assertEqual(
                routes["joint_optimization"]["method"],
                "checked-candidate-pool+"
                "deterministic-batch-conflict-lns-v2",
            )
            route_by_net = {
                route["net"]: route for route in routes["routes"]
            }
            self.assertEqual(
                {
                    edge["link"]
                    for edge in route_by_net["z_critical"]["tree_edges"]
                },
                {"ab", "bd"},
            )
            self.assertEqual(
                route_by_net["z_critical"]["predicted_max_delay_ns"], 2.0
            )
            self.assertEqual(
                {
                    edge["link"]
                    for edge in route_by_net["a_low_priority"]["tree_edges"]
                },
                {"ac", "cd"},
            )
            normalized = load_sta_paths(
                timing_path,
                routes["demands"],
            )
            checked = validate_native_system_routes(
                assignment, platform, routes, normalized
            )
            self.assertEqual(checked["status"], "pass")
            normalized_reload = load_sta_paths(
                root / "phase4" / "timing_paths.normalized.json",
                routes["demands"],
            )
            self.assertEqual(normalized_reload, normalized)
            corrupted = copy.deepcopy(routes)
            corrupted["routes"][0]["predicted_max_delay_ns"] += 0.25
            with self.assertRaisesRegex(
                ValidationError, "independent edge-delay recomputation"
            ):
                validate_native_system_routes(
                    assignment, platform, corrupted, normalized
                )
            corrupted = copy.deepcopy(routes)
            corrupted["metrics"]["estimated_max_tdm_ratio"] += 1
            with self.assertRaisesRegex(
                ValidationError, "route/TDM proxy recomputation"
            ):
                validate_native_system_routes(
                    assignment, platform, corrupted, normalized
                )

            lock_platform = Platform.from_dict(
                _platform_value(
                    "direction_lock",
                    ["a", "b", "c"],
                    [
                        _link(
                            "ab",
                            "a",
                            "b",
                            lanes=4,
                            direction="half_duplex",
                        ),
                        _link("ac", "a", "c", lanes=4, latency=2),
                        _link("bc", "b", "c", lanes=4, latency=2),
                    ],
                )
            )
            lock_assignment = _assignment(
                lock_platform,
                [
                    ("forward_0", "a", ["b"]),
                    ("forward_1", "a", ["b"]),
                    ("reverse_0", "b", ["a"]),
                ],
            )
            lock_timing = {
                "schema": "emuflow.sta-paths/v1",
                "design": "route_test",
                "paths": [
                    {
                        "id": f"path_{net}",
                        "clock_domain": "clk",
                        "clock_period_ns": 50.0,
                        "slack_ns": 20.0,
                        "fixed_delay_ns": 0.0,
                        "cut_nets": [net],
                        "cut_signature": [signature],
                    }
                    for net, signature in (
                        ("forward_0", "a->b:0"),
                        ("forward_1", "a->b:1"),
                        ("reverse_0", "b->a"),
                    )
                ],
            }
            lock_assignment_path = root / "lock-assignment.json"
            lock_platform_path = root / "lock-platform.json"
            lock_timing_path = root / "lock-timing.json"
            lock_assignment_path.write_text(
                json.dumps(lock_assignment), encoding="utf-8"
            )
            lock_platform_path.write_text(
                json.dumps(lock_platform.to_dict()), encoding="utf-8"
            )
            lock_timing_path.write_text(
                json.dumps(lock_timing), encoding="utf-8"
            )
            lock_report = run_phase4(
                assignment_path=lock_assignment_path,
                platform_path=lock_platform_path,
                output_dir=root / "lock-phase4",
                frame_slots=1,
                provider="timing-aware-load-balanced-v1",
                timing_paths_path=lock_timing_path,
                router=str(executable),
            )
            self.assertEqual(lock_report["validation"]["direction_locks"], 1)
            lock_routes = json.loads(
                (root / "lock-phase4" / "routes.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                lock_routes["provider"], "timing-aware-load-balanced-v1"
            )
            self.assertEqual(lock_routes["constraints"]["lambda_tdm"], 0.0)
            self.assertNotIn("joint_optimization", lock_routes)
            self.assertEqual(
                lock_routes["direction_locks"][0]["from"], "a"
            )
            reverse_route = next(
                route
                for route in lock_routes["routes"]
                if route["net"] == "reverse_0"
            )
            self.assertNotIn(
                "ab", {edge["link"] for edge in reverse_route["tree_edges"]}
            )

    def test_pipeline_routes_real_counter_partition(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        platform = Platform.load(PLATFORM_PATH)
        constraints = normalize_partition_constraints(None, ir, platform)
        clusters = build_clusters(ir, constraints)
        assignment = assign_clusters(
            ir,
            platform,
            clusters,
            constraints,
            seed=4,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.json"
            assignment_path.write_text(
                json.dumps(assignment), encoding="utf-8"
            )
            report = run_phase4(
                assignment_path=assignment_path,
                platform_path=PLATFORM_PATH,
                output_dir=root / "phase4",
                frame_slots=1,
                router=str(tlr_router()),
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["provider"], NATIVE_ROUTER_PROVIDER)
            self.assertGreater(report["validation"]["demands"], 0)
            self.assertGreater(report["validation"]["routed_sinks"], 0)
            self.assertEqual(report["validation"]["overloaded_links"], 0)
            for filename in (
                "route_constraints.normalized.json",
                "route_candidate_pool.json",
                "routes.json",
                "phase4_report.json",
            ):
                self.assertTrue((root / "phase4" / filename).is_file())
            self.assertEqual(
                report["artifacts"]["candidate_pool"],
                "route_candidate_pool.json",
            )
            managed_report = run_phase4(
                assignment_path=assignment_path,
                platform_path=PLATFORM_PATH,
                output_dir=root / "phase4-managed",
                frame_slots=1,
                router=str(tlr_router()),
                managed_storage=True,
            )
            self.assertNotIn("candidate_pool", managed_report["artifacts"])
            self.assertFalse(
                (root / "phase4-managed/route_candidate_pool.json").exists()
            )

    def test_native_router_uses_both_diamond_paths(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b"),
                    _link("bd", "b", "d"),
                    _link("ac", "a", "c"),
                    _link("cd", "c", "d"),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["d"]), ("n1", "a", ["d"])],
        )
        constraints = normalize_route_constraints(
            {"schema": "emuflow.system-route-constraints/v1", "frame_slots": 1},
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        validation = validate_native_system_routes(
            assignment, platform, routes
        )
        self.assertEqual(validation["status"], "pass")
        used_links = {
            edge["link"]
            for route in routes["routes"]
            for edge in route["tree_edges"]
        }
        self.assertEqual(used_links, {"ab", "bd", "ac", "cd"})
        self.assertEqual(validation["max_link_utilization"], 1.0)

    def test_multicast_is_a_reachable_acyclic_tree(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "multicast",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("ac", "a", "c", lanes=4),
                    _link("bd", "b", "d", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("multicast_net", "a", ["b", "c", "d"])],
        )
        constraints = normalize_route_constraints(None, platform, frame_slots=1)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        validation = validate_native_system_routes(
            assignment, platform, routes
        )
        self.assertEqual(validation["routed_sinks"], 3)
        self.assertEqual(validation["demands"], 1)
        self.assertLessEqual(validation["tree_edges"], 3)

    def test_unavailable_links_report_unreachable_sink(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "line",
                ["a", "b", "c"],
                [_link("ab", "a", "b"), _link("bc", "b", "c")],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["c"])])
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "unavailable_links": ["bc"],
            },
            platform,
        )
        with self.assertRaisesRegex(
            (EmuFlowError, ValidationError), "unreachable|cannot reach"
        ):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_infeasible_link_capacity_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "single",
                ["a", "b"],
                [_link("ab", "a", "b")],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 1,
                "max_iterations": 2,
            },
            platform,
        )
        with self.assertRaisesRegex(EmuFlowError, "infeasible"):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_half_duplex_capacity_is_shared(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "half",
                ["a", "b"],
                [_link("ab", "a", "b", direction="half_duplex")],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "b", ["a"])],
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 1,
                "max_iterations": 2,
            },
            platform,
        )
        with self.assertRaisesRegex(EmuFlowError, "unreachable|infeasible"):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_cycle_in_route_artifact_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "cycle",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=4)],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["b"])])
        constraints = normalize_route_constraints(None, platform, frame_slots=1)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        broken = copy.deepcopy(routes)
        broken["routes"][0]["tree_edges"].append(
            {"link": "ab", "from": "b", "to": "a"}
        )
        with self.assertRaisesRegex(ValidationError, "cycle"):
            validate_system_routes(assignment, platform, broken)


if __name__ == "__main__":
    unittest.main()
