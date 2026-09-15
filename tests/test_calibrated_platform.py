import copy
import unittest

from emuflow.calibrated_platform import (
    fit_calibrated_platform,
    materialize_calibrated_boarddb,
    validate_calibrated_platform_holdout,
)
from emuflow.errors import ValidationError


def template():
    return {
        "schema": "emuflow.calibrated-platform-template/v1",
        "model": {
            "name": "synthetic_calibrated_reference",
            "description": "Unit-test-only behavior model",
            "reference_alias": "synthetic-reference",
            "source_class": "synthetic_fixture",
            "authorization_id": "unit-test-public-fixture",
            "publication_scope": "public",
        },
        "device": {
            "part": "academic-calibrated-device",
            "utilization_limit": 0.75,
        },
        "link": {
            "direction": "full_duplex",
            "capacity_sharing": "per_direction",
            "fabric_clock_mhz": 250.0,
        },
        "configurations": [
            {
                "id": "2fpga-p2p",
                "fpgas": ["F0", "F1"],
                "links": [{"id": "L01", "endpoints": ["F0", "F1"]}],
            },
            {
                "id": "4fpga-ring",
                "fpgas": ["F0", "F1", "F2", "F3"],
                "links": [
                    {"id": "L01", "endpoints": ["F0", "F1"]},
                    {"id": "L12", "endpoints": ["F1", "F2"]},
                    {"id": "L23", "endpoints": ["F2", "F3"]},
                    {"id": "L30", "endpoints": ["F3", "F0"]},
                ],
            },
        ],
        "acceptance": {
            "capacity_outcome_accuracy_min": 1.0,
            "link_outcome_accuracy_min": 1.0,
            "delay_mean_relative_error_max": 0.10,
            "delay_max_relative_error_max": 0.15,
        },
    }


def dataset(role="fit"):
    prefix = "fit" if role == "fit" else "holdout"
    result = {
        "schema": "emuflow.platform-calibration-observations/v1",
        "dataset": {
            "id": f"synthetic-{prefix}",
            "role": role,
            "reference_alias": "synthetic-reference",
            "source_class": "synthetic_fixture",
            "authorization_id": "unit-test-public-fixture",
            "publication_scope": "public",
        },
        "capacity_boundaries": [],
        "link_capacity_boundaries": [],
        "link_delay_measurements": [],
    }
    if role == "fit":
        result["capacity_boundaries"] = [
            {
                "id": "fit-lut-pass",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "demand_per_fpga": 740,
                "outcome": "pass",
                "assignment_control": "fixed",
            },
            {
                "id": "fit-lut-fail",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "demand_per_fpga": 761,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
            },
            {
                "id": "fit-ff-pass",
                "configuration": "4fpga-ring",
                "resource": "ff",
                "demand_per_fpga": 1500,
                "outcome": "pass",
                "assignment_control": "fixed",
            },
            {
                "id": "fit-ff-fail",
                "configuration": "4fpga-ring",
                "resource": "ff",
                "demand_per_fpga": 1601,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
            },
        ]
        result["link_capacity_boundaries"] = [
            {
                "id": "fit-link-pass",
                "configuration": "2fpga-p2p",
                "hop_count": 1,
                "offered_bits_per_cycle": 32,
                "outcome": "pass",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
            {
                "id": "fit-link-fail",
                "configuration": "2fpga-p2p",
                "hop_count": 1,
                "offered_bits_per_cycle": 34,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
        ]
        # Ground truth: endpoint=2ns, hop=3ns, TDM-ratio step=4ns,
        # contention=0.5ns/unit; the 250 MHz slot is 4ns for serialization.
        result["link_delay_measurements"] = [
            delay("fit-delay-base", 1, 16, 0, 0, 5.0),
            delay("fit-delay-hop", 2, 16, 0, 0, 8.0),
            delay("fit-delay-serialize", 1, 64, 0, 0, 9.0),
            delay("fit-delay-tdm", 1, 16, 2, 0, 13.0),
            delay("fit-delay-contention", 1, 16, 0, 4, 7.0),
        ]
    else:
        result["capacity_boundaries"] = [
            {
                "id": "holdout-lut-pass",
                "configuration": "4fpga-ring",
                "resource": "lut",
                "demand_per_fpga": 749,
                "outcome": "pass",
                "assignment_control": "fixed",
            },
            {
                "id": "holdout-lut-fail",
                "configuration": "4fpga-ring",
                "resource": "lut",
                "demand_per_fpga": 751,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
            },
        ]
        result["link_capacity_boundaries"] = [
            {
                "id": "holdout-link-pass",
                "configuration": "4fpga-ring",
                "hop_count": 1,
                "offered_bits_per_cycle": 32,
                "outcome": "pass",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
            {
                "id": "holdout-link-fail",
                "configuration": "4fpga-ring",
                "hop_count": 1,
                "offered_bits_per_cycle": 33,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
        ]
        result["link_delay_measurements"] = [
            delay("holdout-delay", 3, 96, 1, 2, 24.0)
        ]
    return result


def delay(identifier, hops, payload, tdm_pressure, contention, observed):
    return {
        "id": identifier,
        "configuration": "4fpga-ring" if hops > 1 else "2fpga-p2p",
        "hop_count": hops,
        "payload_bits": payload,
        "max_tdm_ratio": tdm_pressure + 1,
        "contention_units": contention,
        "observed_delay_ns": observed,
        "assignment_control": "fixed",
        "route_control": "fixed",
    }


class CalibratedPlatformTest(unittest.TestCase):
    def test_fit_recovers_controlled_capacity_and_delay(self):
        model = fit_calibrated_platform(template(), dataset())
        self.assertEqual(
            model["calibration"]["resource_effective_capacity_intervals"]["lut"],
            {"effective_lower": 740, "effective_upper_exclusive": 761},
        )
        self.assertEqual(model["profiles"]["nominal"]["device_capacity"]["lut"], 1000)
        self.assertEqual(
            model["profiles"]["nominal"]["link_payload_bits_per_cycle_per_direction"],
            32,
        )
        delay_model = model["calibration"]["link_delay_model"]
        self.assertAlmostEqual(delay_model["endpoint_ns"], 2.0, places=7)
        self.assertAlmostEqual(delay_model["per_hop_ns"], 3.0, places=7)
        self.assertAlmostEqual(
            delay_model["per_tdm_ratio_step_ns"], 4.0, places=7
        )
        self.assertAlmostEqual(delay_model["contention_ns"], 0.5, places=7)

    def test_materializes_only_declared_platform_configurations(self):
        model = fit_calibrated_platform(template(), dataset())
        boarddb = materialize_calibrated_boarddb(model, "4fpga-ring", "nominal")
        self.assertEqual(len(boarddb["fpgas"]), 4)
        self.assertEqual(len(boarddb["links"]), 4)
        self.assertTrue(boarddb["platform"]["name"].endswith("__nominal"))
        self.assertEqual(boarddb["fpgas"][0]["effective_capacity"]["lut"], 750)
        with self.assertRaisesRegex(ValidationError, "not an explicitly supported"):
            materialize_calibrated_boarddb(model, "8fpga-invented", "nominal")

    def test_disjoint_holdout_passes(self):
        model = fit_calibrated_platform(template(), dataset())
        report = validate_calibrated_platform_holdout(model, dataset("holdout"))
        self.assertEqual(report["status"], "pass")
        self.assertAlmostEqual(report["gates"]["delay_max_relative_error"], 0.0)

    def test_holdout_failure_is_not_silently_accepted(self):
        model = fit_calibrated_platform(template(), dataset())
        holdout = dataset("holdout")
        holdout["link_delay_measurements"][0]["observed_delay_ns"] = 40.0
        report = validate_calibrated_platform_holdout(model, holdout)
        self.assertEqual(report["status"], "fail")

    def test_fit_rejects_free_reference_partitioner_behavior(self):
        observations = dataset()
        observations["capacity_boundaries"][0]["assignment_control"] = "free"
        with self.assertRaisesRegex(ValidationError, "requires fixed"):
            fit_calibrated_platform(template(), observations)

    def test_fit_and_holdout_ids_must_not_overlap(self):
        model = fit_calibrated_platform(template(), dataset())
        holdout = dataset("holdout")
        holdout["link_delay_measurements"][0]["id"] = "fit-delay-base"
        with self.assertRaisesRegex(ValidationError, "overlap fit observations"):
            validate_calibrated_platform_holdout(model, holdout)

    def test_unidentifiable_delay_experiment_is_rejected(self):
        observations = dataset()
        for item in observations["link_delay_measurements"]:
            item["hop_count"] = 1
            item["max_tdm_ratio"] = 1
            item["contention_units"] = 0
        with self.assertRaisesRegex(ValidationError, "not identifiable"):
            fit_calibrated_platform(template(), observations)

    def test_authorization_identity_must_match(self):
        observations = copy.deepcopy(dataset())
        observations["dataset"]["authorization_id"] = "different-authorization"
        with self.assertRaisesRegex(ValidationError, "authorization_id"):
            fit_calibrated_platform(template(), observations)


if __name__ == "__main__":
    unittest.main()
