from __future__ import annotations

import math
import unittest

from emuflow.ppro_blackbox_stage4 import (
    fit_latency_model,
    fit_payload_intervals,
    fit_transport_cost_model,
)


def communication_observation(
    *,
    identifier: str,
    kind: str,
    role: str,
    outcome: str,
    width: int,
    flows: int = 1,
    fanout: int = 1,
    ratio: int = 1,
    hops: int = 1,
    delay: float = 10.0,
):
    failure = None if outcome == "pass" else "link-capacity-boundary"
    routes = (
        [{"id": "forward", "source": "F0", "sinks": ["F1"], "effective_hops": hops, "signal_count": 1}]
        if outcome == "pass"
        else []
    )
    return {
        "schema": "emuflow.ppro-blackbox-observation/v1",
        "identity": {
            "id": identifier,
            "campaign_id": "stage4-test",
            "case_id": identifier,
            "role": role,
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m2",
        },
        "tool": {"name": "PPro mock", "release": "mock", "runner_revision": "1" * 64},
        "workload": {
            "generator_id": "ppro-blackbox-communication-probe-v2",
            "generator_revision": "2" * 64,
            "rtl_sha256": "3" * 64,
            "parameters_sha256": "4" * 64,
            "top_module": "stage4_probe",
        },
        "experiment": {
            "kind": kind,
            "control_mode": "fixed_communication",
            "documented_actions": ["net_route_constraint", "partition_constraint", "random_seed"],
            "constraints_sha256": "5" * 64,
        },
        "execution": {"seed": 1, "outcome": outcome, "failure_code": failure, "runtime_seconds": 1.0},
        "reports": {
            "partition_summary": outcome == "pass",
            "resource_summary": outcome == "pass",
            "route_summary": outcome == "pass",
            "system_timing": outcome == "pass",
        },
        "metrics": {
            "design": {
                "bidirectional": 0,
                "endpoint_count": 1 + fanout,
                "fanout": fanout,
                "flow_count": flows,
                "forced_tdm_ratio": 0,
                "local_baseline": 0,
                "probe_width_bits": width,
                "repeat_index": 0,
                "sink_fpga_index": 1,
                "source_fpga_index": 0,
            },
            "resource_demand": {"lut": 10} if outcome == "pass" else {},
            "fpga_utilization": [{"fpga": "F0", "resources": {"lut": 0.1}}] if outcome == "pass" else [],
            "assignments": [{"partition": "P0", "fpga": "F0"}] if outcome == "pass" else [],
            "routes": routes,
            "communication": {"maximum_tdm_ratio": ratio} if outcome == "pass" else {},
            "timing": {"sr0_worst_cross_fpga_delay_ns": delay} if outcome == "pass" else {},
        },
        "provenance": {"class": "black_box_observation"},
        "derived": {
            "fit_eligible": role == "fit",
            "reason": "controlled-evaluated-observation" if role == "fit" else "holdout-not-fit",
        },
    }


class PProBlackboxStage4Test(unittest.TestCase):
    def test_payload_interval_and_tdm_transition(self):
        values = []
        for width, outcome, role, ratio in (
            (64, "pass", "fit", 1),
            (128, "pass", "fit", 2),
            (256, "link_capacity_infeasible", "fit", 0),
            (192, "pass", "holdout", 3),
        ):
            for repeat in range(2):
                values.append(
                    communication_observation(
                        identifier=f"payload-{width}-{repeat}",
                        kind="payload_capacity",
                        role=role,
                        outcome=outcome,
                        width=width,
                        ratio=ratio,
                    )
                )
        result = fit_payload_intervals(values)
        link = result["link_signatures"][0]
        self.assertEqual(link["lower_successful_width_bits"], 128)
        self.assertEqual(link["upper_infeasible_width_bits"], 256)
        self.assertEqual(link["observed_tdm_levels"][-1]["maximum_tdm_ratio"], 2)
        self.assertTrue(result["all_resolved_holdouts_match"])

    def test_latency_fit_recovers_aggregate_model_and_holdout(self):
        values = []
        points = [
            (16, 1, 1, 1),
            (32, 2, 1, 1),
            (64, 1, 2, 1),
            (96, 2, 3, 1),
            (128, 3, 2, 2),
            (160, 1, 4, 2),
            (192, 2, 3, 3),
            (224, 3, 4, 3),
            (80, 2, 2, 2),
        ]
        for index, (width, hops, ratio, flows) in enumerate(points):
            fanout = 1 + (index % 3)
            delay = 5 + 2 * hops + math.ceil(width * flows / 32) + 3 * (ratio - 1) + 4 * (flows - 1) + 0.5 * (fanout - 1)
            values.append(
                communication_observation(
                    identifier=f"latency-{index}",
                    kind="latency",
                    role="holdout" if index == len(points) - 1 else "fit",
                    outcome="pass",
                    width=width,
                    flows=flows,
                    fanout=fanout,
                    ratio=ratio,
                    hops=hops,
                    delay=delay,
                )
            )
        result = fit_latency_model(values, payload_bits_candidates=[16, 32, 64], bootstrap_samples=32)
        self.assertEqual(result["payload_bits_per_cycle"], 32)
        self.assertLess(result["fit_rmse_ns"], 1e-5)
        self.assertLess(result["holdout_max_relative_error"], 1e-5)
        self.assertAlmostEqual(result["parameters"]["per_hop_ns"]["nominal"], 2.0, places=4)

    def test_transport_cost_uses_same_rtl_local_cross_pairs(self):
        values = []
        points = [
            (16, 1, 1, 1),
            (32, 2, 1, 1),
            (64, 1, 2, 1),
            (96, 2, 3, 1),
            (128, 3, 2, 2),
            (192, 2, 4, 3),
        ]
        for index, (width, flows, ratio, fanout) in enumerate(points):
            role = "holdout" if index == len(points) - 1 else "fit"
            base = communication_observation(
                identifier=f"cost-local-{index}",
                kind="transport_cost",
                role=role,
                outcome="pass",
                width=width,
                flows=flows,
                fanout=fanout,
                ratio=1,
            )
            cross = communication_observation(
                identifier=f"cost-cross-{index}",
                kind="transport_cost",
                role=role,
                outcome="pass",
                width=width,
                flows=flows,
                fanout=fanout,
                ratio=ratio,
            )
            shared_hash = f"{index + 10:064x}"
            base["workload"]["rtl_sha256"] = shared_hash
            cross["workload"]["rtl_sha256"] = shared_hash
            for item, local in ((base, 1), (cross, 0)):
                item["metrics"]["design"]["local_baseline"] = local
                item["metrics"]["design"]["repeat_index"] = 0
            base["experiment"]["control_mode"] = "fixed_assignment"
            base["experiment"]["documented_actions"] = [
                "partition_constraint",
                "random_seed",
            ]
            transported_bits = width * flows
            delta = (
                2 * (1 + fanout)
                + 0.25 * transported_bits
                + 3 * (ratio - 1)
                + 5 * (fanout - 1)
            )
            base["metrics"]["resource_demand"] = {"lut": 100}
            cross["metrics"]["resource_demand"] = {"lut": 100 + delta}
            values.extend([base, cross])
        result = fit_transport_cost_model(values, bootstrap_samples=32)
        lut = result["resources"]["lut"]["parameters"]
        self.assertAlmostEqual(lut["per_endpoint"]["nominal"], 2.0, places=3)
        self.assertAlmostEqual(lut["per_transport_bit"]["nominal"], 0.25, places=3)
        self.assertEqual(result["paired_fit_samples"], 5)
        self.assertEqual(len(result["holdout_checks"]), 1)


if __name__ == "__main__":
    unittest.main()
