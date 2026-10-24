import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.phase4 import run_phase4
from emuflow.phase5 import run_phase5
from emuflow.platform import Platform
from emuflow.routing import demands_from_assignment, normalize_route_constraints
from tests.schedule_reference import build_tdm_schedule
from emuflow.tdm_feedback import (
    build_tdm_feedback,
    validate_tdm_feedback,
)
from emuflow.timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    STA_PATHS_SCHEMA,
    compress_sta_paths,
    normalize_sta_paths,
    route_system_native,
)
from tests.native_build import tlr_router
from tests.test_phase4 import _assignment, _link, _platform_value


class TdmFeedbackTest(unittest.TestCase):
    def _timing_fixture(self):
        platform = Platform.from_dict(
            _platform_value(
                "feedback_diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("bd", "b", "d", lanes=4),
                    _link("ac", "a", "c", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["d"]), ("n1", "a", ["d"])],
        )
        timing_source = {
            "schema": STA_PATHS_SCHEMA,
            "design": "route_test",
            "paths": [
                {
                    "id": f"p{index}",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 10.0 + index,
                    "fixed_delay_ns": 1.0,
                    "cut_nets": [f"n{index}"],
                }
                for index in range(2)
            ],
        }
        timing = compress_sta_paths(
            normalize_sta_paths(
                timing_source,
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 16,
                "reroute_rounds": 0,
                "link_delay_ns": {
                    "ab": 1.0,
                    "bd": 1.0,
                    "ac": 1.1,
                    "cd": 1.1,
                },
            },
            platform,
        )
        return platform, assignment, timing_source, timing, constraints

    def test_feedback_reconstructs_domains_and_rejects_tampering(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "feedback_line",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=1),
                    _link("bc", "b", "c", lanes=1),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["c"]), ("n1", "a", ["c"])],
        )
        routes = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                },
                platform,
            ),
            executable=str(tlr_router()),
        )
        schedule = build_tdm_schedule(routes, platform)
        feedback = build_tdm_feedback(routes, platform, schedule)
        checked = validate_tdm_feedback(
            routes, platform, schedule, feedback
        )
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(checked["domains"], 4)
        self.assertEqual(checked["scheduled_bit_hops"], 4)
        self.assertEqual(checked["timing_paths"], 0)
        active = [
            domain
            for domain in feedback["domains"]
            if domain["scheduled_bit_hops"]
        ]
        self.assertEqual(len(active), 2)
        self.assertTrue(all(domain["routing_price"] > 0 for domain in active))

        tampered = copy.deepcopy(feedback)
        tampered["domains"][0]["routing_price"] += 1.0
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_tdm_feedback(
                routes, platform, schedule, tampered
            )

    def test_timing_feedback_rejects_source_and_path_tampering(self) -> None:
        platform, assignment, _, timing, constraints = self._timing_fixture()
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        schedule = build_tdm_schedule(routes, platform)
        feedback = build_tdm_feedback(routes, platform, schedule)
        self.assertEqual(len(feedback["paths"]), 2)

        changed_path = copy.deepcopy(feedback)
        changed_path["paths"][0]["transport_delay_ns"] += 0.25
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_tdm_feedback(
                routes, platform, schedule, changed_path
            )
        changed_routes = copy.deepcopy(routes)
        changed_routes["metrics"]["total_link_bit_hops"] += 1
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_tdm_feedback(
                changed_routes, platform, schedule, feedback
            )
        changed_schedule = copy.deepcopy(schedule)
        changed_schedule["metrics"]["completion_slot"] += 1
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_tdm_feedback(
                routes, platform, changed_schedule, feedback
            )

    def test_feedback_domain_projection_is_indexed_by_schedule_entry(self) -> None:
        platform, assignment, _, timing, constraints = self._timing_fixture()
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        schedule = build_tdm_schedule(routes, platform)
        feedback = build_tdm_feedback(routes, platform, schedule)
        domains = {
            entry["id"]: entry["capacity_key"]
            for entry in schedule["entries"]
        }
        scheduled_entries = {
            path["path"]: path["scheduled_entries"]
            for path in feedback["paths"]
        }
        for path in feedback["paths"]:
            self.assertEqual(
                path["capacity_domains"],
                sorted({domains[item] for item in scheduled_entries[path["path"]]}),
            )

    def test_phase4_requires_and_revalidates_feedback_sources(self) -> None:
        (
            platform,
            assignment,
            timing_source,
            timing,
            constraints,
        ) = self._timing_fixture()
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        schedule = build_tdm_schedule(routes, platform)
        feedback = build_tdm_feedback(routes, platform, schedule)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = {
                "assignment.json": assignment,
                "platform.json": platform.to_dict(),
                "timing.json": timing_source,
                "constraints.json": constraints,
                "prior-routes.json": routes,
                "prior-schedule.json": schedule,
                "feedback.json": feedback,
            }
            for name, value in values.items():
                (root / name).write_text(
                    json.dumps(value), encoding="utf-8"
                )
            with self.assertRaisesRegex(
                ValueError, "requires --tdm-feedback-routes"
            ):
                run_phase4(
                    root / "assignment.json",
                    root / "platform.json",
                    root / "missing-sources",
                    constraints_path=root / "constraints.json",
                    timing_paths_path=root / "timing.json",
                    provider=GLOBAL_CANDIDATE_PROVIDER,
                    router=str(tlr_router()),
                    tdm_feedback_path=root / "feedback.json",
                )
            report = run_phase4(
                root / "assignment.json",
                root / "platform.json",
                root / "phase4",
                constraints_path=root / "constraints.json",
                timing_paths_path=root / "timing.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
                tdm_feedback_path=root / "feedback.json",
                tdm_feedback_routes_path=root / "prior-routes.json",
                tdm_feedback_schedule_path=root / "prior-schedule.json",
            )
            self.assertEqual(
                report["tdm_feedback"]["validation"]["status"], "pass"
            )
            self.assertEqual(
                report["artifacts"]["tdm_feedback"],
                "tdm_feedback.normalized.json",
            )
            broken = copy.deepcopy(feedback)
            broken["domains"][0]["routing_price"] += 1.0
            (root / "feedback.json").write_text(
                json.dumps(broken), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "does not match"):
                run_phase4(
                    root / "assignment.json",
                    root / "platform.json",
                    root / "tampered",
                    constraints_path=root / "constraints.json",
                    timing_paths_path=root / "timing.json",
                    provider=GLOBAL_CANDIDATE_PROVIDER,
                    router=str(tlr_router()),
                    tdm_feedback_path=root / "feedback.json",
                    tdm_feedback_routes_path=root / "prior-routes.json",
                    tdm_feedback_schedule_path=(
                        root / "prior-schedule.json"
                    ),
                )

    def test_concrete_domain_price_changes_candidate_generation(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "feedback_behavior",
                ["c", "d", "e", "f", "g"],
                [
                    _link("cd", "c", "d", lanes=1, latency=2),
                    _link("cf", "c", "f", lanes=2, latency=1),
                    _link("de", "d", "e", lanes=2, latency=1),
                    _link("dg", "d", "g", lanes=4, latency=1),
                    _link("ef", "e", "f", lanes=1, latency=2),
                    _link("fg", "f", "g", lanes=1, latency=1),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("to_e", "c", ["e"]), ("to_d", "c", ["d"])],
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": STA_PATHS_SCHEMA,
                    "design": "route_test",
                    "paths": [
                        {
                            "id": f"p{index}",
                            "clock_domain": "clk",
                            "clock_period_ns": 80.0,
                            "slack_ns": 30.0 + index,
                            "fixed_delay_ns": 1.0,
                            "cut_nets": [net],
                        }
                        for index, net in enumerate(("to_e", "to_d"))
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "max_iterations": 12,
                "reroute_rounds": 2,
                "link_delay_ns": {
                    "cd": 2.0,
                    "cf": 1.0,
                    "de": 1.0,
                    "dg": 1.0,
                    "ef": 2.0,
                    "fg": 1.0,
                },
            },
            platform,
        )
        first = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        schedule = build_tdm_schedule(first, platform)
        feedback = build_tdm_feedback(first, platform, schedule)
        validate_tdm_feedback(first, platform, schedule, feedback)
        second = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
            tdm_feedback=feedback,
        )
        route_by_net = {
            route["net"]: route for route in first["routes"]
        }
        feedback_route_by_net = {
            route["net"]: route for route in second["routes"]
        }
        self.assertEqual(
            [edge["link"] for edge in route_by_net["to_e"]["tree_edges"]],
            ["cd", "de"],
        )
        self.assertEqual(
            [
                edge["link"]
                for edge in feedback_route_by_net["to_e"]["tree_edges"]
            ],
            ["cf", "ef"],
        )
        prices = {
            domain["key"]: domain["routing_price"]
            for domain in feedback["domains"]
        }
        self.assertGreater(prices["cd:c->d"], prices["cf:c->f"])
        self.assertEqual(
            second["joint_optimization"]["tdm_feedback"][
                "source_schedule_sha256"
            ],
            feedback["source_schedule_sha256"],
        )

    def test_feedback_is_explicit_and_not_a_phase5_side_effect(self):
        from emuflow.system_route_storage import read_system_routes
        from emuflow.tdm_schedule_storage import read_tdm_schedule
        from tests.phase5_fixture import write_native_phase5_fixture
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path, platform_path, assignment_path = write_native_phase5_fixture(root)
            output = root / "phase5"
            report = run_phase5(routes_path, platform_path, output, assignment_path)
            self.assertNotIn("tdm_feedback", report["artifacts"])
            self.assertFalse((output / "tdm_feedback.json").exists())
            platform = Platform.load(platform_path)
            from emuflow.phase5 import _load_phase3_semantics
            routes, _ = _load_phase3_semantics(read_system_routes(routes_path), assignment_path)
            schedule = read_tdm_schedule(output / "schedule.json", materialize=True)
            feedback = build_tdm_feedback(routes, platform, schedule)
            self.assertEqual(validate_tdm_feedback(routes, platform, schedule, feedback)["status"], "pass")


if __name__ == "__main__":
    unittest.main()
