"""Atomic whole-solution author selection, including real Phase 4/5 checks."""

from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import emuflow.author_route_tdm as coordinator
from emuflow.author_routing import AUTHOR_ROUTER_PROVIDER
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json
from emuflow.tdm import TDM_BASELINE_PROVIDER
from tests import test_author_routing as author_fixtures

author_router = author_fixtures.author_router


class AuthorRouteTdmTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="emuflow-author-pair-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assignment, self.platform = self.root / "assignment.json", self.root / "platform.json"
        self.assignment.write_text('{}\n')
        self.platform.write_text('{}\n')
        self.route_dir, self.tdm_dir = self.root / "system-route", self.root / "tdm"

    def _run(self, **kwargs):
        return coordinator.run_author_route_tdm(
            self.assignment, self.platform, self.route_dir, self.tdm_dir, **kwargs,
        )

    def _fake_stages(self, scores, *, fail=(), programming_error=False, mutate=False, timing=True):
        """Controlled checked summaries test selection; real tests below run tools."""
        seen = []

        def phase4(assignment, platform, output, **options):
            policy = options["author_cost_policy"]
            seen.append(policy)
            if programming_error:
                raise RuntimeError("unexpected implementation defect")
            if policy in fail:
                raise EmuFlowError("no legal candidate at /private/server/run")
            output.mkdir(parents=True)
            report = {"status": "pass", "provider": AUTHOR_ROUTER_PROVIDER,
                      "author_routing": {"cost_policy": policy, "executable_sha256": "f" * 64}}
            (output / "routes.json").write_text(json.dumps({"policy": policy}))
            (output / "phase4_report.json").write_text(json.dumps(report))
            if mutate:
                assignment.write_text('{"changed": true}\n')
            return report

        def phase5(routes, platform, output, **options):
            policy = read_json(routes)["policy"]
            output.mkdir(parents=True)
            for name in ("schedule.json", "cross_layer_timing.json"):
                (output / name).write_text(json.dumps({"policy": policy}))
            report = {"status": "pass", "provider": TDM_BASELINE_PROVIDER}
            (output / "phase5_report.json").write_text(json.dumps(report))
            return report

        def validate(assignment, platform, route_dir, tdm_dir, timing_paths):
            policy = read_json(route_dir / "routes.json")["policy"]
            if (read_json(tdm_dir / "schedule.json")["policy"] != policy or
                    read_json(tdm_dir / "cross_layer_timing.json")["policy"] != policy):
                raise ValidationError("cross-layer pair mismatch")
            checked = {"status": "pass", "completion_slot": 4,
                       "cross_layer_timing": {"status": "pass"}}
            if timing:
                checked["timing"] = {"status": "pass", "worst_normalized_slack": scores[policy],
                    "total_negative_normalized_slack": 0, "negative_slack_paths": 0,
                    "p01_normalized_slack": scores[policy], "median_normalized_slack": scores[policy]}
            return {"status": "pass"}, checked

        stack = ExitStack()
        stack.enter_context(patch.object(coordinator, "run_phase4", side_effect=phase4))
        stack.enter_context(patch.object(coordinator, "run_phase5", side_effect=phase5))
        stack.enter_context(patch.object(coordinator, "_validate_pair", side_effect=validate))
        return stack, seen

    def test_each_candidate_can_win_and_exact_ties_keep_continuous(self):
        for a, b, expected in ((2, 1, "author-pressure-v1"), (1, 2, "author-sll-pressure-v1"),
                               (1, 1, "author-pressure-v1")):
            with self.subTest(scores=(a, b)), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.route_dir, self.tdm_dir = Path(directory) / "system-route", Path(directory) / "tdm"
                stages, seen = self._fake_stages(dict(zip(coordinator.AUTHOR_ROUTE_TDM_POLICIES, (a, b))))
                with stages:
                    result = self._run()
                report = result["selection"]
                self.assertEqual(report["selected_cost_policy"], expected)
                self.assertEqual(read_json(self.route_dir / "routes.json")["policy"], expected)
                self.assertEqual(read_json(self.tdm_dir / "schedule.json")["policy"], expected)
                self.assertEqual(seen, list(coordinator.AUTHOR_ROUTE_TDM_POLICIES))
                self.assertGreaterEqual(report["wall_seconds"], 0)
                for candidate in report["candidates"]:
                    self.assertGreaterEqual(candidate["phase4_seconds"], 0)
                    self.assertGreaterEqual(candidate["phase5_seconds"], 0)
                    self.assertFalse(Path(candidate["route_dir"]).is_absolute())

    def test_one_legitimate_failure_keeps_other_author_candidate(self):
        scores = dict.fromkeys(coordinator.AUTHOR_ROUTE_TDM_POLICIES, 1)
        stages, _ = self._fake_stages(scores, fail=("author-pressure-v1",))
        with stages:
            result = self._run()
        self.assertEqual(result["selection"]["selected_cost_policy"], "author-sll-pressure-v1")
        rejected = result["selection"]["candidates"][0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertNotIn("/private/server", rejected["reason"])

    def test_all_fail_preserves_failure_receipt_without_canonical_outputs(self):
        stages, _ = self._fake_stages({}, fail=coordinator.AUTHOR_ROUTE_TDM_POLICIES)
        with stages, self.assertRaisesRegex(EmuFlowError, "both author"):
            self._run()
        report = read_json(self.root / "author-route-tdm/selection.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["candidates"]), 2)
        self.assertFalse(self.route_dir.exists())
        self.assertFalse(self.tdm_dir.exists())

    def test_programming_errors_are_not_disguised_as_infeasible_candidates(self):
        stages, seen = self._fake_stages({}, programming_error=True)
        with stages, self.assertRaisesRegex(RuntimeError, "implementation defect"):
            self._run()
        self.assertEqual(len(seen), 1)

    def test_common_input_mutation_aborts_instead_of_selecting_the_other_candidate(self):
        stages, seen = self._fake_stages(dict.fromkeys(coordinator.AUTHOR_ROUTE_TDM_POLICIES, 1), mutate=True)
        with stages, self.assertRaisesRegex(EmuFlowError, "common input changed"):
            self._run()
        self.assertEqual(len(seen), 1)
        self.assertFalse(self.route_dir.exists())

    def test_input_contents_are_hashed_once_not_per_stage(self):
        original = coordinator._sha256
        hashed = []

        def digest(path):
            hashed.append(Path(path).resolve())
            return original(path)

        stages, _ = self._fake_stages(dict.fromkeys(coordinator.AUTHOR_ROUTE_TDM_POLICIES, 1))
        with stages, patch.object(coordinator, "_sha256", side_effect=digest):
            result = self._run()
        self.assertEqual(hashed.count(self.assignment.resolve()), 1)
        self.assertEqual(hashed.count(self.platform.resolve()), 1)
        self.assertEqual(result["selection"]["common_inputs"]["assignment"]["sha256"], original(self.assignment))

    def test_input_alias_retargeting_is_detected(self):
        alias, replacement = self.root / "input-link.json", self.root / "replacement.json"
        replacement.write_text('{"different": true}\n')
        alias.symlink_to(self.assignment)
        guard = coordinator._Inputs({"assignment": (alias, True)})
        alias.unlink()
        alias.symlink_to(replacement)
        with self.assertRaisesRegex(EmuFlowError, "common input changed"):
            guard.check()

    def test_realized_timing_objective_has_declared_lexicographic_priority(self):
        timing = {"status": "pass", "worst_normalized_slack": -1,
                  "total_negative_normalized_slack": -10, "negative_slack_paths": 4,
                  "p01_normalized_slack": -2, "median_normalized_slack": 0}

        def score(**changes):
            return coordinator._objective({"completion_slot": 8, "timing": {**timing, **changes}}, True)[1]

        baseline = score()
        self.assertGreater(score(worst_normalized_slack=-0.5, total_negative_normalized_slack=-100), baseline)
        self.assertGreater(score(total_negative_normalized_slack=-9, negative_slack_paths=20), baseline)
        self.assertGreater(score(negative_slack_paths=3), baseline)
        self.assertGreater(score(p01_normalized_slack=-1.5), baseline)
        self.assertGreater(score(median_normalized_slack=1), baseline)
        with self.assertRaises(ValidationError):
            score(worst_normalized_slack=float("nan"))

    def test_missing_expected_timing_cannot_fall_back_to_completion(self):
        timing = self.root / "timing.json"
        timing.write_text('{}\n')
        stages, _ = self._fake_stages({}, timing=False)
        with stages, self.assertRaisesRegex(EmuFlowError, "both author"):
            self._run(route_options={"timing_paths_path": timing})
        report = read_json(self.root / "author-route-tdm/selection.json")
        self.assertTrue(all("timing coverage" in x["reason"] for x in report["candidates"]))

    def test_replacing_both_members_with_equal_scoring_other_pair_is_rejected(self):
        stages, _ = self._fake_stages(dict.fromkeys(coordinator.AUTHOR_ROUTE_TDM_POLICIES, 1))
        copytree = shutil.copytree

        def other_pair(source, destination, *args, **kwargs):
            if Path(destination).resolve() in (self.route_dir.resolve(), self.tdm_dir.resolve()):
                source = Path(str(source).replace("author-pressure-v1", "author-sll-pressure-v1"))
            return copytree(source, destination, *args, **kwargs)

        with stages, patch.object(coordinator.shutil, "copytree", side_effect=other_pair):
            with self.assertRaisesRegex(ValidationError, "artifact identity changed"):
                self._run()

    def test_explicit_policy_and_provider_validation(self):
        for provider, policy in ((AUTHOR_ROUTER_PROVIDER, "unknown"), (None, "best-of-two-v1"),
                                  ("native-load-balanced-v1", "author-pressure-v1")):
            with self.subTest(provider=provider, policy=policy), self.assertRaises(ValidationError):
                coordinator.validate_author_flow_policy(provider, policy)
        coordinator.validate_author_flow_policy(None, None)
        coordinator.validate_author_flow_policy(AUTHOR_ROUTER_PROVIDER, "best-of-two-v1")
        for options in ({"provider": "native-load-balanced-v1"}, {"author_cost_policy": "author-pressure-v1"}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self._run(route_options=options)

    def _real_inputs(self, timing=False):
        fixture = author_fixtures.AuthorRoutingTest()
        assignment, platform, constraints = fixture._inputs(
            cuts=[("multicast", "a", ["d"])] if timing else None,
        )
        self.assignment.write_text(json.dumps(assignment))
        self.platform.write_text(json.dumps(platform.to_dict()))
        constraints_path = self.root / "constraints.json"
        constraints_path.write_text(json.dumps(constraints))
        options = {"router": author_router(), "constraints_path": constraints_path}
        if timing:
            timing_path = self.root / "timing.json"
            timing_path.write_text(json.dumps({"schema": "emuflow.sta-paths/v1", "design": "route_test", "paths": [{
                "id": "p0", "clock_domain": "clk", "clock_period_ns": 10.0, "slack_ns": 5.0,
                "fixed_delay_ns": 1.0, "cut_nets": ["multicast"],
            }]}))
            options["timing_paths_path"] = timing_path
        return options

    def test_real_author_p4_p5_pair_is_revalidated_after_promotion(self):
        result = self._run(route_options=self._real_inputs(timing=True),
                           tdm_options={"provider": TDM_BASELINE_PROVIDER})
        report = result["selection"]
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["objective"], "realized-phase5-timing-v1")
        self.assertEqual(report["promoted_phase5_validation"]["timing"]["timing_paths"], 1)
        self.assertEqual(report["promoted_phase5_validation"]["cross_layer_timing"]["status"], "pass")
        self.assertEqual(result["phase4"]["author_routing"]["cost_policy"], report["selected_cost_policy"])

    def test_real_baseline_without_timing_declares_completion_objective(self):
        result = self._run(route_options=self._real_inputs(), tdm_options={"provider": TDM_BASELINE_PROVIDER})
        self.assertEqual(result["selection"]["objective"], "minimum-realized-completion-slot-v1")
        self.assertEqual(result["selection"]["selected_cost_policy"], "author-pressure-v1")

    def test_real_cross_paired_promotion_is_rejected_by_cross_layer_hash(self):
        options = self._real_inputs()
        copytree = shutil.copytree

        def wrong_tdm(source, destination, *args, **kwargs):
            if Path(destination).resolve() == self.tdm_dir.resolve():
                source = self.root / "author-route-tdm/author-sll-pressure-v1/tdm"
            return copytree(source, destination, *args, **kwargs)

        with patch.object(coordinator.shutil, "copytree", side_effect=wrong_tdm):
            with self.assertRaises(ValidationError):
                self._run(route_options=options, tdm_options={"provider": TDM_BASELINE_PROVIDER})
        self.assertNotEqual(read_json(self.root / "author-route-tdm/selection.json")["status"], "pass")


if __name__ == "__main__":
    unittest.main()
