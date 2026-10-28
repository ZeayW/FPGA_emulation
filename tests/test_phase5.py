from tests.native_route_fixture import route_system_native
import copy
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest import mock

from emuflow.common.errors import (
    EmuFlowError,
    TDMScheduleInfeasibleError,
    ValidationError,
)
from emuflow.common.io import read_json
from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.scheduling.phase5 import run_phase5, validate_phase5
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.routing.routing import normalize_route_constraints
from emuflow.scheduling.tdm import _independently_reconstruct_exact_captures, validate_tdm_schedule
from tests.tdm_timing_oracle import timing_summary as reconstruct_tdm_schedule_timing, timing_paths as reconstruct_tdm_schedule_timing_paths, timing_paths as reconstruct_tdm_schedule_timing_paths_from_routes
from tests.schedule_reference import build_tdm_schedule
from tests.schedule_fixture_facts import exact_root_routes
from tests.phase5_fixture import write_native_phase5_fixture
from tests.phase3_store_fixture import write_current_assignment_fixture
from tests.native_build import emuflow_native, tlr_router


ROOT = Path(__file__).resolve().parents[1]


def _platform_value(name, fpga_ids, links):
    return {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "virtual",
            "description": "Phase 5 test topology",
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": "xcvu3p-ffvc1517-2-e",
                "utilization_limit": 1.0,
                "capacity": {"lut": 100, "ff": 100},
            }
            for fpga_id in fpga_ids
        ],
        "links": links,
    }


def _link(link_id, left, right, lanes=1, latency=1, direction="full_duplex"):
    return {
        "id": link_id,
        "endpoints": [left, right],
        "direction": direction,
        "mode": "abstract",
        "data_lanes_per_direction": lanes,
        "fabric_clock_mhz": 250.0,
        "latency_cycles": latency,
    }


def _assignment(platform, cuts):
    return {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": "tdm_test",
        "platform": platform.name,
        "cut_nets": [
            {
                "net": net,
                "cut_class": "register_output",
                "source_fpgas": [source],
                "sink_fpgas": list(sinks),
                "sink_endpoints": len(sinks),
            }
            for net, source, sinks in cuts
        ],
    }


def _routes(platform, cuts, frame_slots):
    assignment = _assignment(platform, cuts)
    constraints = normalize_route_constraints(
        {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": frame_slots,
        },
        platform,
    )
    return exact_root_routes(route_system_native(
        assignment,
        platform,
        constraints,
        executable=str(tlr_router()),
    ))


class Phase5Test(unittest.TestCase):
    def test_retired_provider_is_rejected_before_reading_inputs(self):
        for provider in ("deterministic-round-barrier-earliest-slot-v2", "lagrangian-kkt-v1", "native-timing-dag-ratio-v1"):
            with self.subTest(provider=provider), mock.patch(
                "emuflow.scheduling.phase5.read_system_routes", side_effect=AssertionError("input read"),
            ), self.assertRaisesRegex(ValueError, "unsupported Phase 5 provider"):
                run_phase5(Path("missing"), Path("missing"), Path("unused"), provider=provider)

    def test_untyped_static_exact_input_does_not_fall_back_to_python(self):
        from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest, PLATFORM_PATH
        case = StaticExactCombinationalCutPartitionTest()
        case.setUp()
        _, assignment = case._exact_artifacts()
        routes = case._persisted_routes(case._exact_routes(assignment))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path = root / "routes.json"
            assignment_path = root / "assignment.json"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            write_current_assignment_fixture(assignment_path, case.ir, assignment)
            with self.assertRaisesRegex(ValidationError, "requires the current typed RouteStore"):
                run_phase5(routes_path, PLATFORM_PATH, root / "output", assignment_path)
            self.assertFalse((root / "output").exists())

    def test_retired_schedule_export_and_simulation_apis_are_absent(self):
        from emuflow.scheduling import tdm
        for name in (
            'simulate_tdm_schedule', 'iter_schedule_tsv_lines', 'schedule_to_tsv',
            'write_schedule_tsv', 'build_transport_manifest',
            'schedule_to_systemverilog_testbench',
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(tdm, name))

    def test_zero_demand_schedule_has_no_timing_paths(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "zero_demand",
                ["a", "b"],
                [_link("ab", "a", "b")],
            )
        )
        self.assertEqual(
            reconstruct_tdm_schedule_timing_paths(
                {
                    "schema": "emuflow.system-routes/v1",
                    "routes": [],
                },
                platform,
                {"entries": []},
            ),
            [],
        )

    def test_independently_reconstruct_exact_captures_indexes_100k_segments_once(self) -> None:
        class CountingSegments(dict):
            def __init__(self, value):
                super().__init__(value)
                self.values_calls = 0

            def values(self):
                self.values_calls += 1
                return super().values()

        count = 100_000
        captures = {
            f"capture-{index}": {
                "id": f"capture-{index}",
                "cut_net": f"net-{index}",
                "fpga": "fpga1",
            }
            for index in range(count)
        }
        segments = CountingSegments(
            {
                f"segment-{index}": {
                    "id": f"segment-{index}",
                    "kind": "rx_to_capture",
                    "capture_requirement": f"capture-{index}",
                    "source_cut_net": f"net-{index}",
                    "fpga": "fpga1",
                }
                for index in range(count)
            }
        )
        arrivals = {
            (f"net-{index}", "fpga1"): 1 for index in range(count)
        }
        records, minimum = _independently_reconstruct_exact_captures(
            segments,
            captures,
            arrivals,
            {
                "schema": "emuflow.sampled-virtual-wire-timing-constraints/v1",
                "settle_slots": 1,
                "commit_slot": 3,
                "slot_edge_convention": "tx-sample-before-rx-shadow-update-v1",
            },
        )
        self.assertEqual(len(records), count)
        self.assertEqual(minimum, 1)
        self.assertEqual(segments.values_calls, 1)

    def test_schedule_validation_preserves_hop_coverage_and_semantics(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "two",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=4, latency=2)],
            )
        )
        routes = _routes(
            platform,
            [
                ("n0", "a", ["b"]),
                ("n1", "a", ["b"]),
                ("n2", "b", ["a"]),
            ],
            frame_slots=8,
        )
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(
            schedule["transport_semantics"], "sampled-virtual-wire"
        )
        self.assertEqual(validation["scheduled_bit_hops"], 3)
        self.assertEqual(validation["collisions"], 0)
        mismatched = copy.deepcopy(schedule)
        mismatched["transport_semantics"] = "registered-boundary"
        with self.assertRaisesRegex(
            ValidationError, "transport_semantics"
        ):
            validate_tdm_schedule(routes, platform, mismatched)

    def test_native_schedule_publication_has_no_legacy_exports(self):
        from tests.phase5_stage_fixture import interrupt_native_phase5
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            routes_path, platform_path, assignment_path = write_native_phase5_fixture(root)
            output = root / "phase5"
            report = run_phase5(routes_path, platform_path, output, assignment_path)
            self.assertEqual(report["phase_implementation"], "native-exact-phase5-v1")
            self.assertEqual(
                set(report["artifacts"]),
                {"schedule", "schedule_board", "schedule_store", "cross_layer_timing", "report"},
            )
            self.assertEqual(validate_phase5(routes_path, platform_path, output / "schedule.json", assignment_path)["status"], "pass")
            with read_tdm_schedule(output / "schedule.json") as schedule:
                entries = list(schedule['entries'])
                completions = {row['demand']: row for row in schedule['demand_completions']}
                routes = read_json(routes_path)['routes']
                self.assertTrue(entries)
                self.assertEqual(set(completions), {row['id'] for row in routes})
                arrivals = {(entry['demand'], entry['to']): entry['arrival_slot'] for entry in entries}
                for entry in entries:
                    self.assertLessEqual(entry['ready_slot'], entry['slot'])
                    self.assertGreater(entry['arrival_slot'], entry['slot'])
                for route in routes:
                    completion = completions[route['id']]
                    self.assertEqual(set(completion['sink_arrival_slots']), set(route['sinks']))
                    for sink in route['sinks']:
                        self.assertEqual(completion['sink_arrival_slots'][sink], arrivals[route['id'], sink])
            before = {name: (output / name).read_bytes() for name in report["artifacts"].values()}
            failed = interrupt_native_phase5(routes_path, platform_path, assignment_path, output, "scheduled")
            self.assertEqual(failed.returncode, 71, failed.stderr)
            self.assertIn("injected native Phase5 failure", failed.stderr)
            self.assertEqual({name: (output / name).read_bytes() for name in before}, before)
            self.assertFalse(list(output.glob(".phase5-generation-*")))
            for name in ("schedule.tsv", "transport_manifest.json", "transport_schedule_tb.sv", "tdm_feedback.json"):
                self.assertFalse((output / name).exists())

    def test_generation_rollback_restores_old_changed_route_outputs(self):
        from tests.phase5_stage_fixture import interrupt_native_phase5
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old, platform, assignment = write_native_phase5_fixture(root, prefix="old-")
            changed, _, changed_assignment = write_native_phase5_fixture(root, dependent_return=True, prefix="changed-")
            output = root / "phase5"
            initial = run_phase5(old, platform, output, assignment)
            before = {name: (output / name).read_bytes() for name in initial["artifacts"].values()}
            inventory = {path.name for path in output.iterdir()}
            for position in range(1, 6):
                failed = interrupt_native_phase5(changed, platform, changed_assignment, output, position)
                self.assertEqual(failed.returncode, 71, failed.stderr)
                self.assertEqual({name: (output / name).read_bytes() for name in before}, before)
                self.assertEqual({path.name for path in output.iterdir()}, inventory)
                self.assertEqual(validate_phase5(old, platform, output / "schedule.json", assignment)["status"], "pass")

    def test_report_last_seal_rejects_unrolled_partial_generations(self):
        from tests.phase5_stage_fixture import interrupt_native_phase5
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old, platform, assignment = write_native_phase5_fixture(root, prefix="old-")
            changed, _, changed_assignment = write_native_phase5_fixture(root, dependent_return=True, prefix="changed-")
            for position in range(1, 6):
                output = root / ("phase5-" + str(position))
                run_phase5(old, platform, output, assignment)
                before = (output / "phase5_report.json").read_bytes()
                stopped = interrupt_native_phase5(changed, platform, changed_assignment, output, position, "exit")
                self.assertEqual(stopped.returncode, 72, stopped.stderr)
                if position == 1:
                    self.assertEqual(validate_phase5(old, platform, output / "schedule.json", assignment)["status"], "pass")
                elif position < 5:
                    self.assertEqual((output / "phase5_report.json").read_bytes(), before)
                    with self.assertRaises(ValidationError):
                        validate_phase5(old, platform, output / "schedule.json", assignment)
                else:
                    self.assertEqual(validate_phase5(changed, platform, output / "schedule.json", changed_assignment)["status"], "pass")

    def test_multihop_precedence_includes_store_and_forward_cycle(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "line",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=2, latency=1),
                    _link("bc", "b", "c", lanes=2, latency=1),
                ],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["c"])],
            frame_slots=8,
        )
        schedule = build_tdm_schedule(routes, platform)
        validate_tdm_schedule(routes, platform, schedule)
        entries = sorted(schedule["entries"], key=lambda entry: entry["hop"])
        self.assertEqual(entries[0]["slot"], 1)
        self.assertEqual(entries[0]["arrival_slot"], 2)
        self.assertEqual(entries[1]["ready_slot"], 3)
        self.assertEqual(entries[1]["slot"], 3)
        self.assertEqual(entries[1]["arrival_slot"], 4)

    def test_retired_register_input_round_barrier_routing_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "dependency",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        assignment = _assignment(
            platform,
            [("q", "a", ["b"]), ("d", "b", ["a"])],
        )
        assignment["cut_nets"][1]["cut_class"] = "register_input"
        assignment["cut_nets"][1]["transport_round"] = 1
        constraints = normalize_route_constraints(
            None, platform, frame_slots=8
        )
        with self.assertRaisesRegex(ValidationError, "round-barrier routing is retired"):
            route_system_native(assignment, platform, constraints, executable=str(tlr_router()))

    def test_round_barrier_artifact_is_rejected_before_reading_routes(self):
        with self.assertRaisesRegex(ValidationError, 'unsupported TDM schedule artifact provider'):
            validate_tdm_schedule({}, None, {'provider': 'deterministic-round-barrier-earliest-slot-v2'})

    def test_latency_can_make_route_capacity_schedule_infeasible(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "tight",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
            frame_slots=2,
        )
        with self.assertRaisesRegex(
            TDMScheduleInfeasibleError, "infeasible"
        ):
            build_tdm_schedule(routes, platform)

    def test_scheduler_reserves_final_slot_for_runtime_barrier(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "barrier_slot",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"])],
            frame_slots=2,
        )
        with self.assertRaisesRegex(
            TDMScheduleInfeasibleError, "infeasible"
        ):
            build_tdm_schedule(routes, platform)

    def test_half_duplex_opposing_directions_do_not_collide(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "half",
                ["a", "b"],
                [
                    _link(
                        "ab",
                        "a",
                        "b",
                        lanes=1,
                        latency=1,
                        direction="half_duplex",
                    )
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "b", ["a"])],
        )
        constraints = normalize_route_constraints(
            None, platform, frame_slots=5
        )
        demands = [
            {
                "id": f"d{index:06d}",
                "net": cut["net"],
                "source": cut["source_fpgas"][0],
                "sinks": cut["sink_fpgas"],
                "width_bits": 1,
            }
            for index, cut in enumerate(assignment["cut_nets"])
        ]
        routes = {
            "schema": "emuflow.system-routes/v1",
            "design": assignment["design"],
            "platform": platform.name,
            "provider": "half-duplex-scheduler-fixture",
            "constraints": constraints,
            "demands": demands,
            "routes": [
                {
                    **demand,
                    "tree_edges": [
                        {
                            "link": "ab",
                            "from": demand["source"],
                            "to": demand["sinks"][0],
                        }
                    ],
                    "max_latency_cycles": 1,
                }
                for demand in demands
            ],
            "link_utilization": [
                {
                    "key": "ab:shared",
                    "link": "ab",
                    "direction": "shared",
                    "capacity_bits": 4,
                    "used_bits": 2,
                    "utilization": 0.5,
                }
            ],
            "metrics": {
                "demands": 2,
                "routed_sinks": 2,
                "tree_edges": 2,
                "iterations": 0,
                "max_link_utilization": 0.5,
                "total_link_bit_hops": 2,
            },
        }
        exact_root_routes(routes)
        schedule = build_tdm_schedule(routes, platform)
        validation = validate_tdm_schedule(routes, platform, schedule)
        self.assertEqual(validation["collisions"], 0)
        slots = sorted(entry["slot"] for entry in schedule["entries"])
        self.assertEqual(slots, [1, 2])

    def test_collision_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "collision",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=2, latency=1)],
            )
        )
        routes = _routes(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
            frame_slots=4,
        )
        schedule = build_tdm_schedule(routes, platform)
        broken = copy.deepcopy(schedule)
        broken["entries"][1]["slot"] = broken["entries"][0]["slot"]
        broken["entries"][1]["lane"] = broken["entries"][0]["lane"]
        with self.assertRaisesRegex(ValidationError, "collision"):
            validate_tdm_schedule(routes, platform, broken)

    def test_precedence_violation_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "precedence",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=2, latency=1),
                    _link("bc", "b", "c", lanes=2, latency=1),
                ],
            )
        )
        routes = _routes(platform, [("n0", "a", ["c"])], frame_slots=8)
        schedule = build_tdm_schedule(routes, platform)
        broken = copy.deepcopy(schedule)
        child = next(entry for entry in broken["entries"] if entry["hop"] == 1)
        child["ready_slot"] -= 1
        child["slot"] -= 1
        child["arrival_slot"] -= 1
        with self.assertRaisesRegex(ValidationError, "ready-slot mismatch"):
            validate_tdm_schedule(routes, platform, broken)



if __name__ == "__main__":
    unittest.main()
