from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_calibration_cli import main
from emuflow.ppro_holdout_validation import evaluate_holdout_promotion


def ppro_observation(identifier: str, delay: float):
    return {
        "schema": "emuflow.ppro-blackbox-observation/v1",
        "identity": {
            "id": identifier,
            "campaign_id": "blind-stage6",
            "case_id": identifier,
            "role": "holdout",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m2",
        },
        "tool": {"name": "PPro mock", "release": "mock", "runner_revision": "1" * 64},
        "workload": {
            "generator_id": "upstream-connected-rtl",
            "generator_revision": "2" * 64,
            "rtl_sha256": "3" * 64,
            "parameters_sha256": "4" * 64,
        },
        "experiment": {
            "kind": "application_holdout",
            "control_mode": "free_optimization",
            "documented_actions": ["random_seed"],
            "constraints_sha256": "5" * 64,
        },
        "execution": {"seed": 1, "outcome": "pass", "failure_code": None, "runtime_seconds": 10.0},
        "reports": {"partition_summary": True, "resource_summary": True, "route_summary": True, "system_timing": True},
        "metrics": {
            "design": {"instances": 1000},
            "resource_demand": {"lut": 800},
            "fpga_utilization": [
                {"fpga": "F0", "resources": {"lut": 0.50}},
                {"fpga": "F1", "resources": {"lut": 0.40}},
            ],
            "assignments": [{"partition": "P0", "fpga": "F0"}],
            "routes": [
                {"id": "r0", "source": "F0", "sinks": ["F1"], "effective_hops": 1},
                {"id": "r1", "source": "F0", "sinks": ["F1"], "effective_hops": 1},
            ],
            "communication": {"maximum_tdm_ratio": 2},
            "timing": {"sr0_worst_cross_fpga_delay_ns": delay},
        },
        "provenance": {"class": "black_box_observation"},
        "derived": {"fit_eligible": False, "reason": "holdout-not-fit"},
    }


def result(identifier: str, workload: str, tier: str, algorithm: str, delay: float):
    return {
        "schema": "emuflow.ppro-holdout-result/v1",
        "id": identifier,
        "workload_id": workload,
        "tier": tier,
        "algorithm_id": algorithm,
        "ppro": ppro_observation(identifier, delay),
        "emuflow": {
            "status": "pass",
            "configuration_id": "lx2-m2",
            "physical_seed": 1,
            "resource_utilization": {"lut": 0.55},
            "maximum_tdm_ratio": 2,
            "worst_cross_fpga_delay_ns": delay * 1.05,
            "busiest_pairs": ["F0->F1"],
            "global_wns_ns": -1.0,
            "global_tns_ns": -10.0,
            "global_timing_engine": "opensta",
            "phase1_7_complete": True,
            "macro_cycle_equivalence": True,
            "schedule_legality": True,
            "zero_unrouted_nets": True,
            "zero_drc_violations": True,
            "original_path_coverage": 1.0,
        },
    }


class PProHoldoutValidationTest(unittest.TestCase):
    @staticmethod
    def complete_results():
        return [
            result("aes-a", "aes", "medium", "a", 10.0),
            result("aes-b", "aes", "medium", "b", 12.0),
            result("cpu-a", "cpu", "diversity", "a", 11.0),
            result("gemm-a", "gemm", "large", "a", 13.0),
            result("dla-a", "dla", "large_primary", "a", 14.0),
            result("nvdla-a", "nvdla", "very_large_final", "a", 15.0),
        ]

    def test_complete_tiers_and_ranking_promote(self):
        values = self.complete_results()
        report = evaluate_holdout_promotion(values)
        self.assertTrue(report["promoted"])
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["ranking_checks"][0]["matches"])

    def test_incomplete_physical_or_bad_delay_fails_promotion(self):
        values = self.complete_results()
        values[-1]["emuflow"]["zero_unrouted_nets"] = False
        values[-2]["emuflow"]["worst_cross_fpga_delay_ns"] = 30.0
        report = evaluate_holdout_promotion(values)
        self.assertFalse(report["promoted"])
        self.assertEqual(report["status"], "fail")

    def test_noninteger_tdm_and_missing_report_fail_closed(self):
        value = result("aes-a", "aes", "medium", "a", 10.0)
        value["emuflow"]["maximum_tdm_ratio"] = 2.5
        with self.assertRaises(ValidationError):
            evaluate_holdout_promotion([value])

        value = result("aes-a", "aes", "medium", "a", 10.0)
        value["ppro"]["reports"]["system_timing"] = False
        with self.assertRaises(ValidationError):
            evaluate_holdout_promotion([value])

    def test_zero_reference_delay_does_not_hide_nonzero_error(self):
        values = [
            result("aes-a", "aes", "medium", "a", 0.0),
            result("aes-b", "aes", "medium", "b", 12.0),
            result("cpu-a", "cpu", "diversity", "a", 11.0),
            result("gemm-a", "gemm", "large", "a", 13.0),
            result("dla-a", "dla", "large_primary", "a", 14.0),
            result("nvdla-a", "nvdla", "very_large_final", "a", 15.0),
        ]
        values[0]["emuflow"]["worst_cross_fpga_delay_ns"] = 1.0
        report = evaluate_holdout_promotion(values)
        self.assertFalse(report["promoted"])
        self.assertEqual(report["cases"][0]["cross_fpga_delay_relative_error"], 1.0)

    def test_schema_and_cli_evaluation(self):
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        values = self.complete_results()
        if jsonschema is not None:
            root = Path(__file__).resolve().parents[1]
            schema = json.loads(
                (root / "schemas/ppro-holdout-result-v1.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            jsonschema.Draft202012Validator(schema).validate(values[0])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = []
            for value in values:
                path = root / f"{value['id']}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                paths.append(path)
            report_path = root / "promotion.json"
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "evaluate-holdouts",
                        "--results",
                        *(str(path) for path in paths),
                        "--out",
                        str(report_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(report_path.read_text())["promoted"])
            self.assertTrue(json.loads(output.getvalue())["promoted"])


if __name__ == "__main__":
    unittest.main()
