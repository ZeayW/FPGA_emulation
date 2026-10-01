from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_communication import generate_communication_probe_bundle


REVISION = "6" * 64


class PProBlackboxCommunicationTest(unittest.TestCase):
    def kwargs(self):
        return {
            "kind": "latency",
            "fpga_count": 4,
            "source_index": 0,
            "sink_indices": [1, 2],
            "width": 64,
            "flow_count": 2,
            "bidirectional": True,
            "local_baseline": False,
            "forced_tdm_ratio": 4,
            "repeat": 0,
            "role": "fit",
            "campaign_id": "communication-stage4",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m2",
            "tool_release": "2026.1",
            "runner_revision": REVISION,
            "seed": 1,
        }

    def test_probe_exposes_control_dimensions_and_logical_routes(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = generate_communication_probe_bundle(Path(raw), **self.kwargs())
            metrics = bundle.run_spec["workload"]["design_metrics"]
            self.assertEqual(metrics["probe_width_bits"], 64.0)
            self.assertEqual(metrics["fanout"], 2.0)
            self.assertEqual(metrics["bidirectional"], 1.0)
            self.assertIn(
                "tdm_ratio_constraint",
                bundle.run_spec["experiment"]["documented_actions"],
            )
            constraints = bundle.constraints_path.read_text(encoding="utf-8")
            self.assertIn('"route":"forward"', constraints)
            self.assertIn('"route":"reverse0"', constraints)
            self.assertNotIn("B1.", constraints)

    def test_zero_tdm_does_not_claim_constraint(self):
        with tempfile.TemporaryDirectory() as raw:
            values = self.kwargs()
            values["forced_tdm_ratio"] = 0
            bundle = generate_communication_probe_bundle(Path(raw), **values)
            self.assertNotIn(
                "tdm_ratio_constraint",
                bundle.run_spec["experiment"]["documented_actions"],
            )

    def test_invalid_endpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            values = self.kwargs()
            values["sink_indices"] = [0]
            with self.assertRaisesRegex(ValidationError, "distinct"):
                generate_communication_probe_bundle(Path(raw), **values)

    def test_transport_local_baseline_uses_assignment_only(self):
        with tempfile.TemporaryDirectory() as raw:
            values = self.kwargs()
            values.update(
                kind="transport_cost",
                sink_indices=[0],
                local_baseline=True,
                forced_tdm_ratio=0,
                bidirectional=False,
            )
            bundle = generate_communication_probe_bundle(Path(raw), **values)
            self.assertEqual(bundle.run_spec["experiment"]["control_mode"], "fixed_assignment")
            self.assertNotIn(
                "net_route_constraint", bundle.run_spec["experiment"]["documented_actions"]
            )


if __name__ == "__main__":
    unittest.main()
