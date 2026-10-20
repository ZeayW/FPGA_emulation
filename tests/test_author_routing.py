"""The author kernel must satisfy the same routing/TDM contract as native P4."""

import copy
from functools import lru_cache
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from emuflow.author_routing import (
    AUTHOR_ROUTER_PROVIDER, _parse_author_output,
    route_system_author, validate_author_system_routes,
)
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json
from emuflow.phase4 import run_phase4, validate_phase4
from emuflow.phase5 import run_phase5
from emuflow.partition import assign_clusters, build_clusters, normalize_partition_constraints
from emuflow.platform import Platform
from emuflow.routing import demands_from_assignment, normalize_route_constraints
from emuflow.tdm import TDM_BASELINE_PROVIDER, TDM_EXACT_DEPENDENCY_PROVIDER
from emuflow.timing_routing import GLOBAL_CANDIDATE_PROVIDER, compress_sta_paths, normalize_sta_paths
from emuflow.tdm_ratio import TDM_TIMING_DAG_RATIO_PROVIDER
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router, tdm_ratio_optimizer, tdm_timing_dag_optimizer
from tests.test_phase4 import _assignment, _link, _platform_value


ROOT = Path(__file__).resolve().parents[1]
_BUILD = tempfile.TemporaryDirectory(prefix="emuflow-author-tests-")


@lru_cache(maxsize=1)
def author_router():
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        raise RuntimeError("C++17 compiler required for author routing tests")
    target = Path(_BUILD.name) / "emuflow_synergistic_author_router"
    subprocess.run([compiler, "-std=c++17", "-O2", str(ROOT / "src/native/synergistic_author_router.cpp"), "-o", str(target)], check=True)
    return str(target)


class AuthorRoutingTest(unittest.TestCase):
    def test_explicit_cli_provider_and_existing_default(self):
        from emuflow.cli import _build_parser
        parser = _build_parser()
        args = ["phase4", "--assignment", "a.json", "--platform", "b.json", "--out", "out"]
        self.assertIsNone(parser.parse_args(args).provider)
        self.assertEqual(parser.parse_args(args + ["--provider", AUTHOR_ROUTER_PROVIDER]).provider, AUTHOR_ROUTER_PROVIDER)

    def _inputs(self, cuts=None, links=None, raw_constraints=None):
        platform = Platform.from_dict(_platform_value("author-test", ["a", "b", "c", "d"], links or [
            _link("ab", "a", "b", lanes=4), _link("bd", "b", "d", lanes=4),
            _link("ac", "a", "c", lanes=4), _link("cd", "c", "d", lanes=4),
        ]))
        assignment = _assignment(platform, cuts if cuts is not None else [("multicast", "a", ["b", "d"])])
        constraints = normalize_route_constraints({"schema": "emuflow.system-route-constraints/v1", "frame_slots": 16, **(raw_constraints or {})}, platform)
        return assignment, platform, constraints

    def _route(self, assignment, platform, constraints, timing=None):
        return route_system_author(assignment, platform, constraints, timing, executable=author_router())

    def test_multicast_tree_and_deterministic_identity(self):
        args = self._inputs()
        routed = self._route(*args)
        self.assertEqual(routed["provider"], AUTHOR_ROUTER_PROVIDER)
        self.assertEqual(routed, self._route(*args))
        self.assertEqual(routed["metrics"]["routed_sinks"], 2)
        self.assertEqual(len(routed["routes"][0]["tree_edges"]), 2)
        self.assertEqual(validate_author_system_routes(args[0], args[1], routed)["status"], "pass")
        self.assertNotIn("accepted_reroutes", routed["metrics"])

    def test_empty_demands_succeed_without_fabricated_timing(self):
        args = self._inputs(cuts=[])
        routed = self._route(*args)
        self.assertEqual(routed["routes"], [])
        self.assertEqual(routed["author_routing"]["counters"]["connections"], 0)
        self.assertNotIn("timing", routed)

    def test_explicit_sll_policy_is_independently_identifiable(self):
        assignment, platform, constraints = self._inputs()
        routed = route_system_author(
            assignment, platform, constraints, executable=author_router(),
            cost_policy="author-sll-pressure-v1",
        )
        self.assertEqual(routed["author_routing"]["cost_policy"], "author-sll-pressure-v1")
        self.assertEqual(validate_author_system_routes(assignment, platform, routed)["status"], "pass")
        tampered = copy.deepcopy(routed)
        tampered["author_routing"]["cost_policy"] = "author-pressure-v1"
        with self.assertRaisesRegex(ValidationError, "execution metadata"):
            validate_author_system_routes(assignment, platform, tampered)
        with self.assertRaisesRegex(ValueError, "unsupported author routing cost policy"):
            route_system_author(assignment, platform, constraints, cost_policy="unknown-policy")

    def test_timing_evaluated_from_platform_and_checked_again(self):
        assignment, platform, constraints = self._inputs()
        timing = compress_sta_paths(normalize_sta_paths({"schema": "emuflow.sta-paths/v1", "design": "route_test", "paths": [{
            "id": "p0", "clock_domain": "clk", "clock_period_ns": 10.0,
            "slack_ns": 5.0, "fixed_delay_ns": 1.0, "cut_nets": ["multicast"],
        }]}, demands_from_assignment(assignment, platform)))
        routed = self._route(assignment, platform, constraints, timing)
        self.assertEqual(len(routed["timing"]["paths"]), 1)
        self.assertFalse(routed["author_routing"]["timing_optimization"])
        tampered = copy.deepcopy(routed)
        tampered["timing"]["paths"][0]["delay_ns"] += 1.0
        with self.assertRaisesRegex(ValidationError, "timing records"):
            validate_author_system_routes(assignment, platform, tampered, timing)

    def test_corruption_and_nonfinite_metadata_rejected(self):
        assignment, platform, constraints = self._inputs()
        routed = self._route(assignment, platform, constraints)
        for mutate in (
            lambda r: r["routes"][0].update(predicted_max_delay_ns=float("nan")),
            lambda r: r["routes"][0].update(predicted_max_delay_ns=999),
            lambda r: r["author_routing"]["counters"].update(routed_connections=0),
            lambda r: r["author_routing"].update(executable_sha256="unsealed"),
            lambda r: r["routes"].clear(),
        ):
            altered = copy.deepcopy(routed)
            mutate(altered)
            with self.assertRaises(ValidationError):
                validate_author_system_routes(assignment, platform, altered)

    def test_hop_limit_and_unavailable_link(self):
        args = self._inputs(cuts=[("n0", "a", ["d"])], raw_constraints={"max_route_hops": 2, "unavailable_links": ["ab"]})
        routed = self._route(*args)
        self.assertEqual({e["link"] for e in routed["routes"][0]["tree_edges"]}, {"ac", "cd"})
        bad = self._inputs(cuts=[("n0", "a", ["d"])], raw_constraints={"max_route_hops": 1})
        with self.assertRaises(EmuFlowError):
            self._route(*bad)

    def test_transport_round_capacity_contract(self):
        assignment, platform, constraints = self._inputs(cuts=[("n0", "a", ["d"]), ("n1", "d", ["a"])])
        assignment["cut_nets"][1]["transport_round"] = 1
        routed = self._route(assignment, platform, constraints)
        self.assertEqual(routed["round_capacity_contract"]["active_rounds"], [0, 1])
        altered = copy.deepcopy(routed)
        del altered["round_capacity_contract"]
        with self.assertRaisesRegex(ValidationError, "round_capacity_contract"):
            validate_author_system_routes(assignment, platform, altered)

    def test_half_duplex_lock_is_preserved(self):
        assignment, platform, constraints = self._inputs(cuts=[("n0", "a", ["b"])], links=[_link("ab", "a", "b", direction="half_duplex")])
        routed = self._route(assignment, platform, constraints)
        self.assertEqual(routed["direction_locks"], [{"group": 0, "link": "ab", "from": "a", "to": "b"}])
        routed["direction_locks"][0].update({"from": "b", "to": "a"})
        with self.assertRaisesRegex(ValidationError, "direction lock"):
            validate_author_system_routes(assignment, platform, routed)

    def test_opposing_directions_respect_bank_granularity(self):
        for banks in (1, 2):
            link = _link("ab", "a", "b", lanes=banks)
            link.update(capacity_sharing="shared_bidirectional", bank_count=banks, wires_per_bank=1)
            args = self._inputs(cuts=[("forward", "a", ["b"]), ("reverse", "b", ["a"])], links=[link])
            if banks == 1:
                with self.assertRaises(EmuFlowError):
                    self._route(*args)
            else:
                self.assertEqual(self._route(*args)["metrics"]["routed_sinks"], 2)

    def test_static_exact_dependencies_and_existing_phase5(self):
        from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
        fixture = StaticExactCombinationalCutPartitionTest()
        fixture.setUp()
        _, assignment = fixture._exact_artifacts(dependent_return=True)
        platform = fixture.platform
        constraints = normalize_route_constraints(None, platform, frame_slots=16)
        routed = self._route(assignment, platform, constraints)
        self.assertEqual(routed["semantic_contract_sha256"], assignment["semantic_contract_sha256"])
        self.assertNotIn("semantic_contract", routed)
        self.assertNotIn("round_capacity_contract", routed)
        self.assertTrue(any(r["predecessor_cut_nets"] for r in routed["routes"]))
        altered = copy.deepcopy(routed)
        altered["routes"][0]["dependency_level"] += 1
        with self.assertRaisesRegex(ValidationError, "dependency fields"):
            validate_author_system_routes(assignment, platform, altered)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("assignment.json", assignment), ("platform.json", platform.to_dict()), ("routes.json", routed)):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            p5 = run_phase5(root / "routes.json", root / "platform.json", root / "phase5", assignment_path=root / "assignment.json")
            self.assertEqual(p5["status"], "pass")
            self.assertEqual(p5["provider"], TDM_EXACT_DEPENDENCY_PROVIDER)

    def test_phase4_artifact_enters_existing_phase5(self):
        assignment, platform, constraints = self._inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("assignment.json", assignment), ("platform.json", platform.to_dict()), ("constraints.json", constraints)):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            report = run_phase4(root / "assignment.json", root / "platform.json", root / "phase4",
                                constraints_path=root / "constraints.json", provider=AUTHOR_ROUTER_PROVIDER,
                                router=author_router())
            self.assertEqual(report["provider"], AUTHOR_ROUTER_PROVIDER)
            self.assertEqual(validate_phase4(root / "assignment.json", root / "platform.json", root / "phase4/routes.json")["status"], "pass")
            p5 = run_phase5(root / "phase4/routes.json", root / "platform.json", root / "phase5", provider=TDM_BASELINE_PROVIDER)
            self.assertEqual(p5["status"], "pass")
            self.assertEqual(p5["provider"], TDM_BASELINE_PROVIDER)
            self.assertEqual(read_json(root / "phase4/routes.json")["provider"], AUTHOR_ROUTER_PROVIDER)

    def test_mapped_counter_fixture_natural_partition_enters_existing_phase5(self):
        # Deterministic mapped fixture; fresh RTL synthesis is qualified separately.
        ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
        platform_path = ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
        platform = Platform.load(platform_path)
        constraints = normalize_partition_constraints(None, ir, platform)
        clusters = build_clusters(ir, constraints)
        assignment = assign_clusters(ir, platform, clusters, constraints, seed=4)
        self.assertGreater(len(demands_from_assignment(assignment, platform)), 0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment_path = root / "assignment.json"
            assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
            p4 = run_phase4(assignment_path, platform_path, root / "phase4", frame_slots=16,
                            provider=AUTHOR_ROUTER_PROVIDER, router=author_router())
            self.assertEqual(p4["status"], "pass")
            self.assertGreater(p4["validation"]["demands"], 0)
            self.assertGreater(p4["validation"]["routed_sinks"], 0)
            self.assertEqual(p4["validation"]["overloaded_links"], 0)
            self.assertEqual(validate_phase4(assignment_path, platform_path, root / "phase4/routes.json")["status"], "pass")
            p5 = run_phase5(root / "phase4/routes.json", platform_path, root / "phase5",
                            assignment_path=assignment_path, provider=TDM_BASELINE_PROVIDER)
            self.assertEqual(p5["status"], "pass")
            self.assertEqual(p5["provider"], TDM_BASELINE_PROVIDER)

    def test_protocol_rejects_duplicate_missing_or_unknown_records(self):
        _, _, _ = self._inputs()
        model = {"demands": [{"sinks": [1]}], "arcs": [{}], "round_boundary": None}
        valid = "EMUFLOW_SYNERGISTIC_ROUTE_OUTPUT_V1\nROUTE 0 0\nMETRIC connections 1\nMETRIC routed_connections 1\nMETRIC search_expansions 2\nMETRIC tree_edges 1\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "output"
            for bad in (valid + "ROUTE 0 0\n", valid.replace("ROUTE 0 0\n", ""), valid.replace("ROUTE 0 0", "ROUTE 0 9"), valid + "METRIC made_up 0\n"):
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(EmuFlowError):
                    _parse_author_output(path, model)

    def test_both_routers_enter_same_current_timing_dag_phase5(self):
        assignment, platform, constraints = self._inputs()
        timing = {"schema": "emuflow.sta-paths/v1", "design": "route_test", "paths": [{
            "id": "p0", "clock_domain": "clk", "clock_period_ns": 10.0,
            "slack_ns": 5.0, "fixed_delay_ns": 1.0, "cut_nets": ["multicast"],
            "cut_transitions": [{"net": "multicast", "from": "a", "to": "d"}],
        }]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("assignment.json", assignment), ("platform.json", platform.to_dict()), ("constraints.json", constraints), ("timing.json", timing)):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            for label, provider, executable in (
                ("baseline", GLOBAL_CANDIDATE_PROVIDER, str(tlr_router())),
                ("author", AUTHOR_ROUTER_PROVIDER, author_router()),
            ):
                out = root / label
                p4 = run_phase4(root / "assignment.json", root / "platform.json", out / "phase4",
                                constraints_path=root / "constraints.json", provider=provider,
                                timing_paths_path=root / "timing.json", router=executable)
                self.assertEqual(p4["provider"], provider)
                self.assertEqual(validate_phase4(root / "assignment.json", root / "platform.json", out / "phase4/routes.json", timing_paths_path=root / "timing.json")["status"], "pass")
                p5 = run_phase5(out / "phase4/routes.json", root / "platform.json", out / "phase5",
                                ratio_optimizer=str(tdm_ratio_optimizer()),
                                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                                ratio_max_iterations=20, post_refinement_iterations=20)
                self.assertEqual(p5["status"], "pass")
                self.assertEqual(p5["optimization_provider"], TDM_TIMING_DAG_RATIO_PROVIDER)


if __name__ == "__main__":
    unittest.main()
