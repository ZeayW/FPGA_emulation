"""Independent lookahead policy checks retained after the VTR adapter retirement."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from emuflow.chimew_lookahead import _coalesce_timing_guard_lanes, _timing_weights
from emuflow.io import write_json
from emuflow.errors import ValidationError
from emuflow.multi_fpga_flow import PHASE6_AB_COMPARISON_SCHEMA, validate_phase6_ab_comparison

class ChimewLookaheadPolicyTests(unittest.TestCase):
    def test_timing_guard_coalesces_the_entire_frozen_lane(self) -> None:
        schedule = {
            "entries": [
                {
                    "id": "critical-slot-0",
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "lane": 2,
                },
                {
                    "id": "critical-slot-1",
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "lane": 2,
                },
                {
                    "id": "unprotected",
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "lane": 1,
                },
            ]
        }
        refined = {
            "entries": [
                {"schedule_entry": "critical-slot-0", "group": 0},
                {"schedule_entry": "critical-slot-1", "group": 1},
                # Deliberately share native group 0 with a different lane:
                # the materialization group must still isolate the guard.
                {"schedule_entry": "unprotected", "group": 0},
            ]
        }
        groups, bundles = _coalesce_timing_guard_lanes(
            schedule, refined, {"critical-slot-0"}
        )
        self.assertEqual(groups["unprotected"], 0)
        self.assertEqual(groups["critical-slot-0"], 2)
        self.assertEqual(groups["critical-slot-1"], 2)
        self.assertEqual(bundles, {"lanes":1,"refined_groups":2})

    def test_timing_weight_selects_only_the_exact_multicast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            timing = root / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "d",
                    "paths": [
                        {
                            "id": "critical",
                            "clock_period_ns": 4.0,
                            "slack_ns": -4.0,
                            "normalized_slack": -0.5,
                            "cut_nets": ["n"],
                        }
                    ],
                },
            )
            schedule = {
                "design": "d",
                "entries": [
                    {
                        "id": "side-branch",
                        "net": "n",
                        "from": "a",
                        "to": "b",
                        "link": "ab",
                    },
                    {
                        "id": "critical-hop-0",
                        "net": "n",
                        "from": "a",
                        "to": "c",
                        "link": "ac",
                    },
                    {
                        "id": "critical-hop-1",
                        "net": "n",
                        "from": "c",
                        "to": "d",
                        "link": "cd",
                    },
                ],
            }
            routes = {
                "routes": [
                    {
                        "net": "n",
                        "tree_edges": [
                            {"from": "a", "to": "b", "link": "ab"},
                            {"from": "a", "to": "c", "link": "ac"},
                            {"from": "c", "to": "d", "link": "cd"},
                        ],
                    }
                ],
                "timing": {
                    "paths": [
                        {
                            "path": "critical",
                            "cut_transitions": [
                                {"net": "n", "from": "a", "to": "d"}
                            ],
                        }
                    ]
                },
            }
            weights, source, coverage = _timing_weights(
                timing, schedule, routes
            )
            self.assertEqual(source, timing)
            self.assertEqual(weights["side-branch"], 1.0)
            self.assertEqual(weights["critical-hop-0"], 10.0)
            self.assertEqual(weights["critical-hop-1"], 10.0)
            self.assertEqual(
                coverage, {"exact_path_hops": 2, "whole_net_fallbacks": 0}
            )

    def test_negative_slack_severity_does_not_saturate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            timing = Path(temporary_directory) / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "d",
                    "paths": [
                        {
                            "clock_period_ns": 4.0,
                            "slack_ns": -4.0,
                            "normalized_slack": -0.5,
                            "cut_nets": ["worst"],
                        },
                        {
                            "clock_period_ns": 4.0,
                            "slack_ns": -2.0,
                            "normalized_slack": -0.25,
                            "cut_nets": ["less-critical"],
                        },
                    ],
                },
            )
            schedule = {
                "design": "d",
                "entries": [
                    {"id": "worst", "net": "worst"},
                    {"id": "less", "net": "less-critical"},
                ],
            }
            weights, _, coverage = _timing_weights(timing, schedule, {})
            self.assertEqual(weights["worst"], 10.0)
            self.assertEqual(weights["less"], 3.25)
            self.assertEqual(coverage["whole_net_fallbacks"], 2)

    def test_timing_weight_ablation_controls_are_validated(self) -> None:
        schedule = {"design": "d", "entries": []}
        with self.assertRaisesRegex(ValidationError, "weight scale"):
            _timing_weights(None, schedule, {}, scale=-1.0)
        with self.assertRaisesRegex(ValidationError, "path scope"):
            _timing_weights(None, schedule, {}, path_scope="invalid")

    def test_comparison_validator_rejects_tampered_delta(self) -> None:
        digest = "a" * 64
        def physical(wirelength: int, critical: float, wns: float) -> dict:
            return {
                "backend":{"id":"vivado"},
                "execution":{"seed":1,"requested_workers":1,"effective_workers":1},
                "architecture":{"mode":"vendor-native"},
                "fpgas": [
                    {
                        "critical_path_ns": critical,
                        "physical_result": {
                            "timing": {
                                "wns_ns": wns,
                                "tns_ns": 0.0,
                                "failing_endpoints": 0,
                                "failing_endpoint_constraints": 0,
                            },
                            "closure": {
                                "unrouted_nets": 0,
                                "drc_violations": 0,
                            },
                        },
                    }
                ]
            }

        baseline_physical = physical(100, 10.0, 1.0)
        chimew_physical = physical(90, 9.0, 2.0)
        def runtime(wns: float, tns: float, failing: int) -> dict:
            clock = {
                "worst_slack_bound_ns": wns,
                "total_negative_slack_bound_ns": tns,
                "negative_slack_paths": failing,
            }
            return {
                "system_timing": {
                    "status": "pass" if wns >= 0.0 else "fail",
                    "target_clock": dict(clock),
                    "runtime_clock": dict(clock),
                }
            }

        baseline_runtime = runtime(-2.0, -3.0, 2)
        chimew_runtime = runtime(-1.0, -1.5, 1)
        report = {
            "schema": PHASE6_AB_COMPARISON_SCHEMA,
            "status": "pass",
            "selected_provider": "chimew",
            "baseline_provider": "baseline",
            "qualification": "xilinx-placed-lookahead+fixed-lvds-wiring",
            "frozen_upstream": {
                "emuir_sha256": digest,
                "assignment_sha256": digest,
                "routes_sha256": digest,
                "schedule_sha256": digest,
                "platform_sha256": digest,
            },
            "baseline": {
                "physical": baseline_physical,
                "runtime": baseline_runtime,
            },
            "chimew": {
                "physical": chimew_physical,
                "runtime": chimew_runtime,
            },
            "baseline_physical": {
                "worst_critical_path_ns": 10.0,
                "worst_wns_ns": 1.0,
                "total_tns_ns": 0.0,
                "failing_endpoints": 0,
                "failing_endpoint_constraints": 0,
                "unrouted_nets": 0,
                "drc_violations": 0,
            },
            "chimew_physical": {
                "worst_critical_path_ns": 9.0,
                "worst_wns_ns": 2.0,
                "total_tns_ns": 0.0,
                "failing_endpoints": 0,
                "failing_endpoint_constraints": 0,
                "unrouted_nets": 0,
                "drc_violations": 0,
            },
            "physical_delta": {
                "worst_critical_path_ns": -1.0,
                "worst_wns_ns": 1.0,
                "total_tns_ns": 0.0,
                "failing_endpoints": 0,
                "failing_endpoint_constraints": 0,
            },
            "baseline_system_timing": {
                "target_clock_worst_slack_bound_ns": -2.0,
                "target_clock_total_negative_slack_bound_ns": -3.0,
                "target_clock_negative_slack_paths": 2,
                "runtime_clock_worst_slack_bound_ns": -2.0,
                "runtime_clock_total_negative_slack_bound_ns": -3.0,
                "runtime_clock_negative_slack_paths": 2,
            },
            "chimew_system_timing": {
                "target_clock_worst_slack_bound_ns": -1.0,
                "target_clock_total_negative_slack_bound_ns": -1.5,
                "target_clock_negative_slack_paths": 1,
                "runtime_clock_worst_slack_bound_ns": -1.0,
                "runtime_clock_total_negative_slack_bound_ns": -1.5,
                "runtime_clock_negative_slack_paths": 1,
            },
            "system_timing_delta": {
                "target_clock_worst_slack_bound_ns": 1.0,
                "target_clock_total_negative_slack_bound_ns": 1.5,
                "target_clock_negative_slack_paths": -1,
                "runtime_clock_worst_slack_bound_ns": 1.0,
                "runtime_clock_total_negative_slack_bound_ns": 1.5,
                "runtime_clock_negative_slack_paths": -1,
            },
            "pin_plan_metrics": {"signals": 2},
        }
        with patch(
            "emuflow.multi_fpga_flow.validate_multi_fpga_physical_report",
            return_value={"status": "pass"},
        ):
            self.assertEqual(
                validate_phase6_ab_comparison(report)["status"], "pass"
            )
            report["chimew"]["physical"]["execution"]["seed"] = 2
            with self.assertRaisesRegex(ValidationError, "execution options differ"):
                validate_phase6_ab_comparison(report)
            report["chimew"]["physical"]["execution"]["seed"] = 1
            report["physical_delta"]["worst_critical_path_ns"] = -2.0
            with self.assertRaisesRegex(ValidationError, "deltas disagree"):
                validate_phase6_ab_comparison(report)
