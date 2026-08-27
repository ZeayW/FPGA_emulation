import copy
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
)
from emuflow.partition_pressure import (
    build_partition_pressure_model,
    evaluate_partition_pressure,
    refine_partition_pressure_exhaustive,
    run_partition_pressure_reference,
    run_partition_pressure_native,
    validate_partition_pressure_model,
    validate_partition_pressure_native_bundle,
    validate_partition_pressure_trace,
    validate_partition_pressure_native_against_exhaustive,
    validate_partition_pressure_scalable_trace,
)
from emuflow.phase3 import promote_patron_baseline, run_phase3
from emuflow.platform import Platform
from emuflow.io import read_json, write_json
from emuflow.routing import normalize_route_constraints
from tests.test_phase5 import _link, _platform_value
from tests.native_build import patron_refiner


def _endpoint(instance: str, port: str) -> dict:
    return {"instance": instance, "port": port, "bit": 0}


def _ir() -> EmuIR:
    instances = [
        {
            "id": f"u{index}",
            "type": "LUT1",
            "resources": {"lut": 1},
        }
        for index in range(4)
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure",
                "top": "pressure",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": instances,
            "nets": [
                {
                    "id": "critical",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u0", "O")],
                    "sinks": [_endpoint("u1", "I")],
                },
                {
                    "id": "relaxed",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u2", "O")],
                    "sinks": [_endpoint("u3", "I")],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
    )


def _chain_ir(instances: int) -> EmuIR:
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure_chain",
                "top": "pressure_chain",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": [
                {
                    "id": f"u{index:05d}",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                }
                for index in range(instances)
            ],
            "nets": [
                {
                    "id": f"n{index:05d}",
                    "cut_class": "register_output",
                    "drivers": [_endpoint(f"u{index:05d}", "O")],
                    "sinks": [_endpoint(f"u{index + 1:05d}", "I")],
                }
                for index in range(instances - 1)
            ],
            "clocks": [],
            "warnings": [],
        }
    )


class PartitionPressureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = _ir()
        self.platform = Platform.from_dict(
            _platform_value(
                "pressure_platform",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
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
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in self.clusters["clusters"]
        }
        self.initial = build_partition_assignment(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "a",
                by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=7,
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

    def test_model_is_source_bound_and_tamper_evident(self) -> None:
        checked = validate_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            self.model,
        )
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(checked["paths"], 2)
        broken = copy.deepcopy(self.model)
        broken["paths"][0]["base_slack_ns"] += 0.25
        with self.assertRaisesRegex(ValidationError, "reconstruction"):
            validate_partition_pressure_model(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.timing,
                self.route_constraints,
                broken,
            )

    def test_endpoint_exact_fanout_does_not_charge_an_unrelated_sink(self) -> None:
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "fanout_pressure",
                    "top": "fanout_pressure",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {"id": item, "type": "LUT1", "resources": {"lut": 1}}
                    for item in ("driver", "local", "remote")
                ],
                "nets": [
                    {
                        "id": "fanout",
                        "cut_class": "register_output",
                        "drivers": [_endpoint("driver", "O")],
                        "sinks": [
                            _endpoint("local", "I"),
                            _endpoint("remote", "I"),
                        ],
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        platform = Platform.from_dict(
            _platform_value(
                "fanout_platform",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=1, latency=1),
                    _link("bc", "b", "c", lanes=1, latency=1),
                ],
            )
        )
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 2.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        cluster_by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        assignment = {
            cluster_by_instance["driver"]: "a",
            cluster_by_instance["local"]: "a",
            cluster_by_instance["remote"]: "c",
        }
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 2,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "fanout_pressure",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "to-local",
                    "startpoint": _endpoint("driver", "O"),
                    "endpoint": _endpoint("local", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 5.0,
                    "fixed_delay_ns": 5.0,
                    "path_nets": ["fanout"],
                    "normalized_slack": 0.5,
                },
                {
                    "id": "to-remote",
                    "startpoint": _endpoint("driver", "O"),
                    "endpoint": _endpoint("remote", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 5.0,
                    "fixed_delay_ns": 5.0,
                    "path_nets": ["fanout"],
                    "normalized_slack": 0.5,
                },
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        evaluated = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            assignment,
        )
        paths = {record["path"]: record for record in evaluated["paths"]}
        self.assertEqual(paths["to-local"]["transport_delay_ns"], 0.0)
        self.assertGreater(paths["to-remote"]["transport_delay_ns"], 0.0)
        self.assertEqual(
            paths["to-local"]["transition_model"],
            "endpoint-exact-reverse-chain-v1",
        )

        conservative_timing = copy.deepcopy(timing)
        for record in conservative_timing["paths"]:
            record.pop("startpoint")
            record.pop("endpoint")
        conservative_model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            conservative_timing,
            routes,
        )
        conservative = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            conservative_model,
            assignment,
        )
        conservative_paths = {
            record["path"]: record for record in conservative["paths"]
        }
        self.assertEqual(
            conservative_paths["to-local"]["transport_delay_ns"],
            conservative_paths["to-remote"]["transport_delay_ns"],
        )
        self.assertGreater(
            conservative_paths["to-local"]["transport_delay_ns"], 0.0
        )

    def test_global_best_move_improves_critical_path_and_replays(self) -> None:
        before = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
        )
        final, trace = refine_partition_pressure_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
        )
        self.assertGreaterEqual(len(trace["moves"]), 1)
        self.assertGreater(
            trace["final_metrics"]["worst_normalized_slack"],
            before["metrics"]["worst_normalized_slack"],
        )
        checked = validate_partition_pressure_trace(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")
        repeated, repeated_trace = refine_partition_pressure_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
        )
        self.assertEqual(repeated, final)
        self.assertEqual(repeated_trace, trace)

    def test_shared_capacity_direction_changes_pressure(self) -> None:
        shared_platform_value = _platform_value(
            "shared_pressure",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=1, latency=1)],
        )
        shared_platform_value["links"][0]["capacity_sharing"] = (
            "shared_bidirectional"
        )
        shared = Platform.from_dict(shared_platform_value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            self.ir,
            shared,
        )
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
            },
            shared,
        )
        model = build_partition_pressure_model(
            self.ir,
            shared,
            self.clusters,
            constraints,
            self.timing,
            routes,
        )
        evaluation = evaluate_partition_pressure(
            self.ir,
            shared,
            self.clusters,
            constraints,
            routes,
            model,
            self.initial["cluster_assignment"],
        )
        self.assertEqual(len(evaluation["capacity_domains"]), 1)
        self.assertEqual(
            evaluation["metrics"]["maximum_capacity_domain_load"], 2
        )
        self.assertEqual(
            evaluation["metrics"]["maximum_predicted_tdm_ratio"], 2
        )

    def test_reference_runner_writes_a_replayable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = {
                "ir.json": self.ir.value,
                "platform.json": self.platform.to_dict(),
                "clusters.json": self.clusters,
                "constraints.json": self.constraints,
                "timing.json": self.timing,
                "routes.json": self.route_constraints,
                "assignment.json": self.initial,
            }
            for name, value in inputs.items():
                write_json(root / name, value)
            report = run_partition_pressure_reference(
                root / "ir.json",
                root / "platform.json",
                root / "clusters.json",
                root / "constraints.json",
                root / "timing.json",
                root / "routes.json",
                root / "assignment.json",
                root / "out",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                sorted(path.name for path in (root / "out").iterdir()),
                [
                    "assignment.json",
                    "partition_pressure_model.json",
                    "partition_pressure_report.json",
                    "partition_pressure_trace.json",
                ],
            )

    def test_native_matches_exhaustive_move_for_move(self) -> None:
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            executable=str(patron_refiner()),
        )
        checked = validate_partition_pressure_native_against_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")
        self.assertGreaterEqual(checked["moves"], 1)
        bundle = validate_partition_pressure_native_bundle(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(
            bundle["qualification"], "move-for-move-exhaustive"
        )

    def test_native_legacy_fallback_matches_exhaustive(self) -> None:
        timing = copy.deepcopy(self.timing)
        for path in timing["paths"]:
            path.pop("startpoint")
            path.pop("endpoint")
        model = build_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            timing,
            self.route_constraints,
        )
        self.assertEqual(
            {path["transition_model"] for path in model["paths"]},
            {"conservative-net-worst-v1"},
        )
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            model,
            self.initial,
            executable=str(patron_refiner()),
        )
        checked = validate_partition_pressure_native_against_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")

    def test_phase3_patron_provider_refines_imported_tritonpart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            solution_path = root / "initial.part.2"
            write_json(ir_path, self.ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(timing_path, self.timing)
            raw_routes = {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
            }
            write_json(route_path, raw_routes)
            ordered = sorted(
                self.initial["cluster_assignment"]
            )
            part = {"a": 0, "b": 1}
            solution_path.write_text(
                "\n".join(
                    str(part[self.initial["cluster_assignment"][cluster]])
                    for cluster in ordered
                )
                + "\n",
                encoding="utf-8",
            )
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                constraints_path=None,
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                tritonpart_solution=solution_path,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
            )
            trace = (root / "phase3/patron/refinement_trace.json")
            self.assertEqual(report["status"], "pass")
            self.assertTrue(trace.is_file())
            self.assertTrue(
                (root / "phase3/patron/candidate_assignment.json").is_file()
            )
            self.assertEqual(
                report["provider"], "patron-endpoint-exact-native-v2"
            )
            baseline = promote_patron_baseline(
                ir_path, platform_path, root / "phase3"
            )
            restored = read_json(root / "phase3/assignment.json")
            self.assertEqual(
                baseline["provider"], "tritonpart-openroad-hypergraph-v1"
            )
            self.assertEqual(
                restored["cluster_assignment"],
                self.initial["cluster_assignment"],
            )
            frozen_initial_path = root / "frozen-initial.json"
            write_json(frozen_initial_path, self.initial)
            reused = run_phase3(
                ir_path,
                platform_path,
                root / "phase3-reused",
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                patron_initial_assignment_path=frozen_initial_path,
            )
            self.assertEqual(reused["status"], "pass")
            self.assertNotIn("tritonpart", reused["artifacts"])
            self.assertEqual(
                reused["algorithm_validation"]["status"], "pass"
            )
            legacy = copy.deepcopy(self.initial)
            legacy["cluster_assignment"] = {
                f"legacy-{index}": fpga
                for index, fpga in enumerate(
                    self.initial["cluster_assignment"].values()
                )
            }
            legacy_path = root / "legacy-cluster-ids.json"
            write_json(legacy_path, legacy)
            rebased = run_phase3(
                ir_path,
                platform_path,
                root / "phase3-rebased",
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                patron_initial_assignment_path=legacy_path,
            )
            self.assertEqual(rebased["status"], "pass")
            rebased_initial = read_json(
                root / "phase3-rebased/patron/initial_assignment.json"
            )
            self.assertEqual(
                rebased_initial["instance_assignment"],
                self.initial["instance_assignment"],
            )
            self.assertEqual(
                set(rebased_initial["cluster_assignment"]),
                set(self.initial["cluster_assignment"]),
            )

    def test_scalable_native_sweep_improves_a_sparse_chain(self) -> None:
        ir = _chain_ir(300)
        value = _platform_value(
            "pressure_chain_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=8, latency=1)],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 0.25,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            {
                cluster["id"]: ("a" if index % 2 == 0 else "b")
                for index, cluster in enumerate(clusters["clusters"])
            },
            provider="fixture",
            seed=1,
        )
        route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_chain",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": f"p{index:05d}",
                    "startpoint": _endpoint(f"u{index:05d}", "O"),
                    "endpoint": _endpoint(f"u{index + 1:05d}", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": [f"n{index:05d}"],
                    "normalized_slack": 1.0,
                }
                for index in range(299)
            ],
        }
        model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
        )
        before = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial["cluster_assignment"],
        )
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=64,
        )
        after = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            final["cluster_assignment"],
        )
        self.assertEqual(
            trace["mode"], "endpoint-exact-critical-sweep-v2"
        )
        self.assertGreater(len(trace["moves"]), 0)
        scalable_checked = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(scalable_checked["status"], "pass")
        bundle = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(
            bundle["qualification"],
            "linear-transition-and-endpoint-reconstruction",
        )
        self.assertLessEqual(
            bundle["maximum_endpoint_relative_objective_error"],
            1.0e-12,
        )
        tampered = copy.deepcopy(trace)
        tampered["moves"][0]["target"] = tampered["moves"][0]["source"]
        with self.assertRaisesRegex(ValidationError, "transition"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                final,
                tampered,
            )
        endpoint_tampered = copy.deepcopy(trace)
        endpoint_tampered["final_metrics"]["objective_key"][1] += 1.0e-4
        with self.assertRaisesRegex(ValidationError, "endpoint objective"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                final,
                endpoint_tampered,
            )
        self.assertLess(
            after["metrics"]["objective_key"],
            before["metrics"]["objective_key"],
        )


if __name__ == "__main__":
    unittest.main()
