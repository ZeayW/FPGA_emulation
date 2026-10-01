from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.ppro_blackbox_communication import (
    generate_communication_matrix,
    generate_communication_probe_bundle,
)


REVISION = "6" * 64


class PProBlackboxCommunicationTest(unittest.TestCase):
    def test_transport_matrix_pairs_local_and_cross_with_disjoint_holdout(self):
        with tempfile.TemporaryDirectory() as raw:
            bundles = generate_communication_matrix(
                Path(raw),
                kind="transport_cost",
                fpga_count=4,
                source_index=0,
                sink_indices=[1],
                fit_widths=[32, 64],
                holdout_widths=[48],
                flow_counts=[1, 2],
                bidirectional=False,
                repeats=2,
                campaign_id="transport-matrix",
                public_prior_id="prior-v1",
                configuration_id="platform-v1",
                tool_release="2026.1",
                runner_revision="a" * 64,
                seed_base=11,
            )
            self.assertEqual(len(bundles), 24)
            roles = [bundle.run_spec["identity"]["role"] for bundle in bundles]
            self.assertEqual(roles.count("fit"), 16)
            self.assertEqual(roles.count("holdout"), 8)
            local = [
                bundle
                for bundle in bundles
                if bundle.run_spec["workload"]["design_metrics"]["local_baseline"] == 1
            ]
            self.assertEqual(len(local), 12)
            for bundle in local:
                point = bundle.root.parent
                cross = point / "cross" / "run-spec.json"
                self.assertTrue(cross.is_file())
                self.assertEqual(
                    bundle.run_spec["execution"]["seed"],
                    read_json(cross)["execution"]["seed"],
                )
                self.assertEqual(
                    bundle.run_spec["workload"]["design_metrics"]["pairing_token"],
                    read_json(cross)["workload"]["design_metrics"]["pairing_token"],
                )

    def test_transport_matrix_preserves_multicast_fanout_in_local_pair(self):
        with tempfile.TemporaryDirectory() as raw:
            bundles = generate_communication_matrix(
                Path(raw),
                kind="transport_cost",
                fpga_count=4,
                source_index=0,
                sink_indices=[1, 2],
                fit_widths=[32],
                holdout_widths=[48],
                flow_counts=[1],
                bidirectional=False,
                repeats=2,
                campaign_id="transport-fanout",
                public_prior_id="prior-v1",
                configuration_id="platform-v1",
                tool_release="2026.1",
                runner_revision="a" * 64,
                seed_base=11,
            )
            local = next(
                bundle
                for bundle in bundles
                if bundle.run_spec["workload"]["design_metrics"]["local_baseline"] == 1
            )
            self.assertEqual(local.run_spec["workload"]["design_metrics"]["fanout"], 2)
            constraints = read_json(local.constraints_path)
            self.assertEqual(
                [item["target"] for item in constraints["assignments"]],
                ["F0", "F0", "F0"],
            )

    def test_communication_matrix_rejects_overlapping_fit_and_holdout(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValidationError, "must be disjoint"):
                generate_communication_matrix(
                    Path(raw),
                    kind="latency",
                    fpga_count=2,
                    source_index=0,
                    sink_indices=[1],
                    fit_widths=[32],
                    holdout_widths=[32],
                    flow_counts=[1],
                    bidirectional=False,
                    repeats=2,
                    campaign_id="bad",
                    public_prior_id="prior-v1",
                    configuration_id="platform-v1",
                    tool_release="2026.1",
                    runner_revision="a" * 64,
                    seed_base=1,
                )

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
            self.assertIn('"partition":"P0"', constraints)
            self.assertNotIn('"communication"', constraints)
            self.assertNotIn("B1.", constraints)
            self.assertEqual(
                bundle.run_spec["experiment"]["control_mode"], "fixed_assignment"
            )

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

    def test_real_profile_rejects_unimplemented_forced_tdm_constraint(self):
        with tempfile.TemporaryDirectory() as raw:
            values = self.kwargs()
            values["adapter_profile"] = "ppro-2026-ordinary-reports-v1"
            with self.assertRaisesRegex(ValidationError, "documented provider"):
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
