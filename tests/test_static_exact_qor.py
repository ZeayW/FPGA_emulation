import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.io import read_json, write_json
from emuflow.runtime import QOR_REPORT_SCHEMA
from emuflow.static_exact_qor import (
    _common_source_record,
    _execution_runtime,
    _partition_evidence,
    build_static_exact_qor_comparison,
    parse_static_exact_qor_arms,
    run_static_exact_qor_comparison,
    validate_static_exact_qor_comparison,
)
from emuflow.errors import ValidationError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StaticExactQorTest(unittest.TestCase):
    def test_managed_checkpoint_runtime_is_aggregated(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            roots = {}
            dependency_keys = {
                "v2-partition": "1" * 64,
                "v2-route": "2" * 64,
                "v2-tdm": "3" * 64,
            }
            for name, stage, key, elapsed in (
                ("shared", "shared", "4" * 64, 0.1),
                ("lookahead", "lookahead", "5" * 64, 4.0),
                ("phase6", "phase6", "6" * 64, 0.5),
                ("phase7", "phase7", "7" * 64, 1.0),
            ):
                output = cache / "objects" / key / "output"
                output.mkdir(parents=True)
                roots[name] = output
                value = {
                    "schema": "emuflow.experiment-checkpoint/v2",
                    "storage": "managed",
                    "output_immutable": True,
                    "execution_key": key,
                    "stage": stage,
                    "output_dir": str(output.resolve()),
                    "status": "pass",
                    "execution_elapsed_seconds": elapsed,
                    "dependency_keys": dependency_keys if name == "shared" else {},
                }
                write_json(output.parent / "checkpoint.json", value)
                (output.parent / "checkpoint.json").chmod(0o444)
            for label, stage, elapsed in (
                ("v2-partition", "partition", 2.0),
                ("v2-route", "route", 3.0),
                ("v2-tdm", "tdm", 1.5),
            ):
                object_root = cache / "objects" / dependency_keys[label]
                output = object_root / "output"
                output.mkdir(parents=True)
                write_json(
                    object_root / "checkpoint.json",
                    {
                        "schema": "emuflow.experiment-checkpoint/v2",
                        "storage": "managed",
                        "output_immutable": True,
                        "execution_key": dependency_keys[label],
                        "stage": stage,
                        "output_dir": str(output.resolve()),
                        "status": "pass",
                        "execution_elapsed_seconds": elapsed,
                        "artifacts": {},
                    },
                )
                (object_root / "checkpoint.json").chmod(0o444)
            runtime = _execution_runtime(
                roots["shared"],
                roots["lookahead"],
                roots["phase6"],
                roots["phase7"],
            )
            self.assertEqual(runtime["physical_wall_seconds"], 5.0)
            self.assertEqual(runtime["phase3_to_7_wall_seconds"], 12.0)

            duplicate_key = "8" * 64
            duplicate_output = cache / "objects" / duplicate_key / "output"
            duplicate_output.mkdir(parents=True)
            write_json(
                duplicate_output.parent / "checkpoint.json",
                {
                    "schema": "emuflow.experiment-checkpoint/v2",
                    "storage": "managed",
                    "output_immutable": True,
                    "execution_key": duplicate_key,
                    "stage": "partition",
                    "output_dir": str(duplicate_output.resolve()),
                    "status": "pass",
                    "execution_elapsed_seconds": 2.0,
                    "artifacts": {},
                },
            )
            (duplicate_output.parent / "checkpoint.json").chmod(0o444)
            shared_manifest = roots["shared"].parent / "checkpoint.json"
            shared_manifest.chmod(0o644)
            shared_checkpoint = read_json(shared_manifest)
            shared_checkpoint["dependency_keys"]["v1-partition"] = duplicate_key
            write_json(shared_manifest, shared_checkpoint)
            shared_manifest.chmod(0o444)
            with self.assertRaisesRegex(ValidationError, "dependency is ambiguous"):
                _execution_runtime(
                    roots["shared"],
                    roots["lookahead"],
                    roots["phase6"],
                    roots["phase7"],
                )

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
                "seed": 2,
                "seed_attempts": 1,
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
                    "seed": 2,
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
            self.assertEqual(report["partition_seed"], 2)
            self.assertEqual(report["partition_seed_attempts"], 1)
            self.assertTrue(
                report["promotion_gate"]["controlled_partition_seed"]
            )
            self.assertEqual(
                report["comparisons"]["generalized-v2-vs-sequential"][
                    "target_clock_result"
                ],
                "improved",
            )
            self.assertFalse(
                report["promotion_gate"]["eligible_for_default_promotion"]
            )
            self.assertFalse(
                report["promotion_gate"]["sealed_execution_runtime_available"]
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
    def test_materialized_shared_v1_layout_is_replayable_and_sealed(
        self, _validate
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, records = self._fixture(root)
            for shared, _, _, _ in roots.values():
                partition_path = (
                    shared / "partition/experiment-partition-report.json"
                )
                partition = read_json(partition_path)
                write_json(
                    shared / "partition/phase3_report.json",
                    {
                        "schema": "emuflow.phase3-report/v1",
                        "status": "pass",
                        "seed": 2,
                        "seed_attempts": 1,
                        "cut_mode": partition["cut_mode"],
                        "static_exact_candidate_policy": partition[
                            "static_exact_candidate_policy"
                        ],
                        "validation": partition["phase3"]["validation"],
                    },
                )
                partition_path.unlink()
                (shared / "timing/experiment-timing-report.json").unlink()
                artifacts = {}
                for relative in (
                    "frontend/phase1/design.emuir.json",
                    "timing/path-database.json",
                    "timing/partition-net-weights.json",
                    "partition/assignment.json",
                    "partition/phase3_report.json",
                    "system-route/routes.json",
                    "tdm/schedule.json",
                ):
                    artifacts[relative] = {
                        "sha256": _sha256(shared / relative)
                    }
                write_json(
                    shared / "experiment-shared-report.json",
                    {
                        "schema": "emuflow.experiment-shared-phase1-5/v1",
                        "status": "pass",
                        "platform_sha256": _sha256(platform),
                        "artifacts": artifacts,
                    },
                )

            arms = parse_static_exact_qor_arms(records)
            report = build_static_exact_qor_comparison(platform, arms)
            generalized = next(
                arm
                for arm in report["arms"]
                if arm["label"] == "generalized-static-exact-v2"
            )
            self.assertEqual(
                generalized["partition"]["combinational_cut_nets"], 23
            )
            self.assertEqual(
                report["common_source"]["qualification"],
                "managed-shared-v1-core-source-seal",
            )
            self.assertFalse(
                report["promotion_gate"]["complete_common_source_evidence"]
            )
            self.assertFalse(
                report["promotion_gate"]["eligible_for_default_promotion"]
            )

            phase3_path = (
                roots["generalized-static-exact-v2"][0]
                / "partition/phase3_report.json"
            )
            phase3 = read_json(phase3_path)
            phase3["cut_mode"] = "sequential-only"
            write_json(phase3_path, phase3)
            with self.assertRaisesRegex(ValidationError, "shared seal is broken"):
                build_static_exact_qor_comparison(platform, arms)

    @patch(
        "emuflow.static_exact_qor.validate_phase7_checkpoint",
        return_value={"status": "pass", "provider": "baseline"},
    )
    def test_partition_seed_mismatch_is_rejected(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, records = self._fixture(root)
            shared = roots["generalized-static-exact-v2"][0]
            path = shared / "partition/experiment-partition-report.json"
            report = read_json(path)
            report["seed"] = 3
            report["phase3"]["seed"] = 3
            write_json(path, report)
            arms = parse_static_exact_qor_arms(records)
            with self.assertRaisesRegex(
                ValidationError, "do not share one controlled partition seed"
            ):
                build_static_exact_qor_comparison(platform, arms)

    def test_managed_shared_v1_replays_original_dependency_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, _ = self._fixture(root / "fixture")
            original = roots["generalized-static-exact-v2"][0]
            cache = root / "cache"
            timing_key = "8" * 64
            partition_key = "9" * 64
            shared_key = "a" * 64

            timing_output = cache / "objects" / timing_key / "output"
            partition_output = cache / "objects" / partition_key / "output"
            shared_output = cache / "objects" / shared_key / "output"
            timing_output.mkdir(parents=True)
            partition_output.mkdir(parents=True)
            shutil.copy2(
                original / "timing/experiment-timing-report.json",
                timing_output / "experiment-timing-report.json",
            )
            shutil.copy2(
                original / "partition/experiment-partition-report.json",
                partition_output / "experiment-partition-report.json",
            )
            shutil.copytree(original, shared_output)
            (shared_output / "timing/experiment-timing-report.json").unlink()
            (shared_output / "partition/experiment-partition-report.json").unlink()

            def checkpoint(
                key, stage, output, artifacts, dependencies=None
            ):
                manifest = output.parent / "checkpoint.json"
                write_json(
                    manifest,
                    {
                        "schema": "emuflow.experiment-checkpoint/v2",
                        "storage": "managed",
                        "output_immutable": True,
                        "execution_key": key,
                        "stage": stage,
                        "output_dir": str(output.resolve()),
                        "status": "pass",
                        "execution_elapsed_seconds": 0.1,
                        "dependency_keys": dependencies or {},
                        "artifacts": artifacts,
                    },
                )
                manifest.chmod(0o444)

            for path in (
                timing_output / "experiment-timing-report.json",
                partition_output / "experiment-partition-report.json",
            ):
                path.chmod(0o444)
            checkpoint(
                timing_key,
                "timing",
                timing_output,
                {
                    "experiment-timing-report.json": {
                        "sha256": _sha256(
                            timing_output / "experiment-timing-report.json"
                        )
                    }
                },
            )
            checkpoint(
                partition_key,
                "partition",
                partition_output,
                {
                    "experiment-partition-report.json": {
                        "sha256": _sha256(
                            partition_output
                            / "experiment-partition-report.json"
                        )
                    }
                },
            )
            checkpoint(
                shared_key,
                "shared",
                shared_output,
                {},
                {"timing": timing_key, "v2-partition": partition_key},
            )

            source = _common_source_record(shared_output)
            self.assertEqual(source["target_clocks"], {"clk": 10.0})
            self.assertNotIn("qualification", source)
            evidence = _partition_evidence(
                "generalized-static-exact-v2", shared_output
            )
            self.assertEqual(evidence["combinational_cut_nets"], 23)
            self.assertEqual(evidence["configured_max_dependency_depth"], 3)

            report_path = partition_output / "experiment-partition-report.json"
            report_path.chmod(0o644)
            report = read_json(report_path)
            report["max_cross_fpga_dependency_depth"] = 99
            write_json(report_path, report)
            with self.assertRaisesRegex(ValidationError, "artifact seal is broken"):
                _partition_evidence(
                    "generalized-static-exact-v2", shared_output
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

    @patch(
        "emuflow.static_exact_qor.validate_phase7_checkpoint",
        return_value={"status": "pass", "provider": "baseline"},
    )
    def test_vacuous_legacy_arm_is_an_explicit_negative_control(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, records = self._fixture(root)
            partition_report_path = (
                roots["legacy-static-exact-v1"][0]
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
            self.assertEqual(
                report["exercise_evidence"]["legacy_v1_classification"],
                "vacuous-negative-control",
            )
            self.assertTrue(
                report["exercise_evidence"][
                    "generalized_v2_exercised_real_combinational_cuts"
                ]
            )

    @patch(
        "emuflow.static_exact_qor.validate_phase7_checkpoint",
        return_value={"status": "pass", "provider": "baseline"},
    )
    def test_exercise_flag_must_match_selected_cut_count(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform, roots, records = self._fixture(root)
            partition_report_path = (
                roots["legacy-static-exact-v1"][0]
                / "partition/experiment-partition-report.json"
            )
            partition = read_json(partition_report_path)
            partition["static_exact_combinational_cut_exercised"] = False
            write_json(partition_report_path, partition)
            with self.assertRaisesRegex(ValidationError, "exercise evidence"):
                build_static_exact_qor_comparison(
                    platform, parse_static_exact_qor_arms(records)
                )


if __name__ == "__main__":
    unittest.main()
