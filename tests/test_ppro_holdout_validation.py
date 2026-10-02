from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.ppro_blackbox_application import benchmark_rtl_identity
from emuflow.ppro_calibration_cli import main
from emuflow.ppro_holdout_validation import (
    assemble_holdout_result,
    evaluate_holdout_promotion,
)


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
            "top_module": "application_holdout",
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
                {"id": "r0", "source": "F0", "sinks": ["F1"], "effective_hops": 1, "signal_count": 1},
                {"id": "r1", "source": "F0", "sinks": ["F1"], "effective_hops": 1, "signal_count": 1},
            ],
            "communication": {"maximum_tdm_ratio": 2},
            "timing": {"sr0_worst_cross_fpga_delay_ns": delay},
        },
        "provenance": {"class": "black_box_observation"},
        "derived": {"fit_eligible": False, "reason": "holdout-not-fit"},
    }


def result(identifier: str, workload: str, tier: str, algorithm: str, delay: float):
    benchmark_class = {
        "medium": "secworks_aes",
        "diversity": "open_cpu",
        "large": "koios_compute",
        "large_primary": "koios_dla",
        "very_large_final": "nvdla",
    }[tier]
    return {
        "schema": "emuflow.ppro-holdout-result/v3",
        "id": identifier,
        "workload_id": workload,
        "benchmark_class": benchmark_class,
        "tier": tier,
        "algorithm_id": algorithm,
        "evidence": {
            "producer": "independent-flow-bundle-assembler-v1",
            "benchmark_run_sha256": "6" * 64,
            "platform_manifest_sha256": "7" * 64,
            "platform_boarddb_sha256": "8" * 64,
            "flow_report_sha256": "9" * 64,
            "schedule_sha256": "a" * 64,
            "physical_flow_report_sha256": "b" * 64,
            "qor_report_sha256": "c" * 64,
        },
        "ppro": ppro_observation(identifier, delay),
        "emuflow": {
            "status": "pass",
            "configuration_id": "lx2-m2",
            "rtl_sha256": "3" * 64,
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

    def test_rtl_identity_and_benchmark_class_fail_closed(self):
        value = result("aes-a", "aes", "medium", "a", 10.0)
        value["emuflow"]["rtl_sha256"] = "6" * 64
        with self.assertRaisesRegex(ValidationError, "RTL identities"):
            evaluate_holdout_promotion([value])

        value = result("aes-a", "aes", "medium", "a", 10.0)
        value["benchmark_class"] = "open_cpu"
        with self.assertRaisesRegex(ValidationError, "class and tier"):
            evaluate_holdout_promotion([value])

    def test_duplicate_identity_or_algorithm_fails_closed(self):
        value = result("aes-a", "aes", "medium", "a", 10.0)
        with self.assertRaisesRegex(ValidationError, "duplicate result"):
            evaluate_holdout_promotion([value, value])

        left = result("aes-a", "aes", "medium", "a", 10.0)
        right = result("aes-b", "aes", "medium", "a", 10.0)
        with self.assertRaisesRegex(ValidationError, "duplicate workload algorithm"):
            evaluate_holdout_promotion([left, right])

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
                (root / "schemas/ppro-holdout-result-v3.schema.json").read_text(
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

    def test_assembler_derives_claims_from_sealed_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "sources"
            source_root.mkdir()
            rtl = source_root / "design.v"
            rtl.write_text("module top(input clk); endmodule\n", encoding="utf-8")
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "blind-aes",
                        "design_id": "blind-aes",
                        "top": "top",
                        "sources": ["design.v"],
                        "clocks": ["clk"],
                        "platform": "unused.json",
                        "synthesis": {"family": "xcup", "policy": "logic-only"},
                    }
                ),
                encoding="utf-8",
            )
            identity = benchmark_rtl_identity(benchmark, source_root)
            observation = ppro_observation("blind-aes", 10.0)
            observation["workload"]["rtl_sha256"] = identity["rtl_sha256"]
            observation["workload"]["parameters_sha256"] = identity[
                "parameters_sha256"
            ]
            observation["workload"]["top_module"] = "top"
            observation_path = root / "observation.json"
            observation_path.write_text(json.dumps(observation), encoding="utf-8")

            boarddb = {
                "schema": "emuflow.boarddb/v1",
                "platform": {"name": "calibrated", "kind": "virtual", "description": "test"},
                "fpgas": [
                    {"id": fpga, "part": "academic", "utilization_limit": 0.75,
                     "capacity": {"lut": 100, "ff": 200}}
                    for fpga in ("F0", "F1")
                ],
                "links": [
                    {"id": "l0", "endpoints": ["F0", "F1"], "direction": "full_duplex",
                     "mode": "abstract", "data_lanes_per_direction": 8,
                     "fabric_clock_mhz": 100.0, "latency_cycles": 1,
                     "capacity_sharing": "per_direction"}
                ],
            }
            normalized_boarddb = Platform.from_dict(boarddb).to_dict()
            bundle = root / "platform"
            (bundle / "nominal").mkdir(parents=True)
            (bundle / "nominal" / "boarddb.json").write_text(
                json.dumps(boarddb), encoding="utf-8"
            )
            boarddb_digest = __import__("hashlib").sha256(
                json.dumps(boarddb, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            manifest = {
                "configuration_id": "lx2-m2",
                "profiles": {"nominal": {"boarddb": boarddb_digest}},
            }
            (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            flow = root / "flow"
            for directory in ("frontend/phase1", "partition", "tdm", "physical", "runtime"):
                (flow / directory).mkdir(parents=True, exist_ok=True)
            platform_path = flow / "frontend/phase1/platform.normalized.json"
            platform_path.write_text(json.dumps(normalized_boarddb), encoding="utf-8")
            emuir = {
                "schema": "emuflow.emuir/v1",
                "design": {"name": "top", "top": "top", "source_format": "yosys-json"},
                "ports": [],
                "instances": [],
                "nets": [],
                "clocks": [{"id": "clk"}],
            }
            emuir_path = flow / "frontend/phase1/design.emuir.json"
            emuir_path.write_text(json.dumps(emuir), encoding="utf-8")
            phase3 = {"validation": {"resources_by_fpga": {
                "F0": {"lut": 50, "ff": 20}, "F1": {"lut": 40, "ff": 30}
            }}}
            (flow / "partition/phase3_report.json").write_text(json.dumps(phase3), encoding="utf-8")
            schedule = {"entries": [
                {"from": "F0", "to": "F1", "tdm_ratio": 2},
                {"from": "F0", "to": "F1", "tdm_ratio": 1},
            ]}
            physical = {
                "execution": {"seed": 1},
                "fpgas": [
                    {"physical_result": {"closure": {"unrouted_nets": 0, "drc_violations": 0}}},
                    {"physical_result": {"closure": {"unrouted_nets": 0, "drc_violations": 0}}},
                ],
            }
            qor = {"timing": {
                "timing_scope": "whole-original-design",
                "global_opensta": {"authority": "opensta", "execution": "standalone"},
                "summary": {"original_path_coverage": 1.0},
                "target_clock": {"worst_slack_bound_ns": -1.0, "total_negative_slack_bound_ns": -2.0},
                "paths": [{"path_scope": "cross-fpga", "system_delay_bound_ns": 10.5}],
            }}
            paths = {
                "schedule": flow / "tdm/schedule.json",
                "physical_flow_report": flow / "physical/multi-fpga-physical-flow-report.json",
                "qor_report": flow / "runtime/qor_report.json",
            }
            paths["schedule"].write_text(json.dumps(schedule), encoding="utf-8")
            paths["physical_flow_report"].write_text(json.dumps(physical), encoding="utf-8")
            paths["qor_report"].write_text(json.dumps(qor), encoding="utf-8")
            sha = lambda path: __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            flow_report = {
                "status": "pass",
                "stages": {"frontend": {"synthesis": {"sources": [str(rtl.resolve())]} }},
                "runtime": {
                    "functional_equivalence": {"status": "pass"},
                    "schedule_legality": {"status": "pass", "collisions": 0},
                },
                "artifacts": {
                    "platform": {"path": "frontend/phase1/platform.normalized.json", "sha256": sha(platform_path)},
                    "emuir": {"path": "frontend/phase1/design.emuir.json", "sha256": sha(emuir_path)},
                    **{
                        name: {"path": path.relative_to(flow).as_posix(), "sha256": sha(path)}
                        for name, path in paths.items()
                    },
                },
            }
            (flow / "multi-fpga-flow-report.json").write_text(json.dumps(flow_report), encoding="utf-8")

            with patch(
                "emuflow.ppro_holdout_validation.validate_calibrated_platform_bundle",
                return_value={"status": "pass", "configuration_id": "lx2-m2"},
            ), patch(
                "emuflow.ppro_holdout_validation.validate_multi_fpga_flow_bundle",
                return_value={"status": "pass"},
            ) as flow_validator:
                value = assemble_holdout_result(
                    result_id="blind-aes-a",
                    workload_id="blind-aes",
                    benchmark_class="secworks_aes",
                    algorithm_id="default",
                    ppro_observation_path=observation_path,
                    flow_root=flow,
                    benchmark_run_path=benchmark,
                    source_root=source_root,
                    platform_bundle_root=bundle,
                    profile="nominal",
                )
            flow_validator.assert_called_once_with(flow.resolve(), require_physical=True)
            self.assertEqual(value["emuflow"]["resource_utilization"]["lut"], 0.5)
            self.assertEqual(value["emuflow"]["maximum_tdm_ratio"], 2)
            self.assertEqual(value["emuflow"]["worst_cross_fpga_delay_ns"], 10.5)
            self.assertEqual(value["emuflow"]["busiest_pairs"], ["F0->F1"])
            self.assertEqual(value["evidence"]["schedule_sha256"], sha(paths["schedule"]))

            physical["execution"]["seed"] = 2
            paths["physical_flow_report"].write_text(json.dumps(physical), encoding="utf-8")
            flow_report["artifacts"]["physical_flow_report"]["sha256"] = sha(
                paths["physical_flow_report"]
            )
            (flow / "multi-fpga-flow-report.json").write_text(json.dumps(flow_report), encoding="utf-8")
            with patch(
                "emuflow.ppro_holdout_validation.validate_calibrated_platform_bundle",
                return_value={"status": "pass", "configuration_id": "lx2-m2"},
            ), patch(
                "emuflow.ppro_holdout_validation.validate_multi_fpga_flow_bundle",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(ValidationError, "physical seed 1"):
                    assemble_holdout_result(
                        result_id="blind-aes-a", workload_id="blind-aes",
                        benchmark_class="secworks_aes", algorithm_id="default",
                        ppro_observation_path=observation_path, flow_root=flow,
                        benchmark_run_path=benchmark, source_root=source_root,
                        platform_bundle_root=bundle, profile="nominal",
                    )


if __name__ == "__main__":
    unittest.main()
