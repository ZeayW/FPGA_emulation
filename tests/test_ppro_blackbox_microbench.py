from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_microbench import (
    CAPACITY_AXES,
    generate_capacity_matrix,
    generate_capacity_probe_bundle,
)


REVISION = "8" * 64


class PProBlackboxMicrobenchTest(unittest.TestCase):
    def kwargs(self):
        return {
            "campaign_id": "capacity-stage3",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m1",
            "tool_release": "2026.1",
            "runner_revision": REVISION,
            "seed": 11,
        }

    def test_each_axis_generates_connected_compact_rtl(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for axis in sorted(CAPACITY_AXES):
                bundle = generate_capacity_probe_bundle(
                    root / axis,
                    axis=axis,
                    units=4,
                    repeat=0,
                    role="fit",
                    **self.kwargs(),
                )
                rtl = bundle.rtl_path.read_text(encoding="utf-8")
                self.assertIn("module ppro_blackbox_capacity_probe", rtl)
                self.assertIn("calibration_capacity_payload P0", rtl)
                self.assertIn("digest =", rtl)
                self.assertLess(len(rtl), 8192)
                self.assertEqual(
                    bundle.run_spec["workload"]["design_metrics"]["requested_units"],
                    4.0,
                )
                self.assertEqual(bundle.run_spec["identity"]["role"], "fit")
                self.assertEqual(
                    bundle.run_spec["experiment"]["documented_actions"],
                    ["partition_constraint"],
                )

    def test_hard_resource_axes_use_public_inference_attributes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bram = generate_capacity_probe_bundle(
                root / "bram", axis="bram", units=2, repeat=0, role="fit", **self.kwargs()
            ).rtl_path.read_text(encoding="utf-8")
            uram = generate_capacity_probe_bundle(
                root / "uram", axis="uram", units=2, repeat=0, role="fit", **self.kwargs()
            ).rtl_path.read_text(encoding="utf-8")
            dsp = generate_capacity_probe_bundle(
                root / "dsp", axis="dsp", units=2, repeat=0, role="fit", **self.kwargs()
            ).rtl_path.read_text(encoding="utf-8")
            self.assertIn('ram_style = "block"', bram)
            self.assertIn('ram_style = "ultra"', uram)
            self.assertIn('use_dsp = "yes"', dsp)
            self.assertIn("calibration_capacity_bram_cell", bram)
            self.assertIn("calibration_capacity_uram_cell", uram)
            self.assertIn('keep_hierarchy = "yes"', bram)

    def test_large_lut_probe_is_hierarchically_tiled_below_elaboration_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = generate_capacity_probe_bundle(
                Path(raw),
                axis="lut",
                units=150000,
                repeat=0,
                role="fit",
                **self.kwargs(),
            )
            rtl = bundle.rtl_path.read_text(encoding="utf-8")
            self.assertIn("calibration_capacity_lut_tile", rtl)
            self.assertIn("g_lut_tile_36", rtl)
            self.assertNotIn("gi < 150000", rtl)
            self.assertIn("gi < COUNT", rtl)
            tile_limits = [int(value) for value in re.findall(r"\.COUNT\(([0-9]+)\)", rtl)]
            self.assertEqual(len(tile_limits), 37)
            self.assertTrue(all(value <= 4096 for value in tile_limits))
            self.assertLess(len(rtl), 32768)

    def test_soft_capacity_probe_rejects_multi_million_boundary_search(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(
                ValidationError, "must not be reverse engineered"
            ):
                generate_capacity_probe_bundle(
                    Path(raw) / "too-large",
                    axis="lut",
                    units=3_000_000,
                    repeat=0,
                    role="fit",
                    **self.kwargs(),
                )

    def test_matrix_separates_fit_and_holdout_points(self):
        with tempfile.TemporaryDirectory() as raw:
            kwargs = self.kwargs()
            kwargs.pop("seed")
            bundles = generate_capacity_matrix(
                Path(raw),
                axes=["lut", "ff"],
                fit_units=[8, 16],
                holdout_units=[12],
                repeats=2,
                **kwargs,
            )
            self.assertEqual(len(bundles), 12)
            roles = [bundle.run_spec["identity"]["role"] for bundle in bundles]
            self.assertEqual(roles.count("fit"), 8)
            self.assertEqual(roles.count("holdout"), 4)
            case_ids = [bundle.run_spec["identity"]["case_id"] for bundle in bundles]
            self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_overlapping_fit_and_holdout_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            kwargs = self.kwargs()
            kwargs.pop("seed")
            with self.assertRaisesRegex(ValidationError, "disjoint"):
                generate_capacity_matrix(
                    Path(raw),
                    axes=["lut"],
                    fit_units=[8],
                    holdout_units=[8],
                    repeats=1,
                    **kwargs,
                )


if __name__ == "__main__":
    unittest.main()
