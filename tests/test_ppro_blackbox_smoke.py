from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_smoke import generate_connected_smoke_bundle


REVISION = "9" * 64


class PProBlackboxSmokeTest(unittest.TestCase):
    def generate(self, root: Path):
        return generate_connected_smoke_bundle(
            root,
            campaign_id="stage2-smoke",
            case_id="connected-smoke-w32-d8",
            public_prior_id="lx2-public-prior-v1",
            configuration_id="lx2-m1",
            tool_release="2026.1",
            runner_revision=REVISION,
        )

    def test_bundle_is_deterministic_and_provider_neutral(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bundle"
            first = self.generate(root)
            first_bytes = {
                path.name: path.read_bytes()
                for path in (
                    first.rtl_path,
                    first.filelist_path,
                    first.parameters_path,
                    first.constraints_path,
                    first.run_spec_path,
                )
            }
            second = self.generate(root)
            second_bytes = {
                path.name: path.read_bytes()
                for path in (
                    second.rtl_path,
                    second.filelist_path,
                    second.parameters_path,
                    second.constraints_path,
                    second.run_spec_path,
                )
            }
            self.assertEqual(first_bytes, second_bytes)
            serialized = first.run_spec_path.read_text(encoding="utf-8").lower()
            for forbidden in (
                "/data/",
                "/research/",
                "boarddb",
                "stf",
                "license",
                "command",
                "report_paths",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_c0_smoke_is_not_fitting_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.generate(Path(raw))
            spec = json.loads(bundle.run_spec_path.read_text(encoding="utf-8"))
            self.assertEqual(spec["identity"]["role"], "holdout")
            self.assertEqual(spec["experiment"]["control_mode"], "none")
            self.assertEqual(spec["experiment"]["documented_actions"], [])
            self.assertEqual(spec["execution"]["seed"], 1)

    def test_invalid_dimensions_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValidationError, "width"):
                generate_connected_smoke_bundle(
                    Path(raw),
                    campaign_id="stage2-smoke",
                    case_id="invalid",
                    public_prior_id="lx2-public-prior-v1",
                    configuration_id="lx2-m1",
                    tool_release="2026.1",
                    runner_revision=REVISION,
                    width=1,
                )


if __name__ == "__main__":
    unittest.main()
