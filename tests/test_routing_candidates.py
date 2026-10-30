from tests.native_route_fixture import route_system_native, write_assignment_cuts
from contextlib import closing
import copy
import json
import sqlite3
import re
import tempfile
import unittest
from pathlib import Path

from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform
from emuflow.routing.routing import normalize_route_constraints
from tests.routing_validation_reference import demands_from_assignment
from emuflow.routing.phase4_native import run_native_phase4
from emuflow.routing.system_route_storage import read_system_routes
from tests.routing_reference import exact_route_tree_selection
from tests.timing_routing_reference import (GLOBAL_CANDIDATE_PROVIDER, compress_sta_paths, normalize_sta_paths, validate_native_system_routes)
from tests.native_build import tlr_router
from tests.test_phase4 import _assignment, _link, _platform_value


class RouteCandidatePoolTest(unittest.TestCase):
    def test_adaptive_hop_does_not_clone_the_complete_input(self) -> None:
        source = (
            Path(__file__).parents[1] / "src/core/routing/global_router.cpp"
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
            self.assertEqual(
                sorted(index for batch in batching['batches'] for index in batch),
                list(range(len(timing['paths']))),
            )
            self.assertEqual(
                validate_native_system_routes(assignment, platform, routes[0], timing)['status'],
                'pass',
            )

    def test_large_native_conflict_batches_preserve_priority_and_path_coverage(self):
        # A single capacity domain forces every path into a distinct batch.
        # More than 4096 paths exercises the native linear-resource branch.
        path_count = 4100
        platform = Platform.from_dict(_platform_value('large_batches', ['a', 'b'],
            [_link('ab', 'a', 'b', lanes=path_count)]))
        assignment = _assignment(platform,
            [(f'n{index:05d}', 'a', ['b']) for index in range(path_count)])
        timing = {'schema': 'emuflow.sta-paths/v1', 'design': 'route_test',
            'paths': [{'id': f'p{index:05d}', 'clock_domain': 'clk',
                'clock_period_ns': 4.0, 'slack_ns': float(index % 3),
                'fixed_delay_ns': 0.0, 'cut_nets': [f'n{index:05d}']}
                for index in range(path_count)]}
        timing = compress_sta_paths(normalize_sta_paths(
            timing, demands_from_assignment(assignment, platform)))
        # The normalized source explicitly owns its record order. Keep that
        # order separate from slack priority to test both priority and ties.
        timing['paths'].sort(key=lambda row: row['id'])
        priority = sorted(range(path_count), key=lambda index: (index % 3, index))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write_assignment_cuts(root/'assignment.sqlite', assignment, platform)
            board = root/'board.json'; board.write_text(json.dumps(platform.to_dict()))
            paths = root/'paths.json'; paths.write_text(json.dumps(timing))
            constraints = root/'constraints.json'
            constraints.write_text(json.dumps({'schema': 'emuflow.system-route-constraints/v1',
                'frame_slots': 8, 'reroute_rounds': 0}))
            output = root/'native'
            report = run_native_phase4(source, board, output, timing_paths=paths,
                constraints_path=constraints, executable=str(tlr_router()))
            self.assertEqual(report['stage_implementation_id'], 'native-phase4-v1')
            routes = read_system_routes(output/'routes.json')
            batches = routes['joint_optimization']['refinement_batches']
            self.assertEqual(batches['batch_count'], path_count)
            self.assertEqual(batches['maximum_parallel_batch'], 1)
            self.assertEqual(list(batches['batches']), [[index] for index in priority])
            observed = [index for batch in batches['batches'] for index in batch]
            self.assertEqual(sorted(observed), list(range(path_count)))
            self.assertEqual(len(observed), len(set(observed)))
            with closing(sqlite3.connect(routes.store_path)) as db, db:
                self.assertEqual(db.execute('SELECT seq,path_id FROM timing_paths ORDER BY seq').fetchall(),
                    [(index, f'p{index:05d}') for index in range(path_count)])
                self.assertEqual(db.execute('SELECT batch_index,position,path_index FROM refinement_batch_paths ORDER BY seq').fetchall(),
                    [(batch, 0, index) for batch, index in enumerate(priority)])
                self.assertEqual(db.execute('SELECT count(*) FROM route_edges WHERE link_id=? AND source_fpga=? AND sink_fpga=?',
                    ('ab', 'a', 'b')).fetchone()[0], path_count)
                # Change an actual typed fact to an out-of-range path. The
                # original sealed selector must reject the damaged generation.
                changed = db.execute('UPDATE refinement_batch_paths SET path_index=? WHERE seq=0', (path_count,))
                self.assertEqual(changed.rowcount, 1)
                self.assertEqual(db.execute('SELECT path_index FROM refinement_batch_paths WHERE seq=0').fetchone()[0], path_count)
            with self.assertRaises(ValidationError):
                read_system_routes(output/'routes.json')

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
