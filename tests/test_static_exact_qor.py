import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.io import read_json, write_json
from emuflow.runtime import QOR_REPORT_SCHEMA
from emuflow.static_exact_qor import (
    build_static_exact_qor_comparison,
    parse_static_exact_qor_arms,
    run_static_exact_qor_comparison,
    validate_static_exact_qor_comparison,
)
from emuflow.errors import ValidationError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StaticExactQorTest(unittest.TestCase):
    def _fixture(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        platform = root / "platform.json"
        write_json(platform, {"name": "eda2023-case6-rtl"})
        common_emuir = {"design": "DLA"}
        common_paths = {"paths": ["p0", "p1"]}
        common_weights = {"weights": {"n0": 2.0}}
        labels = {
            "sequential-only": {
                "mode": "sequential-only",
                "policy": "potential-frontier-depth-v1",
                "cuts": 0,
                "depth": 0,
                "offset": 0.0,
            },
            "legacy-static-exact-v1": {
                "mode": "static-exact-combinational",
                "policy": "potential-frontier-depth-v1",
                "cuts": 5,
                "depth": 1,
                "offset": 0.1,
            },
            "generalized-static-exact-v2": {
                "mode": "static-exact-combinational",
                "policy": "assignment-derived-acyclic-v2",
                "cuts": 23,
                "depth": 3,
                "offset": 0.3,
            },
        }
        arms = []
        roots = {}
        for label, config in labels.items():
            shared = root / label / "shared"
            lookahead = root / label / "lookahead"
            phase6 = root / label / "phase6"
            phase7 = root / label / "phase7"
            for directory in (shared, lookahead, phase6, phase7):
                directory.mkdir(parents=True)
            for relative, value in (
                ("frontend/phase1/design.emuir.json", common_emuir),
                ("timing/path-database.json", common_paths),
                ("timing/partition-net-weights.json", common_weights),
                ("partition/assignment.json", {"label": label}),
                ("system-route/routes.json", {"label": label}),
                ("tdm/schedule.json", {"label": label}),
            ):
                write_json(shared / relative, value)
            timing_report = {
                "status": "pass",
                "frontend_emuir_sha256": _sha256(
                    shared / "frontend/phase1/design.emuir.json"
                ),
                "path_database_sha256": _sha256(
                    shared / "timing/path-database.json"
                ),
                "partition_net_weights_sha256": _sha256(
                    shared / "timing/partition-net-weights.json"
                ),
                "clocks": {"clk": 10.0},
                "timing_model_sha256": "1" * 64,
                "architecture_timing_db_sha256": "2" * 64,
            }
            write_json(
                shared / "timing/experiment-timing-report.json", timing_report
            )
            partition_report = {
                "status": "pass",
                "cut_mode": config["mode"],
                "static_exact_candidate_policy": config["policy"],
                "max_cross_fpga_dependency_depth": max(1, config["depth"]),
                "static_exact_combinational_cut_exercised": config["cuts"] > 0,
                "emuir_sha256": timing_report["frontend_emuir_sha256"],
                "weights_sha256": timing_report[
                    "partition_net_weights_sha256"
                ],
                "platform_sha256": _sha256(platform),
                "route_constraints_sha256": "3" * 64,
                "constraints_sha256": None,
                "phase3": {
                    "validation": {
                        "status": "pass",
                        "combinational_cut_nets": config["cuts"],
                        "maximum_combinational_dependency_depth": config["depth"],
                    }
                },
            }
            write_json(
                shared / "partition/experiment-partition-report.json",
                partition_report,
            )
            write_json(phase6 / "schedule.json", {"label": label, "phase": 6})
            qor = {
                "schema": QOR_REPORT_SCHEMA,
                "status": "pass",
                "design": "DLA",
                "platform": "eda2023-case6-rtl",
                "timing": {
                    "status": "pass",
                    "qualification": "whole-original-design",
                    "path_exactness": {"complete_original_path_coverage": True},
                    "target_clock": {
                        "worst_slack_bound_ns": -2.0 + config["offset"],
                        "total_negative_slack_bound_ns": -6.0 + config["offset"],
                        "negative_slack_paths": 4,
                    },
                    "runtime_clock": {
                        "worst_slack_bound_ns": 4.0 + config["offset"],
                        "total_negative_slack_bound_ns": 0.0,
                        "negative_slack_paths": 0,
                    },
                },
                "runtime": {
                    "nominal_virtual_frequency_mhz": 10.0 + config["offset"]
                },
                "partition": {"cut_nets": 100 + config["cuts"]},
                "system_routing": {"bit_hops": 200},
                "tdm": {
                    "scheduled_bit_hops": 300 + config["cuts"],
                    "frame_slots": 32,
                    "completion_slot": 24,
                },
                "physical": {
                    "worst_wns_ns": -1.0 + config["offset"],
                    "total_tns_ns": -3.0 + config["offset"],
                    "transport_cells": 40 + config["cuts"],
                    "physical_cells": 1000 + config["cuts"],
                },
            }
            (phase7 / "runtime").mkdir()
            write_json(phase7 / "runtime/qor_report.json", qor)
            report = {
                "schema": "emuflow.experiment-phase7-checkpoint/v2",
                "status": "pass",
                "provider": "baseline",
                "physical_seed": 1,
                "workers": 8,
                "route_channel_width": 300,
                "phase6_manifest_sha256": hashlib.sha256(
                    f"{label}-manifest".encode()
                ).hexdigest(),
                "frozen_upstream": {
                    "emuir_sha256": timing_report["frontend_emuir_sha256"],
                    "assignment_sha256": _sha256(
                        shared / "partition/assignment.json"
                    ),
                    "routes_sha256": _sha256(
                        shared / "system-route/routes.json"
                    ),
                    "schedule_sha256": _sha256(phase6 / "schedule.json"),
                },
                "physical_summary_sha256": "4" * 64,
                "qor_sha256": _sha256(phase7 / "runtime/qor_report.json"),
            }
            write_json(phase7 / "experiment-phase7-report.json", report)
            roots[label] = (shared, lookahead, phase6, phase7)
            arms.append(
                [
                    label,
                    "1",
                    str(shared),
                    str(lookahead),
                    str(phase6),
                    str(phase7),
                ]
            )
        return platform, roots, arms

    @patch(
        "emuflow.static_exact_qor.validate_phase7_checkpoint",
        return_value={"status": "pass", "provider": "baseline"},
    )
    def test_three_policy_comparison_is_replayable(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, _, records = self._fixture(root)
            arms = parse_static_exact_qor_arms(records)
            report = run_static_exact_qor_comparison(
                platform, arms, root / "comparison"
            )
            self.assertEqual(len(report["arms"]), 3)
            self.assertEqual(report["physical_seeds"], [1])
            self.assertEqual(
                report["comparisons"]["generalized-v2-vs-sequential"][
                    "target_clock_result"
                ],
                "improved",
            )
            self.assertTrue(
                report["promotion_gate"]["eligible_for_default_promotion"]
            )
            self.assertEqual(
                validate_static_exact_qor_comparison(
                    root / "comparison", platform, arms
                )["arms"],
                3,
            )

    @patch(
        "emuflow.static_exact_qor.validate_phase7_checkpoint",
        return_value={"status": "pass", "provider": "baseline"},
    )
    def test_common_source_and_policy_tampering_are_rejected(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, records = self._fixture(root)
            arms = parse_static_exact_qor_arms(records)
            timing_path = (
                roots["generalized-static-exact-v2"][0]
                / "timing/path-database.json"
            )
            write_json(timing_path, {"paths": ["different"]})
            timing_report_path = timing_path.parent / "experiment-timing-report.json"
            timing_report = read_json(timing_report_path)
            timing_report["path_database_sha256"] = _sha256(timing_path)
            write_json(timing_report_path, timing_report)
            with self.assertRaisesRegex(ValidationError, "exact source/timing"):
                build_static_exact_qor_comparison(platform, arms)

            platform, roots, records = self._fixture(root / "fresh")
            arms = parse_static_exact_qor_arms(records)
            partition_report_path = (
                roots["generalized-static-exact-v2"][0]
                / "partition/experiment-partition-report.json"
            )
            partition = read_json(partition_report_path)
            partition["static_exact_candidate_policy"] = (
                "potential-frontier-depth-v1"
            )
            write_json(partition_report_path, partition)
            with self.assertRaisesRegex(ValidationError, "cut contract"):
                build_static_exact_qor_comparison(platform, arms)

    @patch(
        "emuflow.static_exact_qor.validate_phase7_checkpoint",
        return_value={"status": "pass", "provider": "baseline"},
    )
    def test_vacuous_generalized_arm_cannot_pass_promotion_gate(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, records = self._fixture(root)
            partition_report_path = (
                roots["generalized-static-exact-v2"][0]
                / "partition/experiment-partition-report.json"
            )
            partition = read_json(partition_report_path)
            partition["phase3"]["validation"]["combinational_cut_nets"] = 0
            partition["phase3"]["validation"][
                "maximum_combinational_dependency_depth"
            ] = 0
            partition["static_exact_combinational_cut_exercised"] = False
            write_json(partition_report_path, partition)
            report = build_static_exact_qor_comparison(
                platform, parse_static_exact_qor_arms(records)
            )
            self.assertFalse(
                report["promotion_gate"]["eligible_for_default_promotion"]
            )


if __name__ == "__main__":
    unittest.main()
