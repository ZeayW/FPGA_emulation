from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_topology import (
    generate_ordered_pair_matrix,
    generate_topology_probe_bundle,
)


REVISION = "7" * 64


class PProBlackboxTopologyTest(unittest.TestCase):
    def kwargs(self):
        return {
            "fpga_count": 4,
            "width": 1,
            "pipeline_stages": 4,
            "campaign_id": "topology-stage3",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m2",
            "tool_release": "2026.1",
            "runner_revision": REVISION,
        }

    def test_probe_is_connected_and_uses_logical_aliases_only(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = generate_topology_probe_bundle(
                Path(raw),
                source_index=0,
                sink_index=3,
                repeat=0,
                role="fit",
                seed=1,
                **self.kwargs(),
            )
            rtl = bundle.rtl_path.read_text(encoding="utf-8")
            self.assertIn("calibration_producer P0", rtl)
            self.assertIn("calibration_consumer P1", rtl)
            self.assertIn("transported_payload", rtl)
            constraints = bundle.constraints_path.read_text(encoding="utf-8")
            self.assertIn('"target":"F0"', constraints)
            self.assertIn('"target":"F3"', constraints)
            self.assertNotIn('"communication"', constraints)
            self.assertNotIn("B1.", constraints)
            self.assertEqual(
                bundle.run_spec["experiment"]["control_mode"], "fixed_assignment"
            )

    def test_matrix_fits_every_direction_and_repeats_named_holdouts(self):
        with tempfile.TemporaryDirectory() as raw:
            bundles = generate_ordered_pair_matrix(
                Path(raw),
                holdout_pairs=[(0, 3), (3, 0)],
                repeats=2,
                **self.kwargs(),
            )
            self.assertEqual(len(bundles), 28)
            fits = [bundle for bundle in bundles if bundle.run_spec["identity"]["role"] == "fit"]
            self.assertEqual(len(fits), 24)
            holdouts = [bundle for bundle in bundles if bundle.run_spec["identity"]["role"] == "holdout"]
            self.assertEqual(len(holdouts), 4)
            self.assertTrue(all("f0-f3" in bundle.root.as_posix() or "f3-f0" in bundle.root.as_posix() for bundle in holdouts))
            self.assertTrue(all("/holdout/" in bundle.root.as_posix() for bundle in holdouts))

    def test_same_endpoint_and_missing_holdout_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValidationError, "different source"):
                generate_topology_probe_bundle(
                    Path(raw) / "same",
                    source_index=1,
                    sink_index=1,
                    repeat=0,
                    role="fit",
                    seed=1,
                    **self.kwargs(),
                )
            with self.assertRaisesRegex(ValidationError, "holdout"):
                generate_ordered_pair_matrix(
                    Path(raw) / "matrix",
                    holdout_pairs=[],
                    repeats=1,
                    **self.kwargs(),
                )


if __name__ == "__main__":
    unittest.main()
