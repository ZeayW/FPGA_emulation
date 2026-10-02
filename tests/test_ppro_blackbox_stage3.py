from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_stage3 import (
    finalize_calibrated_capacity,
    fit_capacity_intervals,
    fit_effective_topology,
)


def observation(
    *,
    identifier: str,
    kind: str,
    generator: str,
    role: str,
    outcome: str,
    design: dict,
    routes=None,
    resource_demand=None,
):
    controlled = "fixed_assignment" if kind == "resource_capacity" else "fixed_communication"
    actions = ["partition_constraint", "random_seed"]
    if kind == "topology_reachability":
        actions.append("net_route_constraint")
    failure_code = {
        "pass": None,
        "capacity_infeasible": "capacity-boundary",
        "routing_infeasible": "routing-boundary",
    }[outcome]
    return {
        "schema": "emuflow.ppro-blackbox-observation/v1",
        "identity": {
            "id": identifier,
            "campaign_id": "stage3-test",
            "case_id": identifier,
            "role": role,
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m2",
        },
        "tool": {"name": "PPro mock", "release": "mock", "runner_revision": "1" * 64},
        "workload": {
            "generator_id": generator,
            "generator_revision": "2" * 64,
            "rtl_sha256": "3" * 64,
            "parameters_sha256": "4" * 64,
            "top_module": "stage3_probe",
        },
        "experiment": {
            "kind": kind,
            "control_mode": controlled,
            "documented_actions": actions,
            "constraints_sha256": "5" * 64,
        },
        "execution": {
            "seed": 1,
            "outcome": outcome,
            "failure_code": failure_code,
            "runtime_seconds": 1.0,
        },
        "reports": {
            "partition_summary": outcome == "pass",
            "resource_summary": outcome == "pass",
            "route_summary": outcome == "pass",
            "system_timing": outcome == "pass",
        },
        "metrics": {
            "design": design,
            "resource_demand": resource_demand or ({} if outcome != "pass" else {"lut": 1}),
            "fpga_utilization": [] if outcome != "pass" else [{"fpga": "F0", "resources": {"lut": 0.1}}],
            "assignments": [] if outcome != "pass" else [{"partition": "P0", "fpga": "F0"}],
            "routes": routes or [],
            "communication": {},
            "timing": {},
        },
        "provenance": {"class": "black_box_observation"},
        "derived": {
            "fit_eligible": role == "fit",
            "reason": "controlled-evaluated-observation" if role == "fit" else "holdout-not-fit",
        },
    }


class PProBlackboxStage3Test(unittest.TestCase):
    def test_capacity_interval_uses_repeats_and_holdout(self):
        values = []
        for units, outcome, role in ((80, "pass", "fit"), (100, "capacity_infeasible", "fit"), (70, "pass", "holdout")):
            for repeat in range(2):
                values.append(
                    observation(
                        identifier=f"cap-{units}-{repeat}",
                        kind="resource_capacity",
                        generator="ppro-blackbox-capacity-lut-v4",
                        role=role,
                        outcome=outcome,
                        design={"requested_units": units},
                        resource_demand={"lut": 72 if units == 80 else 0},
                    )
                )
        result = fit_capacity_intervals(values)
        self.assertEqual(result["axes"]["lut"]["lower_successful_units"], 80)
        self.assertEqual(result["axes"]["lut"]["upper_infeasible_units"], 100)
        self.assertTrue(result["all_resolved_holdouts_match"])
        self.assertEqual(result["holdout_checks"][0]["expected"], "pass")

    def test_capacity_rejects_non_monotonic_and_single_repeats(self):
        one_pass = observation(
            identifier="cap-pass",
            kind="resource_capacity",
            generator="ppro-blackbox-capacity-lut-v4",
            role="fit",
            outcome="pass",
            design={"requested_units": 100},
        )
        one_fail = observation(
            identifier="cap-fail",
            kind="resource_capacity",
            generator="ppro-blackbox-capacity-lut-v4",
            role="fit",
            outcome="capacity_infeasible",
            design={"requested_units": 80},
        )
        with self.assertRaisesRegex(ValidationError, "fewer than"):
            fit_capacity_intervals([one_pass, one_fail])

    def test_finalize_capacity_uses_public_limit_and_black_box_checks(self):
        root = Path(__file__).resolve().parents[1]
        prior = json.loads(
            (root / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text()
        )
        hard_axes = {}
        hard_holdouts = []
        for axis, resource, lower, upper in (
            ("bram", "bram36k", 1580, 1640),
            ("dsp", "dsp48", 2850, 2950),
            ("uram", "uram288", 238, 245),
        ):
            hard_axes[axis] = {
                "lower_successful_units": lower,
                "upper_infeasible_units": upper,
                "resource_demand_at_lower": {resource: lower},
            }
            hard_holdouts.append({"axis": axis, "matches": True})
        boundary = {
            "schema": "emuflow.ppro-capacity-fit/v1",
            "axes": hard_axes,
            "holdout_checks": hard_holdouts,
            "excluded_observations": 0,
            "all_resolved_holdouts_match": True,
        }

        observations = []
        for axis, resource, public, demands in (
            ("lut", "lut", 4_086_000, ((100_000, "fit"), (120_000, "fit"), (140_000, "holdout"), (140_000, "holdout"))),
            ("ff", "ff", 8_172_000, ((200_000, "fit"), (240_000, "fit"), (280_000, "holdout"), (280_000, "holdout"))),
        ):
            for index, (demand, role) in enumerate(demands):
                value = observation(
                    identifier=f"norm-{axis}-{index}",
                    kind="resource_capacity",
                    generator=f"ppro-blackbox-capacity-{axis}-v4",
                    role=role,
                    outcome="pass",
                    design={"requested_units": demand},
                    resource_demand={resource: demand},
                )
                value["metrics"]["fpga_utilization"] = [
                    {
                        "fpga": "F0",
                        "resources": {resource: math.ceil(demand / public * 100.0) / 100.0},
                    }
                ]
                observations.append(value)

        result = finalize_calibrated_capacity(
            prior=prior,
            boundary_fits=[boundary],
            normalization_observations=observations,
            utilization_limit_percent=75,
        )
        self.assertEqual(result["schema"], "emuflow.ppro-calibrated-capacity/v2")
        self.assertEqual(result["axes"]["lut"]["effective_resource_capacity"], 3_064_500)
        self.assertEqual(result["axes"]["ff"]["effective_resource_capacity"], 6_129_000)
        self.assertTrue(result["all_resolved_holdouts_match"])

    def test_finalize_capacity_rejects_wrong_report_normalization(self):
        root = Path(__file__).resolve().parents[1]
        prior = json.loads(
            (root / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text()
        )
        item = observation(
            identifier="bad-normalization",
            kind="resource_capacity",
            generator="ppro-blackbox-capacity-lut-v4",
            role="fit",
            outcome="pass",
            design={"requested_units": 100_000},
            resource_demand={"lut": 100_000},
        )
        item["metrics"]["fpga_utilization"] = [
            {"fpga": "F0", "resources": {"lut": 0.50}}
        ]
        with self.assertRaisesRegex(ValidationError, "requires repeated fit"):
            finalize_calibrated_capacity(
                prior=prior,
                boundary_fits=[],
                normalization_observations=[item],
                utilization_limit_percent=75,
            )

    def test_topology_fit_keeps_direction_and_does_not_invent_shared_groups(self):
        values = []
        for source, sink, outcome in ((0, 1, "pass"), (1, 0, "routing_infeasible")):
            for repeat in range(2):
                routes = (
                    [
                        {"id": "route0", "source": f"F{source}", "sinks": ["F2"], "effective_hops": 1, "signal_count": 32},
                        {"id": "route1", "source": "F2", "sinks": [f"F{sink}"], "effective_hops": 1, "signal_count": 32},
                        {"id": "control", "source": f"F{source}", "sinks": [f"F{sink}"], "effective_hops": 1, "signal_count": 2},
                    ]
                    if outcome == "pass"
                    else []
                )
                values.append(
                    observation(
                        identifier=f"topo-{source}-{sink}-{repeat}",
                        kind="topology_reachability",
                        generator="ppro-blackbox-topology-reachability-v2",
                        role="fit",
                        outcome=outcome,
                        design={
                            "source_fpga_index": source,
                            "sink_fpga_index": sink,
                            "probe_width_bits": 32,
                        },
                        routes=routes,
                    )
                )
        for repeat in range(2):
            values.append(
                observation(
                    identifier=f"topo-holdout-{repeat}",
                    kind="topology_reachability",
                    generator="ppro-blackbox-topology-reachability-v2",
                    role="holdout",
                    outcome="pass",
                    design={
                        "source_fpga_index": 0,
                        "sink_fpga_index": 1,
                        "probe_width_bits": 32,
                    },
                    routes=[
                        {"id": "route0", "source": "F0", "sinks": ["F2"], "effective_hops": 1, "signal_count": 32},
                        {"id": "route1", "source": "F2", "sinks": ["F1"], "effective_hops": 1, "signal_count": 32},
                    ],
                )
            )
        result = fit_effective_topology(values)
        self.assertEqual(result["directed_edges"][0]["state"], "reachable")
        self.assertEqual(result["directed_edges"][0]["effective_hops"], 2)
        self.assertEqual(result["probe_width_bits"], 32)
        self.assertEqual(result["directed_edges"][1]["state"], "unreachable")
        self.assertTrue(result["all_holdouts_match"])
        self.assertTrue(result["holdout_checks"][0]["matches"])
        self.assertEqual(result["shared_capacity_groups"]["status"], "not_identifiable")


if __name__ == "__main__":
    unittest.main()
