from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.experiment_partition import (
    run_partition_checkpoint,
    validate_partition_checkpoint,
)
from emuflow.experiment_upstream import (
    MANAGED_DAG_VALIDATION_MODE,
    EXPERIMENT_PARTITION_SCHEMA,
    EXPERIMENT_TDM_SCHEMA,
    _portable_cut_timing_projection,
    materialize_shared_phase1_5,
    run_cut_timing_checkpoint,
    run_route_checkpoint,
    run_tdm_checkpoint,
    validate_materialized_shared_phase1_5,
    validate_tdm_checkpoint,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExperimentUpstreamTest(unittest.TestCase):
    def test_shared_checkpoint_calls_partition_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            timing = root / "timing"
            partition = root / "partition"
            cut_timing = root / "cut-timing"
            route = root / "route"
            tdm = root / "tdm"
            platform = root / "platform.json"
            output = root / "shared"
            partition_inputs = {
                "constraints_path": root / "partition-constraints.json",
                "route_constraints_path": root / "route-constraints.json",
                "tritonpart_solution": root / "initial.part",
                "patron_initial_assignment_path": root / "initial.json",
                "patron_physical_system_timing_path": root / "system-timing.json",
            }
            with (
                mock.patch(
                    "emuflow.experiment_upstream.validate_frontend_checkpoint"
                ),
                mock.patch(
                    "emuflow.experiment_upstream.validate_timing_checkpoint"
                ),
                mock.patch(
                    "emuflow.experiment_partition.validate_partition_checkpoint"
                ) as validate_partition,
                mock.patch(
                    "emuflow.experiment_upstream.validate_cut_timing_checkpoint"
                ),
                mock.patch(
                    "emuflow.experiment_upstream.validate_route_checkpoint"
                ) as validate_route,
                mock.patch(
                    "emuflow.experiment_upstream.validate_tdm_checkpoint"
                ) as validate_tdm,
                mock.patch(
                    "emuflow.experiment_upstream._prepare_empty_output",
                    return_value=output,
                ),
                mock.patch("emuflow.experiment_upstream._link_or_copy"),
                mock.patch(
                    "emuflow.experiment_upstream._sha256", return_value="0" * 64
                ),
                mock.patch("emuflow.experiment_upstream.write_json"),
                mock.patch(
                    "emuflow.experiment_upstream.validate_materialized_shared_phase1_5",
                    return_value={"status": "pass"},
                ),
            ):
                materialize_shared_phase1_5(
                    frontend,
                    timing,
                    partition,
                    cut_timing,
                    route,
                    tdm,
                    platform,
                    output,
                    **partition_inputs,
                )
            validate_partition.assert_called_once_with(
                frontend, timing, platform, partition, **partition_inputs
            )
            validate_route.assert_called_once_with(
                partition, cut_timing, platform, route,
                constraints_path=partition_inputs["route_constraints_path"],
            )
            validate_tdm.assert_called_once_with(
                route, platform, tdm,
                constraints_path=partition_inputs["route_constraints_path"],
            )

    def test_shared_cli_forwards_partition_validation_inputs(self) -> None:
        from emuflow.cli import main

        required = ("frontend", "timing", "partition", "cut-timing", "route", "tdm", "platform", "out")
        forwarded = {
            "constraints": "constraints_path",
            "route-constraints": "route_constraints_path",
            "tritonpart-solution": "tritonpart_solution",
            "patron-initial-assignment": "patron_initial_assignment_path",
            "patron-physical-system-timing": "patron_physical_system_timing_path",
        }
        argv = ["experiment-stage", "shared-materialize"]
        for option in (*required, *forwarded):
            argv.extend((f"--{option}", f"fixture-{option}"))
        with mock.patch("emuflow.cli.materialize_shared_phase1_5", return_value={}) as run:
            self.assertEqual(main(argv), 0)
        for option, keyword in forwarded.items():
            self.assertEqual(run.call_args.kwargs[keyword], Path(f"fixture-{option}"))

    def test_managed_route_and_tdm_write_directly_without_hash_or_self_replay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partition = root / "partition"
            cut_timing = root / "cut-timing"
            route = root / "route"
            tdm = root / "tdm"
            partition.mkdir()
            cut_timing.mkdir()
            (partition / "assignment.json").write_text("{}", encoding="utf-8")
            (cut_timing / "cut-timing-paths.json").write_text(
                "{}", encoding="utf-8"
            )

            def phase4(*_args, **kwargs):
                self.assertTrue(kwargs["managed_storage"])
                (route / "routes.json").write_text("{}", encoding="utf-8")
                (route / "phase4_report.json").write_text("{}", encoding="utf-8")
                return {"status": "pass"}

            def phase5(*_args, **kwargs):
                self.assertTrue(kwargs["managed_storage"])
                (tdm / "schedule.json").write_text("{}", encoding="utf-8")
                (tdm / "phase5_report.json").write_text("{}", encoding="utf-8")
                return {"status": "pass"}

            with (
                mock.patch(
                    "emuflow.experiment_upstream._sha256",
                    side_effect=AssertionError("hashing entered managed hot path"),
                ),
                mock.patch(
                    "emuflow.experiment_upstream.run_phase4", side_effect=phase4
                ),
                mock.patch(
                    "emuflow.experiment_upstream.run_phase5", side_effect=phase5
                ),
                mock.patch(
                    "emuflow.experiment_upstream.validate_route_checkpoint"
                ) as route_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_tdm_checkpoint"
                ) as tdm_validate,
            ):
                route_report = run_route_checkpoint(
                    partition,
                    cut_timing,
                    root / "platform.json",
                    route,
                    managed_storage=True,
                    managed_dag_node=True,
                )
                tdm_report = run_tdm_checkpoint(
                    route,
                    root / "platform.json",
                    tdm,
                    managed_storage=True,
                    managed_dag_node=True,
                )
            route_validate.assert_not_called()
            tdm_validate.assert_not_called()
            self.assertEqual(
                route_report["validation_mode"], MANAGED_DAG_VALIDATION_MODE
            )
            self.assertEqual(
                tdm_report["validation_mode"], MANAGED_DAG_VALIDATION_MODE
            )
            self.assertFalse(any("sha256" in key for key in route_report))
            self.assertFalse(any("sha256" in key for key in tdm_report))

    def test_managed_shared_materialization_performs_no_hash_or_deep_replay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = {
                name: root / name
                for name in (
                    "frontend",
                    "timing",
                    "partition",
                    "cut-timing",
                    "route",
                    "tdm",
                )
            }
            files = {
                "frontend": ("phase1/design.emuir.json", "phase1/phase1_report.json"),
                "timing": ("path-database.json", "partition-net-weights.json"),
                "partition": ("clusters.json", "assignment.json", "phase3_report.json"),
                "cut-timing": (
                    "cut-timing-paths.json",
                    "cut-segment-qualification.json",
                ),
                "route": ("routes.json", "phase4_report.json"),
                "tdm": ("schedule.json", "phase5_report.json"),
            }
            for stage, relative_files in files.items():
                for relative in relative_files:
                    path = roots[stage] / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"{stage}:{relative}\n", encoding="utf-8")

            def managed(_root, *, expected_stage):
                return {
                    "execution_key": hashlib.sha256(
                        expected_stage.encode("utf-8")
                    ).hexdigest()
                }

            with (
                mock.patch(
                    "emuflow.experiment_upstream._managed_checkpoint",
                    side_effect=managed,
                ),
                mock.patch(
                    "emuflow.experiment_upstream._sha256",
                    side_effect=AssertionError("hashing entered managed hot path"),
                ),
                mock.patch(
                    "emuflow.experiment_upstream.Platform.load",
                    return_value=SimpleNamespace(name="fixture-platform"),
                ),
                mock.patch(
                    "emuflow.experiment_upstream.validate_frontend_checkpoint"
                ) as frontend_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_timing_checkpoint"
                ) as timing_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_partition_checkpoint"
                ) as partition_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_cut_timing_checkpoint"
                ) as cut_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_route_checkpoint"
                ) as route_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_tdm_checkpoint"
                ) as tdm_validate,
                mock.patch(
                    "emuflow.experiment_upstream.validate_shared_phase1_5"
                ) as shared_validate,
            ):
                report = materialize_shared_phase1_5(
                    roots["frontend"],
                    roots["timing"],
                    roots["partition"],
                    roots["cut-timing"],
                    roots["route"],
                    roots["tdm"],
                    root / "platform.json",
                    root / "shared",
                    managed_dag_node=True,
                )
            for validator in (
                frontend_validate,
                timing_validate,
                partition_validate,
                cut_validate,
                route_validate,
                tdm_validate,
                shared_validate,
            ):
                validator.assert_not_called()
            self.assertEqual(
                report["validation_mode"], "managed-dependency-certificates"
            )
            self.assertTrue(
                all("sha256" not in record for record in report["artifacts"].values())
            )

            assignment = root / "shared/partition/assignment.json"
            assignment.write_text("different-size", encoding="utf-8")
            with mock.patch(
                "emuflow.experiment_upstream.Platform.load",
                return_value=SimpleNamespace(name="fixture-platform"),
            ), self.assertRaisesRegex(ValidationError, "artifact certificate"):
                validate_materialized_shared_phase1_5(
                    root / "shared",
                    root / "platform.json",
                    managed_dag_node=True,
                )

    def test_managed_partition_hot_path_performs_no_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            timing = root / "timing"
            output = root / "partition"
            (frontend / "phase1").mkdir(parents=True)
            timing.mkdir()
            (frontend / "phase1/design.emuir.json").write_text(
                "{}", encoding="utf-8"
            )
            (timing / "partition-net-weights.json").write_text(
                "{}", encoding="utf-8"
            )
            (timing / "path-database.json").write_text(
                "{}", encoding="utf-8"
            )

            def fake_phase3(*_args, **_kwargs):
                for name in (
                    "assignment.json",
                    "clusters.json",
                    "phase3_report.json",
                ):
                    (output / name).write_text("{}", encoding="utf-8")
                return {
                    "status": "pass",
                    "validation": {
                        "status": "pass",
                        "combinational_cut_nets": 0,
                    },
                    "mfspart_post_refinement": {
                        "refinement": {
                            "runtime": {
                                "optimizer_wall_seconds": 1.0,
                                "candidate_check_wall_seconds": 0.1,
                            }
                        }
                    },
                }

            with (
                mock.patch(
                    "emuflow.experiment_partition.run_phase3",
                    side_effect=fake_phase3,
                ),
                mock.patch(
                    "emuflow.experiment_partition._sha256",
                    side_effect=AssertionError("hashing entered hot path"),
                ),
            ):
                report = run_partition_checkpoint(
                    frontend,
                    timing,
                    root / "platform.json",
                    output,
                    provider="tritonpart",
                    managed_dag_node=True,
                )
                checked = validate_partition_checkpoint(
                    frontend,
                    timing,
                    root / "platform.json",
                    output,
                    expected_provider="tritonpart",
                    online_validation=True,
                )
        self.assertFalse(any("sha256" in key for key in report))
        self.assertEqual(checked["status"], "pass")
        self.assertLessEqual(
            checked["runtime"]["validator_wall_seconds"], 1.0
        )

    def test_partition_run_rejects_solution_for_non_tritonpart_provider(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValidationError, "requires provider=tritonpart"
        ):
            run_partition_checkpoint(
                Path("unused-frontend"),
                Path("unused-timing"),
                Path("unused-platform"),
                Path("unused-output"),
                provider="greedy",
                tritonpart_solution=Path("candidate.part.3"),
            )

    @mock.patch("emuflow.experiment_upstream.validate_cut_timing_checkpoint")
    @mock.patch("emuflow.experiment_upstream._sha256", return_value="0" * 64)
    @mock.patch("emuflow.experiment_upstream.project_sta_path_database")
    @mock.patch("emuflow.experiment_upstream.build_cut_segment_qualification")
    @mock.patch("emuflow.experiment_upstream.validate_timing_checkpoint")
    def test_cut_timing_projects_complete_prepartition_database(
        self,
        _validate_timing,
        build_qualification,
        project,
        _sha,
        _validate_cut,
    ) -> None:
        build_qualification.return_value = {"status": "pass"}
        project.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            timing = root / "timing"
            partition = root / "partition"
            output = root / "cut"
            (frontend / "phase1").mkdir(parents=True)
            timing.mkdir()
            partition.mkdir()
            (frontend / "phase1/design.emuir.json").write_text(
                "{}", encoding="utf-8"
            )
            complete = timing / "path-database.json"
            complete.write_text("{}", encoding="utf-8")
            (partition / "assignment.json").write_text(
                json.dumps(
                    {
                        "schema": "emuflow.partition-assignment/v1",
                        "cut_nets": [{"net": "n0"}],
                    }
                ),
                encoding="utf-8",
            )

            run_cut_timing_checkpoint(
                frontend,
                timing,
                partition,
                output,
                clocks={"clk": 10.0},
            )

            self.assertEqual(project.call_args.args[0], complete.resolve())
            self.assertNotEqual(
                project.call_args.args[0], output / "cut-segment-qualification.json"
            )
            self.assertEqual(
                build_qualification.call_args.args[:3],
                (
                    (frontend / "phase1/design.emuir.json").resolve(),
                    (partition / "assignment.json").resolve(),
                    complete.resolve(),
                ),
            )
            self.assertEqual(
                _validate_cut.call_args.args[:3],
                (frontend, timing, partition),
            )

    def test_cut_timing_projection_provenance_is_relocation_portable(
        self,
    ) -> None:
        artifact = {
            "schema": "emuflow.sta-paths/v1",
            "source": {
                "provider": "partition-projected-sta-paths-v1",
                "input_sha256": "a" * 64,
            },
            "paths": [{"id": "p0"}],
        }
        relocated = {
            **artifact,
            "source": {
                **artifact["source"],
                "input": "/cache/objects/key/output/cut-path-database.json",
            },
        }
        self.assertEqual(
            _portable_cut_timing_projection(
                artifact,
                "cut-path-database.json",
                database_sha256="a" * 64,
            ),
            _portable_cut_timing_projection(
                relocated,
                "cut-path-database.json",
                database_sha256="a" * 64,
            ),
        )
        legacy = {
            **artifact,
            "source": {
                "provider": "partition-projected-sta-paths-v1",
                "input": "/cache/staging/key/output/cut-path-database.json",
            },
        }
        self.assertEqual(
            _portable_cut_timing_projection(
                legacy, "cut-path-database.json", database_sha256="a" * 64
            )["source"]["input"],
            "cut-path-database.json",
        )

        tampered = {**relocated, "paths": [{"id": "different"}]}
        self.assertNotEqual(
            _portable_cut_timing_projection(
                artifact,
                "cut-path-database.json",
                database_sha256="a" * 64,
            ),
            _portable_cut_timing_projection(
                tampered,
                "cut-path-database.json",
                database_sha256="a" * 64,
            ),
        )
        with self.assertRaisesRegex(
            ValidationError, "projection source is invalid"
        ):
            _portable_cut_timing_projection(
                {
                    **artifact,
                    "source": {**artifact["source"], "input_sha256": "b" * 64},
                },
                "cut-path-database.json",
                database_sha256="a" * 64,
            )

    def _tdm_fixture(
        self,
        root: Path,
        *,
        provider: str,
        optimization_provider: str | None = None,
    ) -> tuple[Path, Path, Path]:
        route = root / "route"
        tdm = root / "tdm"
        platform = root / "platform.json"
        route.mkdir()
        tdm.mkdir()
        (route / "routes.json").write_text("{}", encoding="utf-8")
        platform.write_text("{}", encoding="utf-8")
        (tdm / "schedule.json").write_text(
            json.dumps({"provider": provider}), encoding="utf-8"
        )
        phase5 = {"provider": provider}
        if optimization_provider is not None:
            phase5["optimization_provider"] = optimization_provider
        (tdm / "phase5_report.json").write_text(
            json.dumps(phase5), encoding="utf-8"
        )
        report = {
            "schema": EXPERIMENT_TDM_SCHEMA,
            "status": "pass",
            "routes_sha256": _sha256(route / "routes.json"),
            "platform_sha256": _sha256(platform),
            "schedule_sha256": _sha256(tdm / "schedule.json"),
            "phase5_report_sha256": _sha256(tdm / "phase5_report.json"),
            "phase5": phase5,
        }
        (tdm / "experiment-tdm-report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return route, platform, tdm

    def _partition_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        frontend = root / "frontend"
        timing = root / "timing"
        partition = root / "partition"
        platform = root / "platform.json"
        (frontend / "phase1").mkdir(parents=True)
        timing.mkdir()
        partition.mkdir()
        (frontend / "phase1/design.emuir.json").write_text(
            "{}", encoding="utf-8"
        )
        (timing / "partition-net-weights.json").write_text(
            "{}", encoding="utf-8"
        )
        platform.write_text("{}", encoding="utf-8")
        (partition / "clusters.json").write_text("{}", encoding="utf-8")
        assignment = {
            "provider_metadata": {
                "seed_attempts": [
                    {"mode": "timing_weighted", "seed": 0},
                    {"mode": "timing_weighted", "seed": 1},
                    {"mode": "unweighted_baseline", "seed": 0},
                ],
                "balance_repair": {"enabled": True, "summary": {}},
            }
        }
        (partition / "assignment.json").write_text(
            json.dumps(assignment), encoding="utf-8"
        )
        (partition / "phase3_report.json").write_text("{}", encoding="utf-8")
        report = {
            "schema": EXPERIMENT_PARTITION_SCHEMA,
            "status": "pass",
            "provider": "tritonpart",
            "seed": 0,
            "seed_attempts": 2,
            "repair_balance": True,
            "emuir_sha256": _sha256(
                frontend / "phase1/design.emuir.json"
            ),
            "platform_sha256": _sha256(platform),
            "weights_sha256": _sha256(
                timing / "partition-net-weights.json"
            ),
            "assignment_sha256": _sha256(partition / "assignment.json"),
            "clusters_sha256": _sha256(partition / "clusters.json"),
            "phase3_report_sha256": _sha256(
                partition / "phase3_report.json"
            ),
            "constraints_sha256": None,
            "route_constraints_sha256": None,
        }
        (partition / "experiment-partition-report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return frontend, timing, platform, partition

    @mock.patch("emuflow.experiment_partition.validate_phase3")
    def test_partition_validator_binds_seed_sweep_and_repair(
        self, validate_phase3
    ) -> None:
        validate_phase3.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            frontend, timing, platform, partition = self._partition_fixture(
                Path(temporary)
            )
            checked = validate_partition_checkpoint(
                frontend,
                timing,
                platform,
                partition,
                expected_provider="tritonpart",
                expected_seed=0,
                expected_seed_attempts=2,
                expected_repair_balance=True,
            )
            self.assertEqual(checked["status"], "pass")

    @mock.patch("emuflow.experiment_partition.validate_phase3")
    def test_partition_validator_seals_optional_constraints(
        self, validate_phase3
    ) -> None:
        validate_phase3.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend, timing, platform, partition = self._partition_fixture(root)
            constraints = root / "constraints.json"
            constraints.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.partition-constraints/v1",
                        "fixed": [],
                    }
                ),
                encoding="utf-8",
            )
            report_path = partition / "experiment-partition-report.json"
            report = json.loads(report_path.read_text())
            report["constraints_sha256"] = _sha256(constraints)
            report_path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(
                validate_partition_checkpoint(
                    frontend, timing, platform, partition,
                    constraints_path=constraints,
                )["status"],
                "pass",
            )
            constraints.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "constraints seal"):
                validate_partition_checkpoint(
                    frontend, timing, platform, partition,
                    constraints_path=constraints,
                )

    @mock.patch("emuflow.experiment_partition.validate_phase3")
    def test_partition_validator_seals_precomputed_tritonpart_solution(
        self, validate_phase3
    ) -> None:
        validate_phase3.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend, timing, platform, partition = self._partition_fixture(root)
            solution = root / "candidate.part.3"
            solution.write_text("0\n1\n2\n", encoding="utf-8")
            report_path = partition / "experiment-partition-report.json"
            report = json.loads(report_path.read_text())
            report["tritonpart_solution_sha256"] = _sha256(solution)
            report_path.write_text(json.dumps(report), encoding="utf-8")

            self.assertEqual(
                validate_partition_checkpoint(
                    frontend,
                    timing,
                    platform,
                    partition,
                    tritonpart_solution=solution,
                )["status"],
                "pass",
            )
            solution.write_text("0\n0\n0\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "TritonPart solution seal"
            ):
                validate_partition_checkpoint(
                    frontend,
                    timing,
                    platform,
                    partition,
                    tritonpart_solution=solution,
                )

    @mock.patch("emuflow.experiment_partition.validate_phase3")
    def test_partition_validator_rejects_resealed_policy_mismatch(
        self, validate_phase3
    ) -> None:
        validate_phase3.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            frontend, timing, platform, partition = self._partition_fixture(
                Path(temporary)
            )
            report_path = partition / "experiment-partition-report.json"
            report = json.loads(report_path.read_text())
            report["seed_attempts"] = 3
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "seed-attempt report disagrees"
            ):
                validate_partition_checkpoint(
                    frontend, timing, platform, partition
                )

            report["seed_attempts"] = 2
            report["repair_balance"] = False
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "balance-repair report disagrees"
            ):
                validate_partition_checkpoint(
                    frontend, timing, platform, partition
                )

    @mock.patch("emuflow.experiment_partition.validate_phase3")
    def test_partition_validator_binds_static_exact_cut_policy(
        self, validate_phase3
    ) -> None:
        validate_phase3.return_value = {
            "status": "pass",
            "cut_mode": "static-exact-combinational",
        }
        with tempfile.TemporaryDirectory() as temporary:
            frontend, timing, platform, partition = self._partition_fixture(
                Path(temporary)
            )
            clusters_path = partition / "clusters.json"
            clusters_path.write_text(
                json.dumps(
                    {
                        "policy": {
                            "cut_mode": "static-exact-combinational",
                            "max_cross_fpga_dependency_depth": 1,
                            "comb_segment_budget_slots": 3,
                        }
                    }
                ),
                encoding="utf-8",
            )
            report_path = partition / "experiment-partition-report.json"
            report = json.loads(report_path.read_text())
            report.update(
                {
                    "clusters_sha256": _sha256(clusters_path),
                    "cut_mode": "static-exact-combinational",
                    "max_cross_fpga_dependency_depth": 1,
                    "comb_segment_budget_slots": 3,
                }
            )
            report_path.write_text(json.dumps(report), encoding="utf-8")
            checked = validate_partition_checkpoint(
                frontend,
                timing,
                platform,
                partition,
                expected_cut_mode="static-exact-combinational",
                expected_max_cross_fpga_dependency_depth=1,
                expected_comb_segment_budget_slots=3,
            )
            self.assertEqual(checked["cut_mode"], "static-exact-combinational")

            report["comb_segment_budget_slots"] = 2
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError,
                "comb-segment-budget-slots seal disagrees",
            ):
                validate_partition_checkpoint(
                    frontend, timing, platform, partition
                )

    @mock.patch("emuflow.experiment_partition.validate_phase3")
    def test_partition_validator_enforces_actual_exact_cut_evidence(
        self, validate_phase3
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            frontend, timing, platform, partition = self._partition_fixture(
                Path(temporary)
            )
            clusters_path = partition / "clusters.json"
            clusters_path.write_text(
                json.dumps(
                    {
                        "policy": {
                            "cut_mode": "static-exact-combinational",
                            "max_cross_fpga_dependency_depth": 1,
                            "comb_segment_budget_slots": 1,
                        }
                    }
                ),
                encoding="utf-8",
            )
            report_path = partition / "experiment-partition-report.json"
            report = json.loads(report_path.read_text())
            report.update(
                {
                    "clusters_sha256": _sha256(clusters_path),
                    "cut_mode": "static-exact-combinational",
                    "max_cross_fpga_dependency_depth": 1,
                    "comb_segment_budget_slots": 1,
                    "minimum_combinational_cut_nets": 1,
                }
            )
            report_path.write_text(json.dumps(report), encoding="utf-8")

            validate_phase3.return_value = {
                "status": "pass",
                "cut_mode": "static-exact-combinational",
                "combinational_cut_nets": 0,
            }
            with self.assertRaisesRegex(
                ValidationError, "fewer combinational cut nets"
            ):
                validate_partition_checkpoint(
                    frontend,
                    timing,
                    platform,
                    partition,
                    expected_minimum_combinational_cut_nets=1,
                )

            validate_phase3.return_value[
                "combinational_cut_nets"
            ] = 1
            report["static_exact_combinational_cut_exercised"] = True
            report_path.write_text(json.dumps(report), encoding="utf-8")
            checked = validate_partition_checkpoint(
                frontend,
                timing,
                platform,
                partition,
                expected_minimum_combinational_cut_nets=1,
            )
            self.assertEqual(checked["combinational_cut_nets"], 1)

            report["static_exact_combinational_cut_exercised"] = False
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "exercise status disagrees"
            ):
                validate_partition_checkpoint(
                    frontend,
                    timing,
                    platform,
                    partition,
                    expected_minimum_combinational_cut_nets=1,
                )

    @mock.patch("emuflow.experiment_upstream.validate_phase5")
    def test_tdm_validator_binds_academic_optimization_provider(
        self, validate_phase5
    ) -> None:
        validate_phase5.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            route, platform, tdm = self._tdm_fixture(
                Path(temporary),
                provider="lagrangian-kkt-ratio-aware-list-schedule-v1",
                optimization_provider="aspdac26-timing-dag-lagrangian-v1",
            )
            checked = validate_tdm_checkpoint(
                route,
                platform,
                tdm,
                expected_provider="aspdac26-timing-dag-lagrangian-v1",
            )
            self.assertEqual(checked["status"], "pass")
            with self.assertRaisesRegex(
                ValidationError, "provider contract disagrees"
            ):
                validate_tdm_checkpoint(
                    route,
                    platform,
                    tdm,
                    expected_provider=(
                        "lagrangian-kkt-ratio-aware-list-schedule-v1"
                    ),
                )

    @mock.patch("emuflow.experiment_upstream.validate_phase5")
    def test_tdm_validator_preserves_baseline_provider_contract(
        self, validate_phase5
    ) -> None:
        validate_phase5.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            route, platform, tdm = self._tdm_fixture(
                Path(temporary), provider="static-tdm-v2"
            )
            checked = validate_tdm_checkpoint(
                route,
                platform,
                tdm,
                expected_provider="static-tdm-v2",
            )
            self.assertEqual(checked["status"], "pass")

if __name__ == "__main__":
    unittest.main()
