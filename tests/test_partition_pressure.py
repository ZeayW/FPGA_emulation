import copy
import itertools
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    CUT_MODE_STATIC_EXACT,
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
)
from emuflow.combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
)
from emuflow.partition_pressure import (
    _canonical_digest,
    _flow_refinement_configuration,
    _parse_patron_native_output,
    _write_patron_native_input,
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


def _capacity_release_ir(instances: int = 300) -> EmuIR:
    """Large sparse graph whose second improving move needs another sweep."""

    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure_capacity_release",
                "top": "pressure_capacity_release",
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
                    "id": "n00000",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u00000", "O")],
                    "sinks": [_endpoint("u00001", "I")],
                },
                {
                    "id": "n00002",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u00002", "O")],
                    "sinks": [_endpoint("u00003", "I")],
                },
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

    def test_physical_hop_guard_configuration_preserves_legacy_contracts(
        self,
    ) -> None:
        legacy = _flow_refinement_configuration(True, 512, version=2)
        ranked = _flow_refinement_configuration(True, 512, version=3)
        guarded = _flow_refinement_configuration(True, 512, version=4)
        self.assertEqual(legacy["maximum_tail_moves"], 16)
        self.assertNotIn("frontier_selection", legacy)
        self.assertEqual(ranked["maximum_tail_moves"], 256)
        self.assertEqual(
            ranked["frontier_selection"],
            "ranked-worst-path-window-v1",
        )
        self.assertNotIn("physical_hop_guard", ranked)
        self.assertEqual(
            guarded["physical_hop_guard"], "scale_ns*route_hops"
        )
        self.assertEqual(guarded["physical_hop_guard_scale_ns"], 5.0)

    def test_physical_hop_guard_is_charged_once_per_route_arc(self) -> None:
        baseline = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
        )
        guarded = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
            physical_hop_guard_scale_ns=5.0,
        )
        baseline_paths = {
            record["path"]: record for record in baseline["paths"]
        }
        for record in guarded["paths"]:
            self.assertAlmostEqual(
                record["transport_delay_ns"]
                - baseline_paths[record["path"]]["transport_delay_ns"],
                5.0,
                places=12,
            )
        with self.assertRaisesRegex(ValidationError, "non-negative"):
            evaluate_partition_pressure(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                self.model,
                self.initial["cluster_assignment"],
                physical_hop_guard_scale_ns=-0.5,
            )

    def test_v7_native_batch_parser_rejects_tamper(self) -> None:
        before = "2 4 1 2 3 4 5 6"
        after = "1 3 1 2 3 4 5 6"
        ranked = "1000000000 3000000000 1 2 3 4 5 6"
        lines = [
            "EMUFLOW_PATRON_OUTPUT_V7",
            "MODE endpoint-exact-critical-flow-v7",
            f"INITIAL {before}",
            f"BATCH 0 1 {before} {after} {ranked}",
            "CHANGE 0 0 0 1",
            f"FINAL {after}",
            "ASSIGN 0 1",
            "ASSIGN 1 0",
            "END",
        ]
        indexes = {"clusters": ["c0", "c1"], "parts": ["a", "b"]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patron.out"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            assignment, _moves, batches, *_rest = (
                _parse_patron_native_output(path, indexes)
            )
            self.assertEqual(assignment, {"c0": "b", "c1": "a"})
            self.assertEqual(
                batches[0]["changes"],
                [{"cluster": "c0", "source": "a", "target": "b"}],
            )

            changed = list(lines)
            changed[4] = "CHANGE 0 2 0 1"
            path.write_text("\n".join(changed) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "out of range"):
                _parse_patron_native_output(path, indexes)

            truncated = list(lines)
            truncated[3] = f"BATCH 0 2 {before} {after} {ranked}"
            path.write_text(
                "\n".join(truncated) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "coverage"):
                _parse_patron_native_output(path, indexes)

    def test_v9_native_input_and_bundle_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native_input = Path(temporary) / "patron-v9.in"
            native_output = Path(temporary) / "patron-v9.out"
            indexes = _write_patron_native_input(
                native_input,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                self.model,
                self.initial,
                0,
                9,
            )
            lines = native_input.read_text(encoding="utf-8").splitlines()
            flow_fields = lines[2].split()
            self.assertEqual(flow_fields[0], "FLOW")
            self.assertEqual(len(flow_fields), 9)
            for index, line in enumerate(lines):
                fields = line.split()
                if fields and fields[0] == "CLUSTER":
                    fields[3] = fields[2]
                    lines[index] = " ".join(fields)
            native_input.write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    str(patron_refiner()),
                    str(native_input),
                    str(native_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                native_output.read_text(encoding="utf-8").splitlines()[0],
                "EMUFLOW_PATRON_OUTPUT_V9",
            )
            (
                cluster_assignment,
                moves,
                batches,
                initial_metrics,
                final_metrics,
                mode,
            ) = _parse_patron_native_output(native_output, indexes)
        self.assertEqual(mode, "endpoint-exact-critical-flow-v9")
        final = build_partition_assignment(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            cluster_assignment,
            provider="patron-endpoint-exact-flow-native-v9",
            seed=self.initial["seed"],
        )
        trace = {
            "schema": "emuflow.partition-pressure-trace/v9",
            "design": self.model["design"],
            "platform": self.model["platform"],
            "provider": "patron-endpoint-exact-flow-native-v9",
            "mode": mode,
            "configuration": {
                "max_moves": 0,
                "max_scalable_sweeps": self.model["configuration"][
                    "max_scalable_sweeps"
                ],
                "scalable_ejection_critical_limit": self.model[
                    "configuration"
                ]["scalable_ejection_critical_limit"],
                "scalable_ejection_donor_limit": self.model["configuration"]
                ["scalable_ejection_donor_limit"],
                "max_scalable_ejections": self.model["configuration"]
                ["max_scalable_ejections"],
                "flow_refinement": _flow_refinement_configuration(
                    True, len(self.model["clusters"]), version=3
                ),
            },
            "model_sha256": _canonical_digest(self.model),
            "initial_assignment_sha256": _canonical_digest(self.initial),
            "initial_metrics": initial_metrics,
            "moves": moves,
            "batches": batches,
            "final_metrics": final_metrics,
            "final_cluster_assignment_sha256": _canonical_digest(
                cluster_assignment
            ),
        }
        checked = validate_partition_pressure_native_bundle(
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
        self.assertEqual(checked["status"], "pass")

    def test_v10_flow_batch_and_physical_hop_guard_match_small_oracle(
        self,
    ) -> None:
        instance_count = 40
        block_size = 10
        cut_count = 8
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "pressure_flow_oracle",
                    "top": "pressure_flow_oracle",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {
                        "id": f"u{index:03d}",
                        "type": "LUT1",
                        "resources": {"lut": 1},
                    }
                    for index in range(instance_count)
                ],
                "nets": [
                    {
                        "id": f"n{index:03d}",
                        "cut_class": "register_output",
                        "drivers": [
                            _endpoint(
                                f"u{block_size + index:03d}", "O"
                            )
                        ],
                        "sinks": [
                            _endpoint(
                                f"u{2 * block_size + index:03d}", "I"
                            )
                        ],
                    }
                    for index in range(cut_count)
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        value = _platform_value(
            "pressure_flow_oracle_platform",
            ["a", "b", "c", "d"],
            [
                _link("ab", "a", "b", lanes=2, latency=1),
                _link("bc", "b", "c", lanes=2, latency=1),
                _link("cd", "c", "d", lanes=2, latency=1),
            ],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 4,
                "balance_tolerance": 0.5,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        parts = ["a", "b", "c", "d"]
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            {
                by_instance[f"u{index:03d}"]: parts[index // block_size]
                for index in range(instance_count)
            },
            provider="fixture",
            seed=1,
        )
        route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 3,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_flow_oracle",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": f"p{index:03d}",
                    "startpoint": _endpoint(
                        f"u{block_size + index:03d}", "O"
                    ),
                    "endpoint": _endpoint(
                        f"u{2 * block_size + index:03d}", "I"
                    ),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": [f"n{index:03d}"],
                    "normalized_slack": 1.0,
                }
                for index in range(cut_count)
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
        movable = [
            by_instance[f"u{block_size + index:03d}"]
            for index in range(cut_count)
        ]

        flow_only_best_rank = None
        for targets in itertools.product(("c", "d"), repeat=cut_count):
            trial = dict(initial["cluster_assignment"])
            trial.update(dict(zip(movable, targets)))
            try:
                evaluation = evaluate_partition_pressure(
                    ir,
                    platform,
                    clusters,
                    constraints,
                    route_constraints,
                    model,
                    trial,
                    include_tdm_wait=True,
                )
            except ValidationError:
                continue
            objective = evaluation["metrics"]["objective_key"]
            rank = tuple(
                round(float(value) / 1.0e-9)
                if index < 2
                else round(float(value))
                for index, value in enumerate(objective)
            )
            flow_only_best_rank = (
                rank
                if flow_only_best_rank is None
                else min(flow_only_best_rank, rank)
            )
        self.assertIsNotNone(flow_only_best_rank)

        # Every timing path can be made local in this compact fixture.  Since
        # transport delay is non-negative, the following rank is also a
        # constructive lower bound on all eight objective components.
        global_best_rank = (-1000000000, 0, 0, 1, 0, 0, 0, 0)

        results = []
        for _repeat in range(2):
            final, trace = run_partition_pressure_native(
                ir,
                platform,
                clusters,
                constraints,
                route_constraints,
                model,
                initial,
                executable=str(patron_refiner()),
                max_moves=0,
                flow_refinement=True,
            )
            self.assertEqual(len(trace["batches"]), 1)
            self.assertEqual(
                tuple(trace["batches"][0]["ranked_objective_key"]),
                global_best_rank,
            )
            self.assertLess(
                global_best_rank,
                flow_only_best_rank,
            )
            for index in range(cut_count):
                launch = by_instance[
                    f"u{block_size + index:03d}"
                ]
                capture = by_instance[
                    f"u{2 * block_size + index:03d}"
                ]
                self.assertEqual(
                    final["cluster_assignment"][launch],
                    final["cluster_assignment"][capture],
                )
            validation = validate_partition_pressure_native_bundle(
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
            self.assertEqual(validation["status"], "pass")
            results.append((final, trace))
        self.assertEqual(results[0], results[1])

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
                    for item in ("driver", "local", "remote", "remote_peer")
                ],
                "nets": [
                    {
                        "id": "fanout",
                        "cut_class": "register_output",
                        "drivers": [_endpoint("driver", "O")],
                        "sinks": [
                            _endpoint("local", "I"),
                            _endpoint("remote", "I"),
                            _endpoint("remote_peer", "I"),
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
            cluster_by_instance["remote_peer"]: "c",
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
        reduced_fanout_assignment = dict(assignment)
        reduced_fanout_assignment[cluster_by_instance["remote_peer"]] = "a"
        reduced_fanout = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            reduced_fanout_assignment,
        )
        reduced_paths = {
            record["path"]: record for record in reduced_fanout["paths"]
        }
        self.assertAlmostEqual(
            paths["to-remote"]["transport_delay_ns"]
            - reduced_paths["to-remote"]["transport_delay_ns"],
            model["configuration"]["boundary_fanout_penalty_scale_ns"]
            * (math.log2(3.0) - math.log2(2.0)),
        )
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

    def test_exact_mode_uses_an_atomic_ejection_when_balance_blocks_moves(
        self,
    ) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 0.0,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "a",
                by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=7,
        )
        model = build_partition_pressure_model(
            self.ir,
            self.platform,
            clusters,
            constraints,
            self.timing,
            self.route_constraints,
        )
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            clusters,
            constraints,
            self.route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
        )
        self.assertEqual(trace["mode"], "endpoint-exact-global-best-v6")
        self.assertGreaterEqual(len(trace["moves"]), 1)
        self.assertEqual(trace["moves"][0]["kind"], "ejection")
        self.assertIsNotNone(trace["moves"][0]["partner"])
        checked = validate_partition_pressure_native_against_exhaustive(
            self.ir,
            self.platform,
            clusters,
            constraints,
            self.route_constraints,
            model,
            initial,
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
                cut_mode="sequential-only",
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
                report["provider"], "patron-endpoint-exact-native-v6"
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
                cut_mode="sequential-only",
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
                cut_mode="sequential-only",
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

    def test_phase3_patron_consumes_generalized_static_exact_contract(
        self,
    ) -> None:
        ir = _ir()
        ir.value["nets"][0]["cut_class"] = "combinational"
        ir.value["clocks"] = [
            {
                "id": "clk",
                "name": "clk",
                "source_port": "clk",
                "period_ns": None,
            }
        ]
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
                "fixed": [
                    {"instance": "u0", "fpga": "a"},
                    {"instance": "u1", "fpga": "b"},
                ],
            },
            ir,
            self.platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=8,
            comb_segment_budget_slots=1,
            frame_slots=8,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        initial = build_partition_assignment(
            ir,
            self.platform,
            clusters,
            constraints,
            {
                cluster_for["u0"]: "a",
                cluster_for["u1"]: "b",
                cluster_for["u2"]: "a",
                cluster_for["u3"]: "b",
            },
            provider="fixture-static-exact-v2",
            seed=7,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            constraints_path = root / "constraints.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            initial_path = root / "initial.json"
            write_json(ir_path, ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(constraints_path, constraints)
            write_json(timing_path, self.timing)
            write_json(
                route_path,
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                    "tdm_ratio_quantum": 1,
                },
            )
            write_json(initial_path, initial)
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                constraints_path=constraints_path,
                provider="patron",
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                patron_initial_assignment_path=initial_path,
                cut_mode=CUT_MODE_STATIC_EXACT,
                max_cross_fpga_dependency_depth=8,
                comb_segment_budget_slots=1,
                static_exact_candidate_policy=(
                    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                ),
            )
            assignment = read_json(root / "phase3/assignment.json")
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["cut_mode"], "static-exact-combinational"
            )
            self.assertEqual(
                assignment["metrics"]["combinational_cut_nets"], 1
            )
            self.assertEqual(
                assignment["metrics"][
                    "maximum_combinational_dependency_depth"
                ],
                1,
            )
            self.assertEqual(
                report["algorithm_validation"]["status"], "pass"
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
        with patch.dict(
            "os.environ",
            {
                "EMUFLOW_PATRON_FLOW_APPLY": "1",
                "EMUFLOW_PATRON_FLOW_DISTANCE": "999999",
            },
        ):
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
            trace["mode"], "endpoint-exact-critical-ejection-v6"
        )
        self.assertGreater(len(trace["moves"]), 0)
        self.assertEqual(trace["configuration"]["max_scalable_sweeps"], 4)
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

        flow_final, flow_trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=0,
            flow_refinement=True,
        )
        self.assertEqual(
            flow_trace["mode"], "endpoint-exact-critical-flow-v10"
        )
        self.assertEqual(
            flow_trace["configuration"]["flow_refinement"][
                "frontier_selection"
            ],
            "ranked-worst-path-window-v1",
        )
        self.assertTrue(
            flow_trace["configuration"]["flow_refinement"]["enabled"]
        )
        self.assertEqual(
            flow_trace["configuration"]["flow_refinement"][
                "physical_hop_guard_scale_ns"
            ],
            5.0,
        )
        flow_bundle = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
            model,
            initial,
            flow_final,
            flow_trace,
        )
        self.assertEqual(flow_bundle["status"], "pass")
        v9_final, v9_trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=0,
            algorithm_version=9,
        )
        self.assertEqual(v9_trace["mode"], "endpoint-exact-critical-flow-v9")
        self.assertEqual(v9_trace["configuration"]["algorithm_version"], 9)
        self.assertNotIn(
            "physical_hop_guard",
            v9_trace["configuration"]["flow_refinement"],
        )
        self.assertEqual(
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                v9_final,
                v9_trace,
            )["status"],
            "pass",
        )
        flow_config_tampered = copy.deepcopy(flow_trace)
        flow_config_tampered["configuration"]["flow_refinement"][
            "corridor_distance"
        ] += 1
        with self.assertRaisesRegex(ValidationError, "trace bounds"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                flow_final,
                flow_config_tampered,
            )
        guard_tampered = copy.deepcopy(flow_trace)
        guard_tampered["configuration"]["flow_refinement"][
            "physical_hop_guard_scale_ns"
        ] = 0.0
        with self.assertRaisesRegex(ValidationError, "trace bounds"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                flow_final,
                guard_tampered,
            )

    def test_scalable_multipass_revisits_capacity_blocked_cluster(self) -> None:
        ir = _capacity_release_ir()
        value = _platform_value(
            "pressure_capacity_release_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=8, latency=1)],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fixed": [
                    {"instance": "u00000", "fpga": "a"},
                    {"instance": "u00002", "fpga": "b"},
                ],
                "min_used_fpgas": 2,
                "balance_tolerance": 0.01,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        self.assertEqual(len(clusters["clusters"]), 300)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        mapping = {
            cluster["id"]: (
                "a" if int(cluster["instances"][0][1:]) % 2 == 0 else "b"
            )
            for cluster in clusters["clusters"]
        }
        mapping[by_instance["u00002"]] = "b"
        mapping[by_instance["u00003"]] = "a"
        mapping[by_instance["u00005"]] = "a"
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            mapping,
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
            "design": "pressure_capacity_release",
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
                for index in (0, 2)
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
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=8,
        )
        self.assertEqual(
            [
                (move["sweep"], move["cluster"], move["source"], move["target"])
                for move in trace["moves"]
            ],
            [
                (0, by_instance["u00003"], "a", "b"),
                (1, by_instance["u00001"], "b", "a"),
            ],
        )
        checked = validate_partition_pressure_scalable_trace(
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
        self.assertEqual(checked["status"], "pass")
        skipped_sweep = copy.deepcopy(trace)
        skipped_sweep["moves"][1]["sweep"] = 2
        with self.assertRaisesRegex(ValidationError, "sweep"):
            validate_partition_pressure_scalable_trace(
                ir,
                platform,
                clusters,
                constraints,
                route_constraints,
                model,
                initial,
                final,
                skipped_sweep,
            )
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
                skipped_sweep,
            )

    def test_scalable_ejection_escapes_an_exact_balance_cork(self) -> None:
        ir = _capacity_release_ir()
        value = _platform_value(
            "pressure_ejection_platform",
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
                "balance_tolerance": 0.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        mapping = {
            cluster["id"]: (
                "a" if int(cluster["instances"][0][1:]) % 2 == 0 else "b"
            )
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            mapping,
            provider="fixture",
            seed=1,
        )
        routes = normalize_route_constraints(
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
            "design": "pressure_capacity_release",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u00000", "O"),
                    "endpoint": _endpoint("u00001", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["n00000"],
                    "normalized_slack": 1.0,
                }
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        before = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial["cluster_assignment"],
        )
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
        )
        self.assertEqual(
            trace["mode"], "endpoint-exact-critical-ejection-v6"
        )
        self.assertGreaterEqual(len(trace["moves"]), 1)
        self.assertEqual(trace["moves"][0]["kind"], "ejection")
        self.assertLess(
            trace["final_metrics"]["objective_key"],
            before["metrics"]["objective_key"],
        )
        replay = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(replay["status"], "pass")
        bundle = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            routes,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(bundle["status"], "pass")
        tampered = copy.deepcopy(trace)
        tampered["moves"][0]["partner_target"] = (
            tampered["moves"][0]["partner_source"]
        )
        with self.assertRaisesRegex(ValidationError, "transition"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                routes,
                model,
                initial,
                final,
                tampered,
            )

    def test_scalable_ejection_can_release_into_a_third_block(self) -> None:
        raw_ir = copy.deepcopy(_capacity_release_ir().value)
        raw_ir["design"]["name"] = "pressure_third_block_ejection"
        raw_ir["design"]["top"] = "pressure_third_block_ejection"
        raw_ir["nets"] = raw_ir["nets"][:1]
        resources = {
            "u00000": {"lut": 10},
            "u00001": {"lut": 0},
            "u00003": {"lut": 1, "ff": 10},
            "u00004": {"lut": 10, "ff": 10},
        }
        for instance in raw_ir["instances"]:
            instance["resources"] = resources.get(
                instance["id"], {"lut": 1}
            )
        ir = EmuIR(raw_ir)
        value = _platform_value(
            "pressure_third_block_ejection_platform",
            ["a", "b", "c"],
            [
                _link("ab", "a", "b", lanes=8, latency=1),
                _link("ac", "a", "c", lanes=8, latency=50),
                _link("bc", "b", "c", lanes=8, latency=50),
            ],
        )
        capacities = {
            "a": {"lut": 120, "ff": 10},
            "b": {"lut": 108, "ff": 20},
            "c": {"lut": 120, "ff": 20},
        }
        for fpga in value["fpgas"]:
            fpga["capacity"] = capacities[fpga["id"]]
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fixed": [{"instance": "u00001", "fpga": "b"}],
                "min_used_fpgas": 3,
                "balance_tolerance": 2.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        parts = ("a", "b", "c")
        mapping = {
            cluster["id"]: parts[
                int(cluster["instances"][0][1:]) % len(parts)
            ]
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            mapping,
            provider="fixture",
            seed=1,
        )
        routes = normalize_route_constraints(
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
            "design": "pressure_third_block_ejection",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u00000", "O"),
                    "endpoint": _endpoint("u00001", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["n00000"],
                    "normalized_slack": 1.0,
                }
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
        )
        self.assertEqual(
            trace["mode"], "endpoint-exact-critical-ejection-v6"
        )
        self.assertGreaterEqual(len(trace["moves"]), 1)
        first = trace["moves"][0]
        self.assertEqual(first["kind"], "ejection")
        self.assertEqual(first["cluster"], by_instance["u00000"])
        self.assertEqual(first["source"], "a")
        self.assertEqual(first["target"], "b")
        self.assertEqual(first["partner"], by_instance["u00004"])
        self.assertEqual(first["partner_source"], "b")
        self.assertEqual(first["partner_target"], "c")
        replay = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(replay["status"], "pass")


if __name__ == "__main__":
    unittest.main()
