from tests.native_route_fixture import route_system_native
import copy
import re
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.routing import demands_from_assignment, normalize_route_constraints
from emuflow.routing_batches import (
    build_compact_route_refinement_batches,
    build_route_refinement_batches,
)
from tests.routing_reference import exact_route_tree_selection
from emuflow.timing_routing import (GLOBAL_CANDIDATE_PROVIDER, compress_sta_paths, normalize_sta_paths, validate_native_system_routes)
from tests.native_build import tlr_router
from tests.test_phase4 import _assignment, _link, _platform_value


class RouteCandidatePoolTest(unittest.TestCase):
    def test_adaptive_hop_does_not_clone_the_complete_input(self) -> None:
        source = (
            Path(__file__).parents[1] / "src" / "native" / "tlr_router.cpp"
        ).read_text(encoding="utf-8")
        helper = re.search(
            r"Route shortest_path_tree_with_limit\([^}]+\n  \}",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(helper)
        body = helper.group(0)
        self.assertNotIn("Input limited_model", body)
        self.assertIn("hop_limit", body)

    def _fixture(self):
        platform = Platform.from_dict(
            _platform_value(
                "candidate_pool",
                ["a", "b", "c", "d", "e"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("bd", "b", "d", lanes=4),
                    _link("be", "b", "e", lanes=4),
                    _link("ac", "a", "c", lanes=4),
                    _link("ce", "c", "e", lanes=4),
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
        return platform, assignment, timing, constraints


    def test_complete_stage_is_deterministic_and_matches_exact_small_tree(self):
        platform, assignment, timing, constraints = self._fixture()
        first = route_system_native(assignment, platform, constraints, timing)
        second = route_system_native(assignment, platform, constraints, timing)
        self.assertEqual(first, second)
        oracle = exact_route_tree_selection(assignment, platform, constraints, timing)
        actual = {(edge['link'],edge['from'],edge['to']) for edge in first['routes'][0]['tree_edges']}
        self.assertEqual(actual, {tuple(edge) for edge in oracle['trees']['multicast']})
        self.assertEqual(validate_native_system_routes(assignment, platform, first, timing)['status'], 'pass')

    def test_independent_validator_rejects_tampered_delay_and_tree(self):
        platform, assignment, timing, constraints = self._fixture()
        routes = route_system_native(assignment, platform, constraints, timing)
        broken = copy.deepcopy(routes)
        broken['routes'][0]['predicted_max_delay_ns'] += 1
        with self.assertRaisesRegex(ValidationError, 'delay'):
            validate_native_system_routes(assignment, platform, broken, timing)
        broken = copy.deepcopy(routes)
        broken['routes'][0]['tree_edges'].append({'link':'ab','from':'b','to':'a'})
        with self.assertRaisesRegex(ValidationError, 'cycle|multiple path'):
            validate_native_system_routes(assignment, platform, broken, timing)


    def test_global_candidate_stage_matches_exact_small_tree_objective(self):
        platform, assignment, timing, constraints = self._fixture()
        routes = route_system_native(assignment, platform, constraints, timing)
        oracle = exact_route_tree_selection(assignment, platform, constraints, timing)
        self.assertEqual(routes['metrics']['worst_normalized_slack'], oracle['metrics']['worst_normalized_slack'])
        self.assertEqual(routes['metrics']['estimated_worst_tdm_normalized_slack'], oracle['metrics']['worst_tdm_normalized_slack'])
        self.assertTrue(routes['metrics']['master_exact'])
        self.assertEqual(routes['metrics']['master_rounds'], 1)
        self.assertEqual(validate_native_system_routes(assignment, platform, routes, timing)['status'], 'pass')

    def test_parallel_candidate_and_reroute_batches_are_byte_stable(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "parallel_batches",
                ["a", "b", "c", "d", "e", "f"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("ac", "a", "c", lanes=4),
                    _link("bc", "b", "c", lanes=4),
                    _link("de", "d", "e", lanes=4),
                    _link("df", "d", "f", lanes=4),
                    _link("ef", "e", "f", lanes=4),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("left", "a", ["c"]), ("right", "d", ["f"])],
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "left_path",
                            "clock_domain": "clk0",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["left"],
                        },
                        {
                            "id": "right_path",
                            "clock_domain": "clk1",
                            "clock_period_ns": 20.0,
                            "slack_ns": 11.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["right"],
                        },
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "reroute_rounds": 4,
                "link_delay_ns": {
                    link.id: 1.0 for link in platform.links
                },
            },
            platform,
        )
        with tempfile.TemporaryDirectory() as temporary:
            routes = []
            for workers in (1, 2, 8):
                routes.append(
                    route_system_native(
                        assignment,
                        platform,
                        constraints,
                        timing,
                        executable=str(tlr_router()),
                        provider=GLOBAL_CANDIDATE_PROVIDER,
                        candidate_workers=workers,
                    )
                )
            self.assertEqual(routes[0], routes[1])
            self.assertEqual(routes[0], routes[2])
            batching = routes[0]["joint_optimization"][
                "refinement_batches"
            ]
            self.assertEqual(batching["batch_count"], 1)
            self.assertEqual(batching["maximum_parallel_batch"], 2)
            self.assertEqual(batching["batches"], [[0, 1]])

    def test_large_conflict_batch_certificate_uses_linear_resource_cursor(
        self,
    ) -> None:
        path_count = 10_000
        platform = Platform.from_dict(
            _platform_value(
                "large_batches",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=path_count)],
            )
        )
        assignment = _assignment(
            platform,
            [(f"n{index}", "a", ["b"]) for index in range(path_count)],
        )
        demands = demands_from_assignment(assignment, platform)
        candidate_pool = {
            "constraints": normalize_route_constraints(
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                },
                platform,
            ),
            "candidates": [
                {
                    "generator": "shortest-path-tree",
                    "demand_id": demand["id"],
                    "tree_edges": [
                        {"link": "ab", "from": "a", "to": "b"}
                    ],
                }
                for demand in demands
            ],
        }
        timing_paths = {
            "normalization": {
                "positive_slack_scale_ns": 1.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 1.0,
            },
            "paths": [
                {
                    "id": f"p{index}",
                    "clock_period_ns": 1.0,
                    "slack_ns": 1.0,
                    "normalized_slack": 1.0,
                    "cut_nets": [f"n{index}"],
                }
                for index in range(path_count)
            ],
        }
        certificate = build_route_refinement_batches(
            assignment, platform, candidate_pool, timing_paths
        )
        compact_certificate = build_compact_route_refinement_batches(
            [
                {
                    "clock_period_ns": 1.0,
                    "baseline_slack_ns": 1.0,
                    "demands": [index],
                }
                for index in range(path_count)
            ],
            timing_paths["normalization"],
            [1] * path_count,
        )
        self.assertEqual(compact_certificate, certificate)
        self.assertEqual(certificate["batch_count"], path_count)
        self.assertEqual(certificate["maximum_parallel_batch"], 1)
        self.assertEqual(certificate["batches"][0], [0])
        self.assertEqual(certificate["batches"][-1], [path_count - 1])

    def test_global_master_mixes_generators_across_demands(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "mixed_master",
                ["a", "b", "c", "d", "e"],
                [
                    _link("bc", "b", "c", lanes=4),
                    _link("ae", "a", "e", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                    _link("ad", "a", "d", lanes=4),
                    _link("ac", "a", "c", lanes=4),
                    _link("bd", "b", "d", lanes=4),
                    _link("de", "d", "e", lanes=4),
                    _link("ce", "c", "e", lanes=4),
                    _link("ab", "a", "b", lanes=4),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [
                ("n0", "a", ["c"]),
                ("n1", "b", ["e"]),
                ("n2", "e", ["d", "c"]),
            ],
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": f"p{index}",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": float(index),
                            "fixed_delay_ns": fixed,
                            "cut_nets": [f"n{index}"],
                        }
                        for index, fixed in enumerate((5.0, 10.0, 5.0))
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
                    "bc": 1.2,
                    "ae": 1.2,
                    "cd": 1.5,
                    "ad": 2.0,
                    "ac": 0.9,
                    "bd": 1.5,
                    "de": 1.0,
                    "ce": 3.0,
                    "ab": 1.2,
                },
            },
            platform,
        )
        with tempfile.TemporaryDirectory() as temporary:
            routes = route_system_native(
                assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider=GLOBAL_CANDIDATE_PROVIDER,
            )

            selection = routes["joint_optimization"][
                "candidate_generation"
            ]["master_selection"]
            self.assertEqual(
                [item["generator"] for item in selection],
                [
                    "shortest-path-tree",
                    "shortest-path-tree",
                    "nearest-terminal-steiner",
                ],
            )
            self.assertEqual(routes["metrics"]["master_switches"], 1)

            tampered = copy.deepcopy(routes)
            tampered["joint_optimization"]["candidate_generation"][
                "master_selection"
            ][0]["generator"] = "refined-final"
            with self.assertRaisesRegex(ValidationError, "master selection"):
                validate_native_system_routes(
                    assignment, platform, tampered, timing
                )

    def test_large_master_can_start_from_feasible_advanced_column(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "advanced_initial_column",
                ["a", "b", "c", "d", "e", "f"],
                [
                    _link("ab", "a", "b", lanes=1),
                    _link("af", "a", "f", lanes=1),
                    _link("bc", "b", "c", lanes=1),
                    _link("be", "b", "e", lanes=1),
                    _link("cd", "c", "d", lanes=1),
                    _link("de", "d", "e", lanes=1),
                    _link("df", "d", "f", lanes=1),
                    _link("ef", "e", "f", lanes=1),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [
                ("n0", "e", ["c", "f"]),
                ("n1", "f", ["a", "b", "d"]),
                ("n2", "c", ["d"]),
                ("n3", "f", ["d"]),
                ("n4", "c", ["d", "f", "b"]),
                ("n5", "b", ["c", "a"]),
                ("n6", "e", ["d", "c"]),
                ("n7", "e", ["b"]),
                ("n8", "a", ["d", "e", "f"]),
            ],
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": f"p{index}",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": float(index % 4),
                            "fixed_delay_ns": float(index % 3 + 1),
                            "cut_nets": [f"n{index}"],
                        }
                        for index in range(9)
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 2,
                "reroute_rounds": 0,
                "link_delay_ns": {
                    "ab": 1.0,
                    "af": 2.5,
                    "bc": 3.0,
                    "be": 2.5,
                    "cd": 1.0,
                    "de": 1.5,
                    "df": 2.0,
                    "ef": 0.8,
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
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        selection = routes["joint_optimization"]["candidate_generation"][
            "master_selection"
        ]
        self.assertFalse(routes["metrics"]["master_exact"])
        self.assertEqual(routes["metrics"]["master_switches"], 0)
        self.assertEqual(
            {record["generator"] for record in selection},
            {"shallow-light-tree"},
        )
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes, timing
            )["status"],
            "pass",
        )


if __name__ == "__main__":
    unittest.main()
