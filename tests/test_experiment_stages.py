import argparse
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow import experiment_dag
from emuflow.cli import _Python38BooleanOptionalAction, _build_parser
from emuflow.errors import EmuFlowError
from emuflow.experiment_stages import (
    _ValidationSession,
    _phase7_qor_projection,
    _physical_timing_databases,
    _placement_aware_positions,
    _prepare_empty_output,
    _validate_managed_phase6_checkpoint,
    _sta_path_database,
    _timing_paths,
    validate_phase6_checkpoint,
    validate_shared_phase1_5,
    resume_physical_lookahead,
)
from emuflow.experiment_upstream import (
    run_frontend_checkpoint,
    validate_frontend_checkpoint,
)
from emuflow.io import read_json, write_json
from emuflow.pin_planning import SIGNAL_POSITION_HINTS_SCHEMA
from emuflow.runtime import QOR_REPORT_SCHEMA


class ExperimentStagesTest(unittest.TestCase):
    def test_python38_boolean_optional_action_supports_both_spelling(self) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--repair-balance",
            action=_Python38BooleanOptionalAction,
            default=None,
        )
        self.assertTrue(parser.parse_args(["--repair-balance"]).repair_balance)
        self.assertFalse(
            parser.parse_args(["--no-repair-balance"]).repair_balance
        )
        self.assertIsNone(parser.parse_args([]).repair_balance)

    def test_phase7_qor_projection_is_compact_and_rejects_nonfinite(self) -> None:
        qor = {
            "schema": QOR_REPORT_SCHEMA,
            "status": "pass",
            "design": "design",
            "platform": "platform",
            "timing": {
                "status": "pass",
                "qualification": "whole-design",
                "path_exactness": {"scheduled_link_tdm": True},
                "target_clock": {
                    "worst_slack_bound_ns": -2.0,
                    "total_negative_slack_bound_ns": -4.0,
                    "negative_slack_paths": 2,
                    "large_path_payload": [0] * 100,
                },
                "runtime_clock": {
                    "worst_slack_bound_ns": 1.0,
                    "total_negative_slack_bound_ns": 0.0,
                    "negative_slack_paths": 0,
                },
            },
            "physical": {
                "status": "pass",
                "worst_wns_ns": -0.5,
                "total_tns_ns": -1.5,
                "unrouted_nets": 0,
                "drc_violations": 0,
                "large_route_payload": [0] * 100,
            },
        }
        projection = _phase7_qor_projection(qor)
        self.assertNotIn(
            "large_path_payload", projection["timing"]["target_clock"]
        )
        self.assertNotIn("large_route_payload", projection["physical"])
        qor["timing"]["target_clock"]["worst_slack_bound_ns"] = float("nan")
        with self.assertRaisesRegex(Exception, "must be finite"):
            _phase7_qor_projection(qor)

    def test_validation_session_deduplicates_one_physical_report(self) -> None:
        report = {"schema": "fixture"}
        session = _ValidationSession()
        with mock.patch(
            "emuflow.experiment_stages.validate_multi_fpga_physical_report",
            return_value={"status": "pass"},
        ) as validate:
            first = session.validate_physical(report)
            first["status"] = "mutated-by-caller"
            second = session.validate_physical(report)
        validate.assert_called_once_with(report)
        self.assertEqual(second, {"status": "pass"})

    def test_shared_timing_uses_partition_projected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing"
            timing.mkdir()
            self.assertIsNone(_sta_path_database(root))
            (timing / "path-database.json").write_text(
                "{}", encoding="utf-8"
            )
            self.assertEqual(
                _sta_path_database(root), timing / "path-database.json"
            )
            (timing / "cut-path-database.json").write_text(
                "{}", encoding="utf-8"
            )
            self.assertIsNone(_timing_paths(root))
            write_json(
                timing / "cut-timing-paths.json",
                {"source": {"input": "cut-path-database.json"}},
            )
            self.assertEqual(
                _physical_timing_databases(root),
                (
                    timing / "path-database.json",
                    timing / "cut-path-database.json",
                ),
            )
            projected = timing / "cut-timing-paths.json"
            self.assertEqual(_timing_paths(root), projected)
            write_json(
                projected,
                {"source": {"input": "path-database.json"}},
            )
            self.assertEqual(
                _physical_timing_databases(root),
                (
                    timing / "path-database.json",
                    timing / "path-database.json",
                ),
            )

    def test_shared_validator_passes_projected_timing_paths_to_phase4(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        platform = repository / "platforms/virtual/xcvu3p_2fpga_p2p.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            run_frontend_checkpoint(
                platform,
                frontend,
                yosys_json=repository / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
            )
            for relative in (
                "partition/clusters.json",
                "partition/assignment.json",
                "partition/phase3_report.json",
                "system-route/routes.json",
                "system-route/phase4_report.json",
                "tdm/schedule.json",
                "tdm/phase5_report.json",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json(path, {})
            projected = root / "timing/cut-timing-paths.json"
            projected.parent.mkdir(parents=True, exist_ok=True)
            write_json(projected, {"source": {"input_sha256": "a" * 64}})
            with mock.patch("emuflow.experiment_stages.validate_phase3"), mock.patch(
                "emuflow.experiment_stages.validate_phase4"
            ) as validate_phase4, mock.patch("emuflow.experiment_stages.validate_phase5"):
                report = validate_shared_phase1_5(root, platform)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                validate_phase4.call_args.kwargs["timing_paths_path"],
                projected.resolve(),
            )

    def test_physical_timing_requires_original_database_and_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing"
            timing.mkdir()
            self.assertEqual(_physical_timing_databases(root), (None, None))
            (timing / "path-database.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "sealed Phase 4"):
                _physical_timing_databases(root)
            write_json(
                timing / "cut-timing-paths.json",
                {"source": {"input": "path-database.json"}},
            )
            self.assertEqual(
                _physical_timing_databases(root),
                (
                    timing / "path-database.json",
                    timing / "path-database.json",
                ),
            )
            (timing / "cut-timing-paths.json").unlink()
            (timing / "path-database.json").unlink()
            (timing / "cut-path-database.json").write_text(
                "{}", encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "complete original STA"):
                _physical_timing_databases(root)

    def test_physical_timing_rejects_unknown_projection_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing"
            timing.mkdir()
            for name in ("path-database.json", "cut-path-database.json"):
                (timing / name).write_text("{}", encoding="utf-8")
            write_json(
                timing / "cut-timing-paths.json",
                {"source": {"input": "unsealed.json"}},
            )
            with self.assertRaisesRegex(Exception, "unknown STA database"):
                _physical_timing_databases(root)

    def test_physical_timing_accepts_content_addressed_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing"
            timing.mkdir()
            full = timing / "path-database.json"
            cut = timing / "cut-path-database.json"
            full.write_text('{"paths":["complete"]}', encoding="utf-8")
            cut.write_text('{"paths":["through-cut"]}', encoding="utf-8")
            projected = timing / "cut-timing-paths.json"

            write_json(
                projected,
                {"source": {"input_sha256": hashlib.sha256(full.read_bytes()).hexdigest()}},
            )
            self.assertEqual(_physical_timing_databases(root), (full, full))

            write_json(
                projected,
                {"source": {"input_sha256": hashlib.sha256(cut.read_bytes()).hexdigest()}},
            )
            self.assertEqual(_physical_timing_databases(root), (full, cut))

            write_json(projected, {"source": {"input_sha256": "0" * 64}})
            with self.assertRaisesRegex(Exception, "digest does not name"):
                _physical_timing_databases(root)

            write_json(
                projected,
                {
                    "source": {
                        "input": "cut-path-database.json",
                        "input_sha256": hashlib.sha256(full.read_bytes()).hexdigest(),
                    }
                },
            )
            with self.assertRaisesRegex(Exception, "path and digest disagree"):
                _physical_timing_databases(root)

    def test_frontend_checkpoint_is_reusable_and_tamper_evident(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        platform = repository / "platforms/virtual/xcvu3p_2fpga_p2p.json"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frontend"
            report = run_frontend_checkpoint(
                platform,
                output,
                yosys_json=repository / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(len(report["source_artifacts"]), 1)
            self.assertTrue(
                (output / report["source_artifacts"][0]["artifact"]).is_file()
            )
            self.assertEqual(
                validate_frontend_checkpoint(output, platform)["status"], "pass"
            )
            (output / "phase1/design.emuir.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "EmuIR seal"):
                validate_frontend_checkpoint(output, platform)

    def test_checkpoint_runner_accepts_precreated_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "staging"
            output.mkdir()
            self.assertEqual(
                _prepare_empty_output(output, "checkpoint"), output.resolve()
            )
            (output / "artifact").write_text("present", encoding="utf-8")
            with self.assertRaisesRegex(EmuFlowError, "must be an empty"):
                _prepare_empty_output(output, "checkpoint")

    def test_direct_stage_output_obeys_validation_server_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {"EMUFLOW_REQUIRE_RESEARCH_STORAGE": "1"},
        ):
            with self.assertRaisesRegex(Exception, "restricted"):
                _prepare_empty_output(Path(temporary) / "outside", "checkpoint")

    def test_frontend_source_artifact_cannot_escape_checkpoint(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        platform = repository / "platforms/virtual/xcvu3p_2fpga_p2p.json"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frontend"
            run_frontend_checkpoint(
                platform,
                output,
                yosys_json=repository / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
            )
            report_path = output / "experiment-frontend-report.json"
            report = read_json(report_path)
            report["source_artifacts"][0]["artifact"] = "sources/../../outside"
            write_json(report_path, report)
            with self.assertRaisesRegex(Exception, "path is invalid"):
                validate_frontend_checkpoint(output, platform)

    def test_placement_aware_positions_reuse_frozen_open_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir = root / "ir.json"
            schedule = root / "schedule.json"
            placement = root / "placement.json"
            write_json(
                ir,
                {
                    "nets": [
                        {
                            "id": "n0",
                            "drivers": [{"instance": "a"}],
                            "sinks": [{"instance": "b"}],
                        }
                    ]
                },
            )
            write_json(
                schedule,
                {
                    "design": "d",
                    "platform": "p",
                    "entries": [
                        {"id": "e0", "net": "n0", "from": "f0", "to": "f1"}
                    ],
                },
            )
            write_json(
                placement,
                {
                    "fpgas": [
                        {
                            "fpga": "f0",
                            "instances": [{"id": "a", "normalised_y": 0.2}],
                        },
                        {
                            "fpga": "f1",
                            "instances": [{"id": "b", "normalised_y": 0.9}],
                        },
                    ]
                },
            )
            positions = _placement_aware_positions(
                ir, schedule, placement, region_count=4
            )
            self.assertEqual(positions["schema"], SIGNAL_POSITION_HINTS_SCHEMA)
            self.assertEqual(
                positions["entries"],
                [
                    {
                        "schedule_entry": "e0",
                        "source_y": 0.2,
                        "sink_y": 0.9,
                        "source_region": 0,
                        "sink_region": 3,
                        "source_fallback": False,
                        "sink_fallback": False,
                    }
                ],
            )

    def test_cli_exposes_provider_seed_checkpoint_commands(self) -> None:
        args = _build_parser().parse_args(
            [
                "experiment-stage",
                "phase7-run",
                "--shared",
                "shared",
                "--lookahead",
                "lookahead",
                "--phase6",
                "phase6",
                "--reuse-validated-phase6-equivalence",
                "--platform",
                "boarddb.json",
                "--seed",
                "3",
                "--workers",
                "8",
                "--out",
                "out",
            ]
        )
        self.assertEqual(args.seed, 3)
        self.assertEqual(args.workers, 8)
        self.assertTrue(args.reuse_validated_phase6_equivalence)
        validated = _build_parser().parse_args(
            [
                "experiment-stage",
                "phase7-validate",
                "result",
                "--shared",
                "shared",
                "--lookahead",
                "lookahead",
                "--phase6",
                "phase6",
                "--reuse-validated-phase6-equivalence",
                "--platform",
                "boarddb.json",
                "--seed",
                "3",
                "--workers",
                "8",
                "--route-channel-width",
                "300",
            ]
        )
        self.assertEqual(
            (validated.seed, validated.workers, validated.route_channel_width),
            (3, 8, 300),
        )
        self.assertTrue(validated.reuse_validated_phase6_equivalence)

    def test_phase6_equivalence_reuse_requires_managed_validation_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            object_root = Path(temporary) / ("a" * 64)
            output = object_root / "output"
            output.mkdir(parents=True)
            marker = output / "marker.json"
            marker.write_text("{}\n", encoding="utf-8")
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
            checkpoint = {
                "schema": "emuflow.experiment-checkpoint/v2",
                "status": "pass",
                "execution_key": "a" * 64,
                "node_id": "phase6-baseline",
                "stage": "phase6",
                "provider": "baseline",
                "physical_seed": None,
                "dependency_keys": {},
                "storage": "managed",
                "output_immutable": True,
                "output_dir": str(output.resolve()),
                "expected_artifacts": [
                    {
                        "path": "marker.json",
                        "role": "evidence-critical",
                        "retention": "required",
                    }
                ],
                "artifacts": {
                    "marker.json": {
                        "kind": "file",
                        "sha256": digest,
                        "bytes": marker.stat().st_size,
                    }
                },
            }
            write_json(object_root / "checkpoint.json", checkpoint)
            validation_key = "b" * 64
            write_json(
                object_root / "validations" / f"{validation_key}.json",
                {
                    "schema": "emuflow.experiment-validation/v1",
                    "execution_key": "a" * 64,
                    "validation_key": validation_key,
                    "status": "pass",
                },
            )
            os.chmod(marker, 0o444)
            os.chmod(output, 0o555)
            with mock.patch(
                "emuflow.experiment_dag.validate_experiment_checkpoint",
                wraps=experiment_dag.validate_experiment_checkpoint,
            ) as validator:
                self.assertEqual(
                    _validate_managed_phase6_checkpoint(output)["stage"], "phase6"
                )
            self.assertEqual(validator.call_count, 1)
            self.assertFalse(validator.call_args.kwargs["verify_artifact_content"])
            os.chmod(output, 0o755)
            with self.assertRaisesRegex(Exception, "writable|immutable"):
                _validate_managed_phase6_checkpoint(output)

    def test_phase6_managed_reuse_skips_upstream_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "experiment-phase6-report.json",
                {
                    "schema": "emuflow.experiment-phase6-checkpoint/v1",
                    "provider": "baseline",
                    "equivalence": {"status": "pass"},
                },
            )
            with mock.patch(
                "emuflow.experiment_stages._validate_managed_phase6_checkpoint"
            ) as managed, mock.patch(
                "emuflow.experiment_stages.validate_shared_phase1_5"
            ) as shared:
                result = validate_phase6_checkpoint(
                    root,
                    root / "shared",
                    None,
                    root / "boarddb.json",
                    validation_mode="validated-checkpoint-reuse",
                )
            managed.assert_called_once_with(root)
            shared.assert_not_called()
            self.assertEqual(result["provider"], "baseline")

    def test_cli_exposes_fine_grained_phase1_5_commands(self) -> None:
        parser = _build_parser()
        timing = parser.parse_args(
            [
                "experiment-stage",
                "timing-run",
                "--frontend",
                "frontend",
                "--clock-period",
                "clk=10",
                "--out",
                "timing",
            ]
        )
        self.assertEqual(timing.clock_period, ["clk=10"])
        shared = parser.parse_args(
            [
                "experiment-stage",
                "shared-materialize",
                "--frontend",
                "f",
                "--timing",
                "t",
                "--partition",
                "p",
                "--cut-timing",
                "c",
                "--route",
                "r",
                "--tdm",
                "d",
                "--platform",
                "board.json",
                "--out",
                "shared",
            ]
        )
        self.assertEqual(shared.experiment_stage_command, "shared-materialize")

    def test_baseline_phase6_does_not_require_lookahead(self) -> None:
        args = _build_parser().parse_args(
            [
                "experiment-stage",
                "phase6-run",
                "--shared",
                "shared",
                "--platform",
                "boarddb.json",
                "--provider",
                "baseline",
                "--out",
                "out",
            ]
        )
        self.assertIsNone(args.lookahead)
        self.assertEqual(args.provider, "baseline")

    def test_lookahead_can_bind_a_baseline_phase6_checkpoint(self) -> None:
        args = _build_parser().parse_args(
            [
                "experiment-stage",
                "lookahead-run",
                "--shared",
                "shared",
                "--baseline-phase6",
                "baseline-phase6",
                "--platform",
                "boarddb.json",
                "--out",
                "out",
            ]
        )
        self.assertEqual(args.baseline_phase6, Path("baseline-phase6"))

    def test_cli_exposes_resumed_physical_lookahead(self) -> None:
        args = _build_parser().parse_args(
            [
                "experiment-stage",
                "lookahead-resume",
                "--shared",
                "shared",
                "--baseline-phase6",
                "baseline",
                "--platform",
                "boarddb.json",
                "--seed",
                "2",
                "--workers",
                "6",
                "--reuse-validated-phase6-equivalence",
                "--out",
                "recovered",
            ]
        )
        self.assertEqual(args.experiment_stage_command, "lookahead-resume")
        self.assertEqual((args.seed, args.workers), (2, 6))
        self.assertTrue(args.reuse_validated_phase6_equivalence)

    def test_cli_exposes_distinct_physical_timing_databases(self) -> None:
        args = _build_parser().parse_args(
            [
                "multi-fpga",
                "physical",
                "--split",
                "split",
                "--platform",
                "boarddb.json",
                "--schedule",
                "schedule.json",
                "--path-database",
                "full.json",
                "--logic-path-database",
                "through-cut.json",
                "--out",
                "physical",
            ]
        )
        self.assertEqual(args.path_database, Path("full.json"))
        self.assertEqual(args.logic_path_database, Path("through-cut.json"))

    def test_resumed_lookahead_requires_only_a_physical_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovered"
            physical = root / "physical"
            physical.mkdir(parents=True)
            write_json(
                physical / "multi-fpga-physical-flow-report.json",
                {"schema": "placeholder"},
            )
            with mock.patch(
                "emuflow.experiment_stages._finish_physical_lookahead",
                return_value={"status": "pass"},
            ) as finish:
                report = resume_physical_lookahead(
                    Path("shared"),
                    Path("baseline"),
                    Path("platform"),
                    root,
                    seed=1,
                    workers=8,
                    region_count=4,
                    reuse_validated_phase6_equivalence=True,
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(finish.call_args.args[4], {"schema": "placeholder"})
            self.assertTrue(
                finish.call_args.kwargs["reuse_validated_phase6_equivalence"]
            )

            (root / "unrelated").write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "contain only physical"):
                resume_physical_lookahead(
                    Path("shared"),
                    Path("baseline"),
                    Path("platform"),
                    root,
                    seed=1,
                    workers=8,
                    region_count=4,
                )

    def test_resumed_lookahead_rebases_sealed_attempt_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovered"
            physical = root / "physical"
            physical.mkdir(parents=True)
            old_root = Path(temporary) / "attempt/output/physical"
            report = {
                "schema": "fixture",
                "fpgas": [
                    {
                        "fpga": "FPGA0",
                        "stages": {
                            "placement_ir": {
                                "output": str(old_root / "FPGA0/placement-ir.json")
                            },
                            "openparf_placement": {
                                "artifacts": {
                                    "vpr_placement": str(
                                        old_root / "FPGA0/openparf/design.place"
                                    )
                                }
                            },
                        },
                    }
                ],
                "external_source": "/research/example/input.json",
            }
            write_json(
                physical / "multi-fpga-physical-flow-report.json", report
            )
            with mock.patch(
                "emuflow.experiment_stages._finish_physical_lookahead",
                return_value={"status": "pass"},
            ) as finish:
                resume_physical_lookahead(
                    Path("shared"),
                    Path("baseline"),
                    Path("platform"),
                    root,
                    seed=1,
                    workers=8,
                    region_count=4,
                )
            rebased = finish.call_args.args[4]
            self.assertEqual(
                rebased["fpgas"][0]["stages"]["placement_ir"]["output"],
                str(physical.resolve() / "FPGA0/placement-ir.json"),
            )
            self.assertEqual(
                rebased["fpgas"][0]["stages"]["openparf_placement"]
                ["artifacts"]["vpr_placement"],
                str(physical.resolve() / "FPGA0/openparf/design.place"),
            )
            self.assertEqual(
                rebased["external_source"], "/research/example/input.json"
            )
            self.assertEqual(
                read_json(
                    physical / "multi-fpga-physical-flow-report.json"
                ),
                rebased,
            )


if __name__ == "__main__":
    unittest.main()
