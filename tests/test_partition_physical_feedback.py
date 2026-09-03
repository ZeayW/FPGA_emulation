import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.partition import (
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
)
from emuflow.partition_physical_feedback import (
    build_partition_physical_feedback,
    validate_partition_physical_feedback,
    validate_partition_physical_feedback_seal,
)
from emuflow.partition_pressure import (
    build_partition_pressure_model,
    evaluate_partition_pressure,
    run_partition_pressure_native,
    validate_partition_pressure_native_bundle,
)
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from emuflow.io import read_json, write_json
from emuflow.experiment_partition import (
    run_partition_checkpoint,
    validate_partition_checkpoint,
)
from emuflow.phase3 import run_phase3
from tests.native_build import hop_partition_refiner, patron_refiner
from tests.test_partition_pressure import _endpoint, _ir
from tests.test_phase5 import _link, _platform_value


class PartitionPhysicalFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = _ir()
        value = _platform_value(
            "physical_feedback_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=2, latency=1)],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 100, "ff": 100}
        self.platform = Platform.from_dict(value)
        self.constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        self.clusters = build_clusters(self.ir, self.constraints)
        self.by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in self.clusters["clusters"]
        }
        self.assignment = build_partition_assignment(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            {
                self.by_instance["u0"]: "a",
                self.by_instance["u1"]: "b",
                self.by_instance["u2"]: "a",
                self.by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=1,
        )
        self.route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            self.platform,
        )
        self.timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 20.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u0", "O"),
                    "endpoint": _endpoint("u1", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["critical"],
                    "normalized_slack": 0.075,
                },
                {
                    "id": "p-relaxed",
                    "startpoint": _endpoint("u2", "O"),
                    "endpoint": _endpoint("u3", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 20.0,
                    "fixed_delay_ns": 0.0,
                    "path_nets": ["relaxed"],
                    "normalized_slack": 1.0,
                },
            ],
        }
        self.model = build_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
        )
        parts = self.assignment["cluster_assignment"]
        residuals = {"p-critical": 5.0, "p-relaxed": 2.0}
        paths = []
        for pressure_path in self.model["paths"]:
            path_id = pressure_path["path"]
            source = parts[pressure_path["start_cluster"]]
            sink = parts[pressure_path["end_cluster"]]
            predicted_logic = (
                pressure_path["clock_period_ns"]
                - pressure_path["base_slack_ns"]
            )
            paths.append(
                {
                    "path": path_id,
                    "path_scope": "cross-fpga",
                    "target_period_ns": pressure_path["clock_period_ns"],
                    "logical_fpga_sequence": [source, sink],
                    "partition_chain_exact": True,
                    "preplacement_fixed_delay_ns": 1.0,
                    "physical_logic_delay_bound_ns": predicted_logic + 1.0,
                    "physical_interface_delay_bound_ns": residuals[path_id],
                }
            )
        self.system_timing = {
            "schema": "emuflow.system-timing/v2",
            "design": self.model["design"],
            "platform": self.model["platform"],
            "paths": paths,
        }
        self.feedback = build_partition_physical_feedback(
            self.model, self.assignment, self.system_timing
        )

    def test_feedback_is_exactly_reconstructed_and_tamper_evident(self) -> None:
        checked = validate_partition_physical_feedback(
            self.model,
            self.assignment,
            self.system_timing,
            self.feedback,
        )
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(checked["source_paths"], 2)
        self.assertEqual(checked["positive_residual_paths"], 2)
        self.assertEqual(
            [record["positive_residual_ns"] for record in self.feedback["paths"]],
            [5.0, 2.0],
        )

        tampered = copy.deepcopy(self.feedback)
        tampered["paths"][0]["positive_residual_ns"] += 0.5
        with self.assertRaisesRegex(ValidationError, "reconstruction"):
            validate_partition_physical_feedback(
                self.model,
                self.assignment,
                self.system_timing,
                tampered,
            )

        malformed = copy.deepcopy(self.feedback)
        malformed["source_assignment_sha256"] = "z" * 64
        with self.assertRaisesRegex(ValidationError, "lowercase SHA-256"):
            validate_partition_physical_feedback_seal(self.model, malformed)

    def test_feedback_rejects_incomplete_or_assignment_inconsistent_sources(
        self,
    ) -> None:
        incomplete = copy.deepcopy(self.system_timing)
        incomplete["paths"].pop()
        with self.assertRaisesRegex(ValidationError, "complete.*population"):
            build_partition_physical_feedback(
                self.model, self.assignment, incomplete
            )

        inconsistent = copy.deepcopy(self.system_timing)
        inconsistent["paths"][0]["logical_fpga_sequence"] = ["b", "a"]
        with self.assertRaisesRegex(ValidationError, "assignment"):
            build_partition_physical_feedback(
                self.model, self.assignment, inconsistent
            )

    def test_feedback_applies_only_to_the_observed_endpoint_pair(self) -> None:
        observed = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.assignment["cluster_assignment"],
            physical_feedback=self.feedback,
            physical_feedback_scale=0.25,
        )
        observed_by_path = {
            record["path"]: record for record in observed["paths"]
        }
        self.assertEqual(
            observed_by_path["p-critical"]["physical_feedback_delay_ns"],
            1.25,
        )

        reversed_assignment = dict(self.assignment["cluster_assignment"])
        reversed_assignment[self.by_instance["u0"]] = "b"
        reversed_assignment[self.by_instance["u1"]] = "a"
        reversed_result = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            reversed_assignment,
            physical_feedback=self.feedback,
            physical_feedback_scale=0.25,
        )
        reversed_by_path = {
            record["path"]: record for record in reversed_result["paths"]
        }
        self.assertEqual(
            reversed_by_path["p-critical"]["physical_feedback_delay_ns"],
            0.0,
        )

    def test_v11_native_matches_independent_objective_oracle(self) -> None:
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.assignment,
            executable=str(patron_refiner()),
            max_moves=8,
            flow_refinement=True,
            physical_feedback=self.feedback,
            physical_feedback_scale=0.25,
        )
        self.assertEqual(trace["schema"], "emuflow.partition-pressure-trace/v11")
        self.assertEqual(trace["mode"], "endpoint-exact-critical-flow-v11")
        checked = validate_partition_pressure_native_bundle(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            self.model,
            self.assignment,
            final,
            trace,
            self.feedback,
            0.25,
        )
        self.assertEqual(checked["status"], "pass")

        with self.assertRaisesRegex(ValidationError, "bounds|configuration"):
            validate_partition_pressure_native_bundle(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.timing,
                self.route_constraints,
                self.model,
                self.assignment,
                final,
                trace,
                self.feedback,
                0.5,
            )

    def test_phase3_persists_and_revalidates_v11_feedback_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            routes_path = root / "route-constraints.json"
            initial_path = root / "initial.json"
            system_timing_path = root / "system-timing.json"
            write_json(ir_path, self.ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(timing_path, self.timing)
            write_json(routes_path, self.route_constraints)
            write_json(initial_path, self.assignment)
            write_json(system_timing_path, self.system_timing)

            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                cut_mode="sequential-only",
                route_constraints_path=routes_path,
                timing_database_path=timing_path,
                hop_refiner=str(hop_partition_refiner()),
                patron_refiner=str(patron_refiner()),
                patron_max_moves=8,
                patron_flow_refinement=True,
                patron_initial_assignment_path=initial_path,
                patron_physical_system_timing_path=system_timing_path,
                patron_physical_feedback_scale=0.25,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["provider"],
                "patron-endpoint-exact-flow-native-v11",
            )
            self.assertEqual(
                report["algorithm_validation"]["physical_feedback"][
                    "positive_residual_paths"
                ],
                2,
            )
            self.assertEqual(
                read_json(root / "phase3/patron/physical_feedback.json"),
                self.feedback,
            )
            self.assertTrue(
                (
                    root
                    / "phase3/patron/physical_feedback_source_assignment.json"
                ).is_file()
            )

    def test_partition_checkpoint_seals_v11_physical_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            timing_root = root / "timing"
            (frontend / "phase1").mkdir(parents=True)
            timing_root.mkdir()
            platform_path = root / "platform.json"
            routes_path = root / "route-constraints.json"
            initial_path = root / "initial.json"
            system_timing_path = root / "system-timing.json"
            write_json(frontend / "phase1/design.emuir.json", self.ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(timing_root / "path-database.json", self.timing)
            write_json(
                timing_root / "partition-net-weights.json",
                {
                    "schema": "emuflow.partition-net-weights/v1",
                    "weights": {},
                },
            )
            write_json(routes_path, self.route_constraints)
            write_json(initial_path, self.assignment)
            write_json(system_timing_path, self.system_timing)
            output = root / "partition"

            with patch(
                "emuflow.experiment_partition.validate_timing_checkpoint",
                return_value={"status": "pass"},
            ):
                report = run_partition_checkpoint(
                    frontend,
                    timing_root,
                    platform_path,
                    output,
                    provider="patron",
                    cut_mode="sequential-only",
                    seed=1,
                    route_constraints_path=routes_path,
                    min_used_fpgas=2,
                    balance_tolerance=1.0,
                    hop_refiner=str(hop_partition_refiner()),
                    patron_refiner=str(patron_refiner()),
                    patron_max_moves=8,
                    patron_flow_refinement=True,
                    patron_initial_assignment_path=initial_path,
                    patron_physical_system_timing_path=system_timing_path,
                    patron_physical_feedback_scale=0.25,
                )
                checked = validate_partition_checkpoint(
                    frontend,
                    timing_root,
                    platform_path,
                    output,
                    route_constraints_path=routes_path,
                    expected_provider="patron",
                    expected_seed=1,
                    patron_initial_assignment_path=initial_path,
                    expected_patron_flow_refinement=True,
                    expected_patron_max_moves=8,
                    patron_physical_system_timing_path=system_timing_path,
                    expected_patron_physical_feedback_scale=0.25,
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["patron_max_moves"], 8)
            self.assertEqual(checked["status"], "pass")
            self.assertEqual(
                checked["algorithm_validation"]["physical_feedback"][
                    "positive_residual_paths"
                ],
                2,
            )

            changed = copy.deepcopy(self.system_timing)
            changed["paths"][0]["physical_logic_delay_bound_ns"] += 0.1
            write_json(system_timing_path, changed)
            with patch(
                "emuflow.experiment_partition.validate_timing_checkpoint",
                return_value={"status": "pass"},
            ), self.assertRaisesRegex(
                ValidationError, "system-timing seal"
            ):
                validate_partition_checkpoint(
                    frontend,
                    timing_root,
                    platform_path,
                    output,
                    route_constraints_path=routes_path,
                    patron_initial_assignment_path=initial_path,
                    patron_physical_system_timing_path=system_timing_path,
                    expected_patron_physical_feedback_scale=0.25,
                )


if __name__ == "__main__":
    unittest.main()
