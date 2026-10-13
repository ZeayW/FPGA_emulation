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
from emuflow.ppro_blackbox_details import parse_calibration_details


ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json"
FIXTURES = ROOT / "tests/fixtures/ppro_blackbox"
ROUTE_TIMING = """Critical Path Report (Top 10 records will be listed at most)
Startpoint: FPGA_1_ins/P0/payload
Endpoint: FPGA_2_ins/P1/received_payload
Delay(ns) Time(ns) Die TDM_ratio pin_name
0.0 0.0 FPGA_1 1 payload_buf/Z (BUF_X1)
7.0 7.0 FPGA_2 1 payload_buf/Z (BUF_X1)
0.1 7.1 FPGA_2 P1/received_payload
7.1 data arrival time ( normalized delay 7.1 )
"""


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

    def test_v5_uses_route_timing_without_post_partition_ssta(self):
        value = copy.deepcopy(self.passing)
        value["workload"]["generator_id"] = "ppro-blackbox-communication-probe-v5"
        value["reports"].update(system_timing=False, route_timing=True, net_tdm=True)
        value["metrics"]["calibration_details"] = parse_calibration_details(
            "", "", {"F1": "F0", "F2": "F1"}, route_timing_text=ROUTE_TIMING,
            report_sha256={"route_timing": "a" * 64, "net_tdm": "b" * 64},
        )
        normalized = validate_blackbox_observation(value)
        self.assertFalse(normalized["reports"]["system_timing"])
        self.assertTrue(normalized["reports"]["route_timing"])
        self.assertEqual(normalized, validate_blackbox_observation(normalized))
        for missing in ("route_timing", "net_tdm"):
            for change in ("absent", "false"):
                with self.subTest(missing=missing, change=change):
                    bad = copy.deepcopy(value)
                    if change == "absent":
                        del bad["reports"][missing]
                    else:
                        bad["reports"][missing] = False
                    with self.assertRaisesRegex(ValidationError, "sr0 timing evidence"):
                        validate_blackbox_observation(bad)
        value["metrics"]["timing"] = {}
        with self.assertRaisesRegex(ValidationError, "sr0 timing evidence"):
            validate_blackbox_observation(value)

    def test_v5_requires_both_route_report_hashes(self):
        value = copy.deepcopy(self.passing)
        value["workload"]["generator_id"] = "ppro-blackbox-communication-probe-v5"
        value["reports"].update(system_timing=False, route_timing=True, net_tdm=True)
        with self.assertRaisesRegex(ValidationError, "report identities"):
            validate_blackbox_observation(value)
        for retained_role in ("route_timing", "net_tdm"):
            value["metrics"]["calibration_details"] = parse_calibration_details(
                "", "", {"F1": "F0"}, report_sha256={retained_role: "a" * 64},
            )
            with self.assertRaisesRegex(ValidationError, "report identities"):
                validate_blackbox_observation(value)
        value["metrics"]["calibration_details"] = parse_calibration_details(
            "", "", {"F1": "F0"},
            report_sha256={"route_timing": "a" * 64, "net_tdm": "b" * 64},
        )
        with self.assertRaisesRegex(ValidationError, "route timing path evidence"):
            validate_blackbox_observation(value)

    def test_historical_communication_timing_contract_is_not_relaxed(self):
        for version in ("v3", "v4", "v5-mock"):
            value = copy.deepcopy(self.passing)
            value["workload"]["generator_id"] = f"ppro-blackbox-communication-probe-{version}"
            self.assertEqual(set(validate_blackbox_observation(value)["reports"]), set(value["reports"]))
            value["reports"].update(system_timing=False, route_timing=True, net_tdm=True)
            with self.assertRaisesRegex(ValidationError, "sr0 timing evidence"):
                validate_blackbox_observation(value)

    def test_optional_reports_do_not_remove_required_base_keys_or_allow_unknown_roles(self):
        for key in ("system_timing", "resource_summary", "partition_summary", "route_summary"):
            value = copy.deepcopy(self.passing)
            del value["reports"][key]
            value["reports"].update(route_timing=True, net_tdm=True)
            with self.assertRaisesRegex(ValidationError, "report-presence map"):
                validate_blackbox_observation(value)
        value = copy.deepcopy(self.passing)
        value["reports"]["private_platform_data"] = True
        with self.assertRaisesRegex(ValidationError, "report-presence map"):
            validate_blackbox_observation(value)

    def test_application_holdout_preserves_route_evidence_without_timing_metric(self):
        value = copy.deepcopy(self.passing)
        value["identity"]["role"] = "holdout"
        value["experiment"]["kind"] = "application_holdout"
        value["experiment"]["control_mode"] = "free_optimization"
        value["metrics"]["assignments"] = [
            {"partition": "P0", "fpga": "F0"},
            {"partition": "P1", "fpga": "F1"},
        ]
        value["metrics"]["timing"] = {}
        value["derived"] = {"fit_eligible": False, "reason": "holdout-not-fit"}
        normalized = validate_blackbox_observation(value)
        self.assertEqual(normalized["metrics"]["timing"], {})
        self.assertFalse(normalized["derived"]["fit_eligible"])

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
