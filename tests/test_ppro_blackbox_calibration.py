from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_calibration import (
    validate_blackbox_observation,
    validate_public_platform_prior,
    validate_redacted_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json"
FIXTURES = ROOT / "tests/fixtures/ppro_blackbox"


def _load(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


class PProBlackboxCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.prior = _load(PRIOR)
        self.passing = _load(FIXTURES / "latency-pass-observation-v1.json")
        self.license_failure = _load(
            FIXTURES / "license-failure-observation-v1.json"
        )

    def test_public_prior_is_deterministic_and_does_not_claim_boarddb(self):
        normalized = validate_public_platform_prior(self.prior)
        self.assertEqual(normalized, validate_public_platform_prior(normalized))
        self.assertEqual(
            normalized["prior"]["claim_scope"],
            "published-upper-bounds-not-boarddb",
        )
        self.assertEqual(
            [item["fpga_count"] for item in normalized["configurations"]],
            [2, 4, 6, 8],
        )
        self.assertIn("effective-topology", normalized["not_identifiable"])

    def test_normal_observation_round_trip_and_fit_gate(self):
        normalized = validate_blackbox_observation(self.passing)
        self.assertEqual(normalized, validate_blackbox_observation(normalized))
        self.assertTrue(normalized["derived"]["fit_eligible"])
        self.assertEqual(
            normalized["metrics"]["timing"]["sr0_worst_cross_fpga_delay_ns"],
            12.5,
        )
        self.assertEqual(normalized, validate_redacted_artifact(normalized))

    def test_license_failure_is_not_hardware_evidence(self):
        normalized = validate_blackbox_observation(self.license_failure)
        self.assertFalse(normalized["derived"]["fit_eligible"])
        contaminated = copy.deepcopy(self.license_failure)
        contaminated["reports"]["resource_summary"] = True
        contaminated["metrics"]["design"] = {"instances": 1}
        with self.assertRaisesRegex(ValidationError, "cannot carry hardware metrics"):
            validate_blackbox_observation(contaminated)

    def test_free_optimization_cannot_be_relabelled_as_fit_evidence(self):
        value = copy.deepcopy(self.passing)
        value["experiment"]["control_mode"] = "free_optimization"
        with self.assertRaisesRegex(ValidationError, "fit eligibility"):
            validate_blackbox_observation(value)

    def test_public_prior_rejects_assumed_value_as_public_fact(self):
        value = copy.deepcopy(self.prior)
        value["device"]["resources"][0]["provenance"] = {
            "class": "research_assumption"
        }
        with self.assertRaisesRegex(ValidationError, "provenance claim"):
            validate_public_platform_prior(value)

    def test_redaction_rejects_paths_raw_reports_and_private_endpoints(self):
        path_value = copy.deepcopy(self.passing)
        path_value["tool"]["release"] = "/data/private/PPro/current"
        with self.assertRaisesRegex(ValidationError, "sensitive path"):
            validate_blackbox_observation(path_value)

        raw_value = copy.deepcopy(self.passing)
        raw_value["metrics"]["raw_report"] = "ordinary report body"
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            validate_blackbox_observation(raw_value)

        endpoint_value = copy.deepcopy(self.passing)
        endpoint_value["provenance"]["note"] = "license@10.0.0.8"
        with self.assertRaisesRegex(ValidationError, "sensitive path"):
            validate_blackbox_observation(endpoint_value)

    def test_prior_sources_are_official_https_only(self):
        value = copy.deepcopy(self.prior)
        value["sources"][0]["uri"] = "https://example.invalid/private"
        with self.assertRaisesRegex(ValidationError, "official HTTPS"):
            validate_public_platform_prior(value)

    def test_undocumented_command_text_has_no_schema_slot(self):
        value = copy.deepcopy(self.passing)
        value["experiment"]["command"] = "internal_tool --dump-boarddb"
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            validate_blackbox_observation(value)


if __name__ == "__main__":
    unittest.main()
