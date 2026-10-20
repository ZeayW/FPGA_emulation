"""The current Phase 4 default is uniform across producer entry points."""

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emuflow.author_routing import AUTHOR_ROUTER_PROVIDER
from emuflow.cli import _build_parser, main
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json
from emuflow.phase4 import run_phase4, validate_phase4
from emuflow.phase5 import run_phase5
from emuflow.tdm import TDM_EXACT_DEPENDENCY_PROVIDER
from emuflow.timing_routing import NATIVE_ROUTER_PROVIDER
from tests import test_author_routing


author_router = test_author_routing.author_router


class AuthorDefaultsTest(unittest.TestCase):
    def _inputs(self, root, *, empty=False, exact=False, timing=False):
        if exact:
            from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
            fixture = StaticExactCombinationalCutPartitionTest()
            fixture.setUp()
            _, assignment = fixture._exact_artifacts(dependent_return=True)
            platform = fixture.platform
        else:
            assignment, platform, _ = test_author_routing.AuthorRoutingTest()._inputs(cuts=[] if empty else None)
        assignment_path, platform_path = root / "assignment.json", root / "platform.json"
        assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
        platform_path.write_text(json.dumps(platform.to_dict()), encoding="utf-8")
        options = {"frame_slots": 16, "router": author_router()}
        if timing:
            timing_path = root / "timing.json"
            timing_path.write_text(json.dumps({
                "schema": "emuflow.sta-paths/v1", "design": "default-test",
                "paths": [{"id": "p0", "clock_domain": "clk", "clock_period_ns": 10.0,
                           "slack_ns": 5.0, "fixed_delay_ns": 1.0,
                           "cut_nets": ["multicast"]}],
            }), encoding="utf-8")
            options["timing_paths_path"] = timing_path
        return assignment_path, platform_path, assignment, options

    def _check(self, root, assignment_path, platform_path, options, **kwargs):
        report = run_phase4(assignment_path, platform_path, root, **options, **kwargs)
        self.assertEqual(report["provider"], AUTHOR_ROUTER_PROVIDER)
        routes = read_json(root / "routes.json")
        metadata = routes["author_routing"]
        self.assertEqual(metadata["cost_policy"], "author-pressure-v1")
        self.assertEqual(metadata.get("order_policy", "hard-connections-v1"), "hard-connections-v1")
        self.assertFalse(metadata["timing_optimization"])
        self.assertEqual(validate_phase4(assignment_path, platform_path, root / "routes.json",
                                         timing_paths_path=options.get("timing_paths_path"))["status"], "pass")
        return routes

    def test_omitted_and_none_defaults_match_with_and_without_timing(self):
        for timing in (False, True):
            with self.subTest(timing=timing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                assignment, platform, _, options = self._inputs(root, timing=timing)
                omitted = self._check(root / "omitted", assignment, platform, options)
                explicit_none = self._check(root / "none", assignment, platform, options, provider=None)
                self.assertEqual(omitted, explicit_none)
                self.assertEqual("timing" in omitted, timing)

    def test_empty_default_does_not_fabricate_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, _, options = self._inputs(root, empty=True)
            routed = self._check(root / "phase4", assignment, platform, options)
            self.assertEqual(routed["routes"], [])
            self.assertNotIn("timing", routed)

    def test_static_exact_default_enters_existing_phase5(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, source, options = self._inputs(root, exact=True)
            routes = self._check(root / "phase4", assignment, platform, options)
            self.assertEqual(routes["semantic_contract_sha256"], source["semantic_contract_sha256"])
            self.assertTrue(any(route["predecessor_cut_nets"] for route in routes["routes"]))
            p5 = run_phase5(root / "phase4/routes.json", platform, root / "phase5",
                            assignment_path=assignment)
            self.assertEqual(p5["status"], "pass")
            self.assertEqual(p5["provider"], TDM_EXACT_DEPENDENCY_PROVIDER)

    def test_default_resolves_author_binary_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, _, options = self._inputs(root)
            options.pop("router")
            with patch("emuflow.author_routing.resolve_native_executable", return_value=Path(author_router())) as resolve:
                self._check(root / "phase4", assignment, platform, options)
            resolve.assert_called_once_with("emuflow_synergistic_author_router", None)
            with patch("emuflow.author_routing.resolve_native_executable", side_effect=EmuFlowError("author unavailable")), \
                    patch("emuflow.phase4.route_system_native") as legacy:
                with self.assertRaisesRegex(EmuFlowError, "author unavailable"):
                    run_phase4(assignment, platform, root / "failed", **options)
            legacy.assert_not_called()

    def test_legacy_policy_conflicts_remain_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, _, options = self._inputs(root)
            for name, value in (("author_cost_policy", "author-pressure-v1"),
                                ("author_order_policy", "hard-connections-v1")):
                with self.subTest(option=name), self.assertRaisesRegex(ValueError, "requires the author"):
                    run_phase4(assignment, platform, root / "failed", provider=NATIVE_ROUTER_PROVIDER,
                               **options, **{name: value})
            with self.assertRaisesRegex(ValueError, "one candidate worker"):
                run_phase4(assignment, platform, root / "workers", candidate_workers=2, **options)

    def test_cli_producer_defaults_and_reader_are_distinct(self):
        parser = _build_parser()

        def command(*path):
            node = parser
            for name in path:
                subparsers = next(action for action in node._actions
                                  if isinstance(action, argparse._SubParsersAction))
                node = subparsers.choices[name]
            return node

        for path, field in ((('phase4',), "provider"),
                            (('multi-fpga', 'compile'), "route_provider"),
                            (('cross-stage', 'optimize'), "route_provider"),
                            (('experiment-stage', 'route-run'), "provider")):
            with self.subTest(command=path):
                node = command(*path)
                self.assertEqual(node.get_default(field), AUTHOR_ROUTER_PROVIDER)
                self.assertIsNone(node.get_default("author_cost_policy"))
                self.assertIsNone(node.get_default("author_order_policy"))
                workers = "route_candidate_workers" if field == "route_provider" else "candidate_workers"
                self.assertEqual(node.get_default(workers), 1)
        self.assertIsNone(command('experiment-stage', 'route-validate').get_default("provider"))
        self.assertEqual(command('contest', 'eda2025-optimize-routing').get_default("route_provider"),
                         AUTHOR_ROUTER_PROVIDER)

    def test_contest_rerouting_default_and_none_use_author(self):
        from emuflow.contest_eda2025 import optimize_eda2025_routing
        from tests import test_contest_eda2025
        from tests.native_build import eda2025_topology_optimizer
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = test_contest_eda2025.Eda2025ContestAdapterTest()._import(root)
            run_phase4(normalized / "partition_assignment.json", normalized / "boarddb.json", root / "routed",
                       constraints_path=normalized / "route_constraints.json",
                       timing_paths_path=normalized / "contest_timing_paths.json", router=author_router())
            for name, options in (("omitted", {}), ("none", {"provider": None})):
                with self.subTest(provider=name):
                    result = optimize_eda2025_routing(
                        normalized / "contest_instance.json", root / "routed/routes.json", root / name,
                        router=author_router(), topology_optimizer=str(eda2025_topology_optimizer()),
                        max_rounds=1, **options,
                    )
                    self.assertEqual(result["status"], "pass")
                    candidates = list((root / name / "rounds").rglob("routed/routes.json"))
                    self.assertTrue(candidates)
                    for route_path in candidates:
                        self.assertEqual(read_json(route_path)["provider"], AUTHOR_ROUTER_PROVIDER)

    def test_route_checkpoint_cli_forwards_explicit_policies(self):
        args = ["experiment-stage", "route-run", "--partition", "partition", "--cut-timing", "timing",
                "--platform", "platform.json", "--out", "output", "--author-cost-policy",
                "author-sll-pressure-v1", "--author-order-policy", "sta-incidence-v1"]
        with patch("emuflow.cli.run_route_checkpoint", return_value={"status": "pass"}) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertEqual(run.call_args.kwargs["provider"], AUTHOR_ROUTER_PROVIDER)
        self.assertEqual(run.call_args.kwargs["author_cost_policy"], "author-sll-pressure-v1")
        self.assertEqual(run.call_args.kwargs["author_order_policy"], "sta-incidence-v1")

    def test_validator_checks_expected_policies_without_changing_legacy_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, _, options = self._inputs(root, timing=True)
            run_phase4(assignment, platform, root / "phase4", **options)
            route_path = root / "phase4/routes.json"
            original_bytes = route_path.read_bytes()
            self.assertNotIn("order_policy", read_json(route_path)["author_routing"])
            validation = validate_phase4(assignment, platform, route_path,
                                         timing_paths_path=options["timing_paths_path"])
            with patch("emuflow.phase4.read_json", wraps=read_json) as read:
                expected = validate_phase4(
                    assignment, platform, route_path, timing_paths_path=options["timing_paths_path"],
                    expected_author_cost_policy="author-pressure-v1",
                    expected_author_order_policy="hard-connections-v1",
                )
            read.assert_called_once_with(route_path)
            self.assertEqual(expected, validation)
            self.assertEqual(route_path.read_bytes(), original_bytes)
            for keyword, value, field in (
                ("expected_author_cost_policy", "author-sll-pressure-v1", "cost_policy"),
                ("expected_author_order_policy", "sta-incidence-v1", "order_policy"),
            ):
                with self.subTest(policy=keyword), self.assertRaisesRegex(ValidationError, field):
                    validate_phase4(assignment, platform, route_path,
                                    timing_paths_path=options["timing_paths_path"], **{keyword: value})

    def test_legacy_artifact_rejects_author_policy_expectations(self):
        from tests.native_build import tlr_router
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, _, options = self._inputs(root)
            options["router"] = str(tlr_router())
            run_phase4(assignment, platform, root / "legacy", provider=NATIVE_ROUTER_PROVIDER, **options)
            route_path = root / "legacy/routes.json"
            self.assertEqual(validate_phase4(assignment, platform, route_path)["status"], "pass")
            for keyword, value in (("expected_author_cost_policy", "author-pressure-v1"),
                                   ("expected_author_order_policy", "hard-connections-v1")):
                with self.subTest(policy=keyword), self.assertRaisesRegex(ValidationError, "requires the author"):
                    validate_phase4(assignment, platform, route_path, **{keyword: value})

    def test_route_checkpoint_validation_cli_forwards_only_explicit_expectations(self):
        args = ["experiment-stage", "route-validate", "output", "--partition", "partition",
                "--cut-timing", "timing", "--platform", "platform.json"]
        for flags, expected in (([], (None, None)),
                                (["--author-cost-policy", "author-sll-pressure-v1", "--author-order-policy",
                                  "sta-incidence-v1"], ("author-sll-pressure-v1", "sta-incidence-v1"))):
            with self.subTest(flags=flags), \
                    patch("emuflow.cli.validate_route_checkpoint", return_value={"status": "pass"}) as validate, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(args + flags), 0)
            self.assertIsNone(validate.call_args.kwargs["expected_provider"])
            self.assertEqual(validate.call_args.kwargs["expected_author_cost_policy"], expected[0])
            self.assertEqual(validate.call_args.kwargs["expected_author_order_policy"], expected[1])


if __name__ == "__main__":
    unittest.main()
