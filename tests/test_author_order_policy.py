import copy
import hashlib
import json
from unittest import mock
import unittest

import emuflow.author_routing as author
from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.routing import demands_from_assignment, normalize_route_constraints
from emuflow.timing_routing import compress_sta_paths, normalize_sta_paths
from tests.test_author_routing import author_router
from tests.test_output_realization_timing import registry
from tests.test_phase4 import _assignment, _link, _platform_value


class AuthorOrderPolicyTest(unittest.TestCase):
    def inputs(self, cuts=None):
        links = [_link("ab", "a", "b"), _link("bd", "b", "d"),
                 _link("ac", "a", "c"), _link("cd", "c", "d")]
        platform = Platform.from_dict(_platform_value("order-test", ["a", "b", "c", "d"], links))
        assignment = _assignment(platform, cuts or [("a_low", "a", ["d"]), ("z_high", "a", ["d"])])
        constraints = normalize_route_constraints({"schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 16, "sll_links": [link["id"] for link in links]}, platform)
        return assignment, platform, constraints

    def timing(self, assignment, platform, counts):
        paths = [{"id": f"{net}/p{member}", "clock_domain": "clk", "clock_period_ns": 10.,
                  "fixed_delay_ns": 1., "slack_ns": 9., "cut_nets": [net]}
                 for net, count in counts.items() for member in range(count)]
        return compress_sta_paths(normalize_sta_paths({"schema": "emuflow.sta-paths/v1",
            "design": "route_test", "paths": paths}, demands_from_assignment(assignment, platform)))

    def route(self, inputs, timing=None, **options):
        return author.route_system_author(*inputs, timing, executable=author_router(), **options)

    def test_default_and_explicit_hard_policy_preserve_input_and_route_bytes(self):
        inputs = self.inputs()
        timing = self.timing(*inputs[:2], {"a_low": 1, "z_high": 3})
        serialized = []
        writer = author._write_native_input
        def capture(path, *args, **kwargs):
            writer(path, *args, **kwargs)
            serialized.append(path.read_bytes())
        with mock.patch.object(author, "_write_native_input", side_effect=capture):
            default = self.route(inputs, timing)
            explicit = self.route(inputs, timing, order_policy="hard-connections-v1")
        self.assertEqual(serialized[0], serialized[1])
        self.assertEqual(json.dumps(default, separators=(",", ":")), json.dumps(explicit, separators=(",", ":")))
        self.assertNotIn("order_policy", default["author_routing"])
        self.assertFalse(default["author_routing"]["timing_optimization"])

    def test_incidence_reverses_only_serialization_and_decodes_canonical_ids(self):
        inputs = self.inputs()
        timing = self.timing(*inputs[:2], {"a_low": 1, "z_high": 3})
        order, priority = author._sta_incidence_order(demands_from_assignment(*inputs[:2]), timing)
        self.assertEqual(order, [1, 0])
        routed = self.route(inputs, timing, order_policy="sta-incidence-v1")
        self.assertEqual(routed["demands"], demands_from_assignment(*inputs[:2]))
        routes = {item["net"]: item for item in routed["routes"]}
        self.assertEqual(routes["a_low"]["id"], "d000000")
        self.assertEqual(routes["z_high"]["id"], "d000001")
        self.assertEqual({edge["link"] for edge in routes["z_high"]["tree_edges"]}, {"ab", "bd"})
        self.assertEqual({edge["link"] for edge in routes["a_low"]["tree_edges"]}, {"ac", "cd"})
        self.assertEqual(routed["author_routing"]["order_priority"], priority)
        self.assertTrue(routed["author_routing"]["timing_optimization"])
        self.assertEqual(author.validate_author_system_routes(*inputs[:2], routed, timing)["status"], "pass")

    def test_original_hardness_distance_precedes_incidence(self):
        inputs = self.inputs([("a_hard", "a", ["d"]), ("z_easy", "a", ["b"])])
        timing = self.timing(*inputs[:2], {"a_hard": 1, "z_easy": 8})
        routed = self.route(inputs, timing, order_policy="sta-incidence-v1")
        routes = {item["net"]: item for item in routed["routes"]}
        # Despite its lower serialization rank, the two-hop connection claims
        # ab first. The high-incidence one-hop connection must then go around.
        self.assertEqual({edge["link"] for edge in routes["a_hard"]["tree_edges"]}, {"ab", "bd"})
        self.assertEqual({edge["link"] for edge in routes["z_easy"]["tree_edges"]}, {"ac", "cd", "bd"})

    def test_zero_incidence_functional_demands_are_still_routed_and_validated(self):
        inputs = self.inputs()
        timing = self.timing(*inputs[:2], {"z_high": 3})
        routed = self.route(inputs, timing, order_policy="sta-incidence-v1")
        self.assertEqual({item["net"] for item in routed["routes"]}, {"a_low", "z_high"})
        self.assertEqual(routed["author_routing"]["order_priority"]["timed_demands"], 1)
        self.assertEqual(routed["author_routing"]["counters"]["routed_connections"], 2)
        self.assertEqual(author.validate_author_system_routes(*inputs[:2], routed, timing)["demands"], 2)

    def test_original_sink_count_precedes_incidence(self):
        inputs = self.inputs([("a_single", "a", ["d"]), ("z_multi", "a", ["b", "d"])])
        timing = self.timing(*inputs[:2], {"a_single": 1, "z_multi": 8})
        routed = self.route(inputs, timing, order_policy="sta-incidence-v1")
        single = next(item for item in routed["routes"] if item["net"] == "a_single")
        self.assertEqual({edge["link"] for edge in single["tree_edges"]}, {"ab", "bd"})

    def test_flat_contest_incidence_keeps_native_input_and_routes_identical(self):
        inputs = self.inputs()
        timing = self.timing(*inputs[:2], {"a_low": 1, "z_high": 1})
        serialized = []
        writer = author._write_native_input
        def capture(path, *args, **kwargs):
            writer(path, *args, **kwargs)
            serialized.append(path.read_bytes())
        with mock.patch.object(author, "_write_native_input", side_effect=capture):
            baseline = self.route(inputs, timing)
            candidate = self.route(inputs, timing, order_policy="sta-incidence-v1")
        self.assertEqual(serialized[0], serialized[1])
        for field in ("demands", "routes", "timing", "metrics", "direction_locks"):
            self.assertEqual(baseline[field], candidate[field])
        self.assertEqual(baseline["author_routing"]["counters"], candidate["author_routing"]["counters"])

    def test_compressed_members_and_output_copies_count_each_original_once(self):
        value = registry()
        copied = copy.deepcopy(value["realizations"][1])
        copied.update(id="p:fpga2", fpga="fpga2", cut_transitions=[{"net": "n", "from": "fpga0", "to": "fpga2"}])
        value["realizations"].append(copied)
        timing = {"paths": [
            {"id": "p:fpga1", "cut_nets": ["n"], "compressed_path_ids": ["p:fpga1", "q:fpga1", "u1", "u2"]},
            {"id": "p:fpga2", "cut_nets": ["n"], "compressed_path_ids": ["p:fpga2"]},
            {"id": "u3", "cut_nets": ["zz"], "compressed_path_ids": ["u3"]},
        ], "output_realizations": value}
        demands = [{"net": "zz"}, {"net": "n"}, {"net": "uncovered"}]
        order, priority = author._sta_incidence_order(demands, timing)
        self.assertEqual(order, [1, 0, 2])
        self.assertEqual(priority["maximum"], 4)  # p + q + u1 + u2, not 5 copies
        self.assertEqual(priority["timed_demands"], 2)
        expected = hashlib.sha256(json.dumps(sorted([("zz", 1), ("n", 4), ("uncovered", 0)]),
            separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(priority["sha256"], expected)
        bad = copy.deepcopy(timing)
        bad["paths"][1]["compressed_path_ids"] = ["p:fpga1"]
        with self.assertRaises(ValidationError):
            author._sta_incidence_order(demands, bad)

    def test_explicit_policy_requires_timing_and_rejects_tampering(self):
        inputs = self.inputs()
        with self.assertRaisesRegex(ValueError, "requires normalized timing"):
            self.route(inputs, order_policy="sta-incidence-v1")
        with self.assertRaisesRegex(ValueError, "unsupported author routing order"):
            self.route(inputs, order_policy="unknown")
        timing = self.timing(*inputs[:2], {"a_low": 1, "z_high": 3})
        routed = self.route(inputs, timing, order_policy="sta-incidence-v1")
        for mutate in (lambda x: x["author_routing"]["order_priority"].update(maximum=999),
                       lambda x: x["author_routing"].update(order_policy="unknown"),
                       lambda x: x["author_routing"].update(timing_optimization=False)):
            bad = copy.deepcopy(routed)
            mutate(bad)
            with self.assertRaises(ValidationError):
                author.validate_author_system_routes(*inputs[:2], bad, timing)
        with self.assertRaisesRegex(ValidationError, "require normalized timing"):
            author.validate_author_system_routes(*inputs[:2], routed)


if __name__ == "__main__":
    unittest.main()
