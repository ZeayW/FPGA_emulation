import copy
import re
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.routing import demands_from_assignment, normalize_route_constraints
from emuflow.routing_candidates import (
    exact_route_candidate_selection,
    validate_route_candidate_pool,
)
from emuflow.routing_batches import build_route_refinement_batches
from emuflow.routing_oracle import exact_route_tree_selection
from emuflow.timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    ROUTE_TDM_PROVIDER,
    compress_sta_paths,
    normalize_sta_paths,
    route_system_native,
    validate_native_system_routes,
)
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

    def test_pool_is_deterministic_and_contains_exact_small_tree(self) -> None:
        platform, assignment, timing, constraints = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / "first.json", root / "second.json"]
            routes = []
            for path in paths:
                routes.append(
                    route_system_native(
                        assignment,
                        platform,
                        constraints,
                        timing,
                        executable=str(tlr_router()),
                        provider=ROUTE_TDM_PROVIDER,
                        candidate_pool_path=path,
                    )
                )
            self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
            self.assertEqual(routes[0], routes[1])
            import json

            pool = json.loads(paths[0].read_text(encoding="utf-8"))
            checked = validate_route_candidate_pool(
                assignment, platform, pool
            )
            self.assertEqual(checked["status"], "pass")
            self.assertEqual(
                set(pool["metrics"]["candidates_by_generator"]),
                {
                    "shortest-path-tree",
                    "delay-demand-balanced",
                    "nearest-terminal-steiner",
                    "refined-final",
                },
            )
            oracle = exact_route_tree_selection(
                assignment, platform, constraints, timing
            )
            optimum = {tuple(edge) for edge in oracle["trees"]["multicast"]}
            generated = [
                {
                    (edge["link"], edge["from"], edge["to"])
                    for edge in candidate["tree_edges"]
                }
                for candidate in pool["candidates"]
            ]
            self.assertIn(optimum, generated)

    def test_pool_rejects_tampered_delay_and_tree(self) -> None:
        platform, assignment, timing, constraints = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pool.json"
            route_system_native(
                assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider=ROUTE_TDM_PROVIDER,
                candidate_pool_path=path,
            )
            import json

            pool = json.loads(path.read_text(encoding="utf-8"))
            broken_delay = copy.deepcopy(pool)
            broken_delay["candidates"][0]["predicted_max_delay_ns"] += 1.0
            with self.assertRaisesRegex(ValidationError, "delay"):
                validate_route_candidate_pool(
                    assignment, platform, broken_delay
                )
            broken_tree = copy.deepcopy(pool)
            broken_tree["candidates"][0]["tree_edges"].append(
                {"link": "ab", "from": "b", "to": "a"}
            )
            with self.assertRaisesRegex(
                ValidationError, "cycle|multiple path"
            ):
                validate_route_candidate_pool(
                    assignment, platform, broken_tree
                )

    def test_adaptive_hop_column_is_not_an_alias(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "adaptive_column",
                ["a", "b", "c", "d", "e", "f"],
                [
                    _link("ab", "a", "b", latency=1),
                    _link("ac", "a", "c", latency=3),
                    _link("ad", "a", "d", latency=1),
                    _link("af", "a", "f", latency=3),
                    _link("bc", "b", "c", latency=2),
                    _link("cd", "c", "d", latency=2),
                    _link("ce", "c", "e", latency=3),
                    _link("cf", "c", "f", latency=1),
                    _link("de", "d", "e", latency=1),
                    _link("ef", "e", "f", latency=3),
                ],
            )
        )
        assignment = _assignment(
            platform, [("multicast", "b", ["c", "d", "e"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "p",
                            "clock_domain": "clk",
                            "clock_period_ns": 50.0,
                            "slack_ns": 30.0,
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
                "frame_slots": 16,
                "reroute_rounds": 0,
                "link_delay_ns": {
                    link.id: float(link.latency_cycles)
                    for link in platform.links
                },
            },
            platform,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pool.json"
            route_system_native(
                assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider=GLOBAL_CANDIDATE_PROVIDER,
                candidate_pool_path=path,
            )
            import json

            pool = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(pool["metrics"]["candidates_by_generator"]),
                {
                    "shortest-path-tree",
                    "delay-demand-balanced",
                    "nearest-terminal-steiner",
                    "directed-metric-closure",
                    "shallow-light-tree",
                    "adaptive-hop-tree",
                    "refined-final",
                },
            )
            by_generator = {
                candidate["generator"]: {
                    edge["link"] for edge in candidate["tree_edges"]
                }
                for candidate in pool["candidates"]
            }
            self.assertEqual(
                by_generator["adaptive-hop-tree"],
                {"ab", "ad", "bc", "ce"},
            )
            self.assertNotEqual(
                by_generator["adaptive-hop-tree"],
                by_generator["nearest-terminal-steiner"],
            )

    def test_global_candidate_provider_matches_exact_restricted_master(
        self,
    ) -> None:
        platform, assignment, timing, constraints = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pool.json"
            routes = route_system_native(
                assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider=GLOBAL_CANDIDATE_PROVIDER,
                candidate_pool_path=path,
            )
            import json

            pool = json.loads(path.read_text(encoding="utf-8"))
            oracle = exact_route_candidate_selection(
                assignment, platform, pool, timing
            )
            selection = routes["joint_optimization"][
                "candidate_generation"
            ]["master_selection"]
            self.assertIn(selection, oracle["optimal_selections"])
            self.assertTrue(routes["metrics"]["master_exact"])
            self.assertEqual(routes["metrics"]["master_rounds"], 1)
            self.assertEqual(
                validate_native_system_routes(
                    assignment, platform, routes, timing
                )["status"],
                "pass",
            )

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
            root = Path(temporary)
            pools = []
            routes = []
            for workers in (1, 2, 8):
                pool = root / f"pool-{workers}.json"
                pools.append(pool)
                routes.append(
                    route_system_native(
                        assignment,
                        platform,
                        constraints,
                        timing,
                        executable=str(tlr_router()),
                        provider=GLOBAL_CANDIDATE_PROVIDER,
                        candidate_pool_path=pool,
                        candidate_workers=workers,
                    )
                )
            self.assertEqual(routes[0], routes[1])
            self.assertEqual(routes[0], routes[2])
            self.assertEqual(pools[0].read_bytes(), pools[1].read_bytes())
            self.assertEqual(pools[0].read_bytes(), pools[2].read_bytes())
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
            pool_path = Path(temporary) / "pool.json"
            routes = route_system_native(
                assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider=GLOBAL_CANDIDATE_PROVIDER,
                candidate_pool_path=pool_path,
            )
            import json

            pool = json.loads(pool_path.read_text(encoding="utf-8"))
            oracle = exact_route_candidate_selection(
                assignment, platform, pool, timing
            )
            selection = routes["joint_optimization"][
                "candidate_generation"
            ]["master_selection"]
            self.assertIn(selection, oracle["optimal_selections"])
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
